"""Verified, read-only export of interactive Codex rollout files.

An export is a private migration bundle, not a live Codex home and not a
backup of all Codex state.  This module only reads ``CODEX_HOME``.  It copies
reviewed rollout JSONL files into an atomically published directory and fails
closed if the source audit, catalog, inventory, or any selected file changes.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import stat
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__
from .archive import (
    UnsafeArchiveState,
    _audit_snapshot_sha256,
    load_verified_audit,
)
from .catalog import CatalogError, find_state_database, read_catalog_signature
from .classifier import RULESET_VERSION, is_valid_thread_id
from .rollouts import inventory


EXPORT_SCHEMA_VERSION = "1.0"
EXPORT_MANIFEST = "manifest.json"
EXPORT_SUMMARY = "summary.txt"
EXPORT_RESTORE = "RESTORE.md"
EXPORT_PRIVATE_SENTINEL = ".codex-history-export-private"
EXPORT_COMPLETE = "COMPLETE"


class ExportError(RuntimeError):
    """Base class for safe, user-facing export failures."""


class UnsafeExportState(ExportError):
    """Raised when an export safety invariant cannot be established."""


ExportProgress = Callable[[int, int, str], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def default_export_path(root: Path) -> Path:
    return root / ("export-%s-%s" % (_timestamp_slug(), secrets.token_hex(4)))


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _containing_git_worktree(path: Path) -> Optional[Path]:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    if current.is_file():
        current = current.parent
    for candidate in (current,) + tuple(current.parents):
        marker = candidate / ".git"
        if marker.is_dir() or marker.is_file():
            return candidate
    return None


def _safe_output_path(output_path: Path, codex_home: Path) -> Path:
    output = output_path.expanduser().resolve(strict=False)
    source = codex_home.expanduser().resolve(strict=True)
    if output.exists() or output.is_symlink():
        raise FileExistsError("output already exists: %s" % output)
    if _is_within(output, source):
        raise UnsafeExportState("private export cannot be inside CODEX_HOME")
    worktree = _containing_git_worktree(output)
    if worktree is not None:
        raise UnsafeExportState(
            "private export cannot be inside a Git worktree: %s" % worktree
        )
    return output


def _safe_codex_home(codex_home: Path) -> Path:
    candidate = codex_home.expanduser()
    try:
        before = os.lstat(candidate)
        resolved = candidate.resolve(strict=True)
        after = os.stat(resolved, follow_symlinks=False)
    except OSError as exc:
        raise UnsafeExportState("CODEX_HOME is missing or unsafe") from exc
    if (
        not stat.S_ISDIR(before.st_mode)
        or not stat.S_ISDIR(after.st_mode)
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
    ):
        raise UnsafeExportState("CODEX_HOME is missing or unsafe")
    return resolved


def _write_bytes(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def _write_json(path: Path, value: Any) -> None:
    _write_bytes(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mkdir_private_tree(staging: Path, relative_parent: PurePosixPath) -> Path:
    current = staging
    for part in relative_parent.parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            if current.is_symlink() or not current.is_dir():
                raise UnsafeExportState("export destination path collision")
        current.chmod(0o700)
    return current


def _safe_relative_rollout(record: Dict[str, Any]) -> PurePosixPath:
    value = record.get("relative_path")
    if not isinstance(value, str):
        raise UnsafeExportState("selected rollout has no relative path")
    relative = PurePosixPath(value)
    storage = record.get("storage_state")
    expected_root = {
        "active": "sessions",
        "archived": "archived_sessions",
    }.get(storage)
    if (
        expected_root is None
        or relative.is_absolute()
        or not relative.parts
        or relative.parts[0] != expected_root
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
        or not relative.name.endswith(".jsonl")
    ):
        raise UnsafeExportState("selected rollout has an unsafe storage path")
    return relative


def _database_path_matches(record: Dict[str, Any], relative: PurePosixPath) -> bool:
    database = record.get("database")
    if not isinstance(database, dict) or database.get("present") is not True:
        return False
    if database.get("archived") is not (record.get("storage_state") == "archived"):
        return False
    value = database.get("relative_rollout_path")
    if not isinstance(value, str):
        return False
    try:
        return PurePosixPath(value).as_posix() == relative.as_posix()
    except (TypeError, ValueError):
        return False


def _selected_records(
    audit: Dict[str, Any], include_empty_shells: bool
) -> List[Tuple[Dict[str, Any], PurePosixPath]]:
    activities = {"meaningful"}
    if include_empty_shells:
        activities.add("empty_shell")

    selected: List[Tuple[Dict[str, Any], PurePosixPath]] = []
    seen_ids = set()
    seen_paths = set()
    for record in audit.get("threads", []):
        if not isinstance(record, dict):
            raise UnsafeExportState("audit contains an invalid thread record")
        if record.get("record_type") != "rollout":
            continue
        if record.get("origin_class") != "interactive":
            continue
        if record.get("activity_state") not in activities:
            continue

        thread_id = record.get("id")
        if not is_valid_thread_id(thread_id):
            raise UnsafeExportState("selected rollout has an invalid thread ID")
        if thread_id in seen_ids:
            raise UnsafeExportState("selected rollouts contain a duplicate thread ID")
        seen_ids.add(thread_id)

        anomalies = record.get("anomalies")
        if anomalies != []:
            raise UnsafeExportState(
                "selected rollout %s has audit anomalies" % thread_id
            )
        if record.get("confidence") != "high" or record.get("file_stable") is not True:
            raise UnsafeExportState(
                "selected rollout %s was not audited as stable and high-confidence"
                % thread_id
            )
        for field in ("file_size", "mtime_ns"):
            value = record.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise UnsafeExportState(
                    "selected rollout %s has invalid file metadata" % thread_id
                )

        relative = _safe_relative_rollout(record)
        relative_text = relative.as_posix()
        if relative_text in seen_paths:
            raise UnsafeExportState("selected rollouts contain a path collision")
        seen_paths.add(relative_text)
        if not _database_path_matches(record, relative):
            raise UnsafeExportState(
                "selected rollout %s does not match the state database" % thread_id
            )
        selected.append((record, relative))

    if not selected:
        raise UnsafeExportState("audit contains no eligible interactive conversations")
    selected.sort(key=lambda item: item[1].as_posix())
    return selected


def _audit_inventory(audit: Dict[str, Any]) -> Dict[str, Tuple[int, int]]:
    result: Dict[str, Tuple[int, int]] = {}
    for record in audit.get("threads", []):
        if not isinstance(record, dict) or record.get("record_type") != "rollout":
            continue
        path = record.get("relative_path")
        size = record.get("file_size")
        mtime_ns = record.get("mtime_ns")
        if (
            not isinstance(path, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not isinstance(mtime_ns, int)
            or isinstance(mtime_ns, bool)
            or path in result
        ):
            raise UnsafeExportState("audit rollout inventory is invalid")
        result[path] = (size, mtime_ns)
    return result


def _live_snapshot(
    codex_home: Path, audit: Dict[str, Any]
) -> Tuple[Dict[str, Tuple[int, int]], Dict[str, Any]]:
    facts, anomalies = inventory(codex_home)
    if anomalies:
        codes = sorted(
            {
                str(item.get("code"))
                for item in anomalies
                if isinstance(item, dict) and item.get("code")
            }
        )
        raise UnsafeExportState(
            "live rollout inventory contains safety anomalies: %s"
            % ", ".join(codes or ["unknown"])
        )
    file_snapshot = {
        path: (fact.size, fact.mtime_ns) for path, fact in facts.items()
    }
    if file_snapshot != _audit_inventory(audit):
        raise UnsafeExportState(
            "source audit is stale; fully stop Codex activity and create a fresh stable audit"
        )

    source = audit.get("source") or {}
    expected_database = source.get("database")
    database_path, candidates = find_state_database(codex_home)
    if (
        database_path is None
        or database_path.name != expected_database
        or candidates != source.get("database_candidates_end")
    ):
        raise UnsafeExportState("selected Codex state database changed after the audit")
    try:
        signature = read_catalog_signature(
            database_path,
            source.get("include_titles") is True,
            source.get("include_cwd") is True,
        )
    except CatalogError as exc:
        raise UnsafeExportState("could not verify the live Codex state database") from exc
    if signature != source.get("database_signature_end"):
        raise UnsafeExportState(
            "source audit is stale; the Codex state database changed"
        )
    return file_snapshot, signature


def _strict_source_path(codex_home: Path, relative: PurePosixPath) -> Path:
    current = codex_home
    for part in relative.parts:
        current = current / part
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise UnsafeExportState("selected rollout is missing") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise UnsafeExportState("selected rollout path contains a symlink")
    try:
        current.resolve(strict=True).relative_to(codex_home)
    except (OSError, ValueError) as exc:
        raise UnsafeExportState("selected rollout escapes CODEX_HOME") from exc
    return current


def _file_snapshot(path: Path) -> Dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise UnsafeExportState("could not safely open selected rollout") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise UnsafeExportState("selected rollout is not a regular file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise UnsafeExportState("selected rollout changed while it was hashed")
        return {
            "device": after.st_dev,
            "inode": after.st_ino,
            "size": after.st_size,
            "mtime_ns": after.st_mtime_ns,
            "sha256": digest.hexdigest(),
        }
    finally:
        os.close(descriptor)


def _snapshot_identity(snapshot: Dict[str, Any]) -> Tuple[int, int, int, int]:
    return (
        snapshot["device"],
        snapshot["inode"],
        snapshot["size"],
        snapshot["mtime_ns"],
    )


def _copy_verified_file(
    source: Path,
    destination: Path,
    expected: Dict[str, Any],
) -> Dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise UnsafeExportState("could not safely open selected rollout for export") from exc
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != _snapshot_identity(expected):
            raise UnsafeExportState("selected rollout changed before it was copied")

        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            digest = hashlib.sha256()
            copied = 0
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise OSError("short write while exporting rollout")
                    view = view[written:]
                    copied += written
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
        destination.chmod(0o600)

        after = os.fstat(source_fd)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != _snapshot_identity(expected):
            raise UnsafeExportState("selected rollout changed while it was copied")
        actual_hash = digest.hexdigest()
        if copied != expected["size"] or actual_hash != expected["sha256"]:
            raise UnsafeExportState("selected rollout content changed while it was copied")
        return {"size": copied, "sha256": actual_hash}
    finally:
        os.close(source_fd)


def _summary_text(summary: Dict[str, Any]) -> str:
    return "\n".join(
        [
            "Codex Interactive History Export",
            "================================",
            "",
            "Conversations: %s" % summary["conversation_count"],
            "Bytes: %s" % summary["payload_bytes"],
            "Active: %s" % summary["by_storage_state"].get("active", 0),
            "Archived: %s" % summary["by_storage_state"].get("archived", 0),
            "Meaningful: %s" % summary["by_activity_state"].get("meaningful", 0),
            "Empty shells: %s" % summary["by_activity_state"].get("empty_shell", 0),
            "",
            "This private bundle contains rollout JSONL files only.",
            "It excludes SQLite state, authentication, configuration, logs, and caches.",
            "See RESTORE.md before using it on another Mac.",
            "",
        ]
    )


def _restore_text() -> str:
    return """# Restoring this private Codex history export

This bundle is intended for a one-time migration to a new Mac **before Codex
or ChatGPT is launched there for the first time**. It contains byte-for-byte
copies of selected interactive rollout files under `sessions/` and
`archived_sessions/`.

It deliberately excludes the Codex SQLite state database, authentication,
configuration, account data, logs, caches, and automated-agent rollouts. It is
therefore not a complete backup of `CODEX_HOME`.

Restoration is a best-effort file migration using undocumented local storage.
It is not a ChatGPT account transfer, a supported cross-account import, or a
guaranteed sidebar/index repair. Use the same ChatGPT account and a compatible
app version when possible.

Keep the destination app closed, make a backup of any existing destination
history, and copy only the bundle's `sessions/` and `archived_sessions/` trees
into the matching locations in the destination `CODEX_HOME`, preserving their
relative paths. Do not copy `manifest.json`, `summary.txt`, `RESTORE.md`, the
private sentinel, or `COMPLETE` into `CODEX_HOME`.

This tool intentionally provides no import or restore command and does not
mutate either machine during export. Review `manifest.json` and keep this
directory private: rollout files contain conversation content and local
metadata.
"""


def export_interactive_history(
    report_directory: Path,
    codex_home: Path,
    output_path: Path,
    include_empty_shells: bool = False,
    progress: Optional[ExportProgress] = None,
) -> Dict[str, Any]:
    """Create and atomically publish a verified interactive-history bundle."""

    source_home = _safe_codex_home(codex_home)
    output = _safe_output_path(output_path, source_home)
    try:
        audit, audit_manifest_sha256 = load_verified_audit(
            report_directory, source_home
        )
    except UnsafeArchiveState as exc:
        raise UnsafeExportState(str(exc)) from exc

    selected = _selected_records(audit, include_empty_shells)
    initial_inventory, initial_database_signature = _live_snapshot(
        source_home, audit
    )

    preflight: Dict[str, Dict[str, Any]] = {}
    source_paths: Dict[str, Path] = {}
    for record, relative in selected:
        path = _strict_source_path(source_home, relative)
        snapshot = _file_snapshot(path)
        if (
            snapshot["size"] != record["file_size"]
            or snapshot["mtime_ns"] != record["mtime_ns"]
        ):
            raise UnsafeExportState("selected rollout changed after the audit")
        relative_text = relative.as_posix()
        preflight[relative_text] = snapshot
        source_paths[relative_text] = path

    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(
        tempfile.mkdtemp(prefix=".%s.tmp-" % output.name, dir=str(output.parent))
    )
    staging.chmod(0o700)
    try:
        _write_bytes(
            staging / EXPORT_PRIVATE_SENTINEL,
            b"Private Codex conversation export. Do not commit, publish, or share.\n",
        )

        payload_entries = []
        total = len(selected)
        for index, (record, relative) in enumerate(selected, start=1):
            parent = _mkdir_private_tree(staging, relative.parent)
            destination = parent / relative.name
            relative_text = relative.as_posix()
            current_source = _strict_source_path(source_home, relative)
            if current_source != source_paths[relative_text]:
                raise UnsafeExportState("selected rollout path changed before copy")
            copied = _copy_verified_file(
                current_source, destination, preflight[relative_text]
            )
            current_source = _strict_source_path(source_home, relative)
            current = _file_snapshot(current_source)
            if current != preflight[relative_text]:
                raise UnsafeExportState("selected rollout changed during the export")
            payload_entries.append(
                {
                    "kind": "rollout",
                    "path": relative_text,
                    "thread_id": record["id"],
                    "storage_state": record["storage_state"],
                    "activity_state": record["activity_state"],
                    "size": copied["size"],
                    "sha256": copied["sha256"],
                }
            )
            if progress:
                progress(index, total, relative_text)

        by_storage = Counter(record["storage_state"] for record, _ in selected)
        by_activity = Counter(record["activity_state"] for record, _ in selected)
        summary = {
            "conversation_count": len(payload_entries),
            "payload_bytes": sum(entry["size"] for entry in payload_entries),
            "by_storage_state": dict(sorted(by_storage.items())),
            "by_activity_state": dict(sorted(by_activity.items())),
        }
        summary_payload = _summary_text(summary).encode("utf-8")
        restore_payload = _restore_text().encode("utf-8")
        _write_bytes(staging / EXPORT_SUMMARY, summary_payload)
        _write_bytes(staging / EXPORT_RESTORE, restore_payload)

        final_inventory, final_database_signature = _live_snapshot(
            source_home, audit
        )
        if (
            final_inventory != initial_inventory
            or final_database_signature != initial_database_signature
        ):
            raise UnsafeExportState("CODEX_HOME changed during the export")
        for _record, relative in selected:
            relative_text = relative.as_posix()
            current_source = _strict_source_path(source_home, relative)
            if _file_snapshot(current_source) != preflight[relative_text]:
                raise UnsafeExportState("selected rollout changed during final verification")

        metadata_entries = [
            {
                "kind": "instructions",
                "path": EXPORT_RESTORE,
                "size": len(restore_payload),
                "sha256": _sha256_bytes(restore_payload),
            },
            {
                "kind": "summary",
                "path": EXPORT_SUMMARY,
                "size": len(summary_payload),
                "sha256": _sha256_bytes(summary_payload),
            },
        ]
        manifest = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "complete": True,
            "tool": {
                "name": "codex-history-audit",
                "version": __version__,
                "ruleset_version": RULESET_VERSION,
            },
            "created_at": _utc_now(),
            "purpose": "interactive_rollout_migration",
            "selection": {
                "origin_class": "interactive",
                "meaningful": True,
                "include_empty_shells": bool(include_empty_shells),
                "storage_states": ["active", "archived"],
            },
            "excludes": [
                "automated_rollouts",
                "ambiguous_rollouts",
                "cloud_tasks",
                "state_database",
                "authentication",
                "configuration",
                "logs",
                "caches",
            ],
            "source_audit_manifest_sha256": audit_manifest_sha256,
            "source_audit_snapshot_sha256": _audit_snapshot_sha256(audit),
            "summary": summary,
            "files": sorted(
                payload_entries + metadata_entries, key=lambda item: item["path"]
            ),
        }
        _write_json(staging / EXPORT_MANIFEST, manifest)

        # Re-read every published member before marking the bundle complete.
        for entry in manifest["files"]:
            member = staging.joinpath(*PurePosixPath(entry["path"]).parts)
            if member.is_symlink() or not member.is_file():
                raise UnsafeExportState("export member disappeared before completion")
            member_snapshot = _file_snapshot(member)
            if (
                member_snapshot["size"] != entry["size"]
                or member_snapshot["sha256"] != entry["sha256"]
            ):
                raise UnsafeExportState("export member failed final checksum verification")

        _write_bytes(staging / EXPORT_COMPLETE, b"complete\n")
        for directory, dirnames, _filenames in os.walk(staging, topdown=False):
            for name in dirnames:
                _fsync_directory(Path(directory) / name)
            _fsync_directory(Path(directory))
        staging.rename(output)
        _fsync_directory(output.parent)
        return manifest
    # KeyboardInterrupt and termination-style exceptions must not strand partial
    # conversation data in the hidden staging directory.
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
