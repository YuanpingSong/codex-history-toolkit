"""Fail-closed planning and execution for official Codex session archiving.

This module never edits rollout files or SQLite directly.  The only mutation it
can initiate is ``codex archive <thread-id>``.  Every invocation is surrounded
by read-only precondition and postcondition checks, and every result is written
to a private, fsynced journal.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import __version__
from .audit import SCHEMA_VERSION as AUDIT_SCHEMA_VERSION
from .audit import run_audit
from .catalog import (
    CatalogError,
    STATE_DB_RE,
    find_state_database,
    read_archive_states,
    read_spawn_edges,
)
from .classifier import (
    RULESET_VERSION,
    is_valid_thread_id,
    normalize_automated_originators,
)
from .reports import PRIVATE_SENTINEL as AUDIT_PRIVATE_SENTINEL
from .reports import REPORT_FILES as AUDIT_REPORT_FILES
from .rollouts import FileFact, inventory


PLAN_SCHEMA_VERSION = "1.1"
RUN_SCHEMA_VERSION = "1.0"
PLAN_FILENAME = "codex-history-archive-plan.json"
PLAN_SUMMARY_FILENAME = "codex-history-archive-plan-summary.txt"
PLAN_PRIVATE_SENTINEL = ".codex-history-archive-private"
PLAN_COMPLETE = "CODEX_HISTORY_ARCHIVE_PLAN_COMPLETE"
RUN_FILENAME = "codex-history-archive-run.json"
RUN_PLAN_FILENAME = "codex-history-archive-run-plan.json"
JOURNAL_FILENAME = "codex-history-archive-journal.jsonl"
RESULT_FILENAME = "codex-history-archive-result.json"
RUN_PRIVATE_SENTINEL = ".codex-history-archive-private"
RUN_COMPLETE = "CODEX_HISTORY_ARCHIVE_RUN_COMPLETE"
RUN_LOCK = ".codex-history-archive-run.lock"
MANIFEST_FILENAME = "manifest.json"

ALLOWED_SELECTIONS = {"automated", "empty-shell", "guardian"}
SUCCESS_STATUSES = {
    "verified_success",
    "verified_success_with_cli_error",
    "verified_success_after_timeout",
    "recovered_verified_success",
}
GLOBAL_BLOCKER_CODES = {
    "CONCURRENT_HISTORY_CHANGE",
    "DATABASE_INTEGRITY_FAILURE",
    "DATABASE_READ_FAILURE",
    "DATABASE_RECHECK_FAILURE",
    "FILE_ADDED_DURING_SCAN",
    "FILE_REMOVED_DURING_SCAN",
    "SPAWN_CHILD_MISSING",
    "SPAWN_EDGE_MISSING",
    "SPAWN_PARENT_MISSING",
    "STATE_DATABASE_SELECTION_CHANGED",
    "SYMLINK_SKIPPED",
    "UNREADABLE_FILE",
}


class ArchiveError(RuntimeError):
    """Base class for a safe, user-facing archive runner failure."""


class UnsafeArchiveState(ArchiveError):
    """Raised when a plan or live history fails a safety invariant."""


class ArchiveOperationStopped(ArchiveError):
    """Raised after a run journals a failed or partial operation and stops."""


@dataclass(frozen=True)
class CommandOutcome:
    exit_code: Optional[int]
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


CommandRunner = Callable[[str, str, Path, float], CommandOutcome]
ProgressReporter = Callable[[int, int, str, str], None]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def default_plan_path(root: Path) -> Path:
    return root / ("archive-plan-%s-%s" % (timestamp_slug(), secrets.token_hex(4)))


def default_run_path(root: Path) -> Path:
    return root / ("archive-run-%s-%s" % (timestamp_slug(), secrets.token_hex(4)))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_bytes(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def _write_json(path: Path, value: Any) -> None:
    _write_bytes(path, json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n")


def _replace_json(path: Path, value: Any) -> None:
    temporary = path.with_name(".%s.tmp-%s" % (path.name, secrets.token_hex(4)))
    _write_json(temporary, value)
    temporary.replace(path)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise UnsafeArchiveState("%s is missing or is not a regular file" % label)


def _load_json(path: Path, label: str) -> Any:
    _regular_file(path, label)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnsafeArchiveState("could not read %s: %s" % (label, exc)) from exc


def _safe_existing_directory(path: Path, label: str) -> Path:
    candidate = path.expanduser()
    try:
        before = os.lstat(candidate)
    except OSError as exc:
        raise UnsafeArchiveState("%s is missing or unsafe" % label) from exc
    if not stat.S_ISDIR(before.st_mode):
        raise UnsafeArchiveState("%s is missing or unsafe" % label)
    try:
        resolved = candidate.resolve(strict=True)
        after = os.stat(resolved, follow_symlinks=False)
    except OSError as exc:
        raise UnsafeArchiveState("%s is missing or unsafe" % label) from exc
    if (
        not stat.S_ISDIR(after.st_mode)
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
    ):
        raise UnsafeArchiveState("%s changed while it was opened" % label)
    return resolved


def _verify_manifest(
    directory: Path,
    expected_files: Sequence[str],
    private_sentinel: str,
    complete_marker: str,
) -> Tuple[Path, str]:
    directory = _safe_existing_directory(directory, "artifact directory")
    _regular_file(directory / private_sentinel, "private sentinel")
    _regular_file(directory / complete_marker, "completion marker")
    manifest_path = directory / MANIFEST_FILENAME
    manifest = _load_json(manifest_path, "manifest")
    if not isinstance(manifest, dict) or manifest.get("complete") is not True:
        raise UnsafeArchiveState("artifact manifest is not complete")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise UnsafeArchiveState("artifact manifest has no file list")

    expected = set(expected_files)
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise UnsafeArchiveState("artifact manifest contains an invalid entry")
        name = entry.get("name")
        if not isinstance(name, str) or PurePosixPath(name).name != name:
            raise UnsafeArchiveState("artifact manifest contains an unsafe filename")
        if name in seen:
            raise UnsafeArchiveState("artifact manifest contains a duplicate filename")
        seen.add(name)
        path = directory / name
        _regular_file(path, "manifest member %s" % name)
        payload = path.read_bytes()
        if entry.get("size") != len(payload) or entry.get("sha256") != _sha256_bytes(
            payload
        ):
            raise UnsafeArchiveState("artifact checksum mismatch for %s" % name)
    if seen != expected:
        raise UnsafeArchiveState("artifact manifest file set does not match expectations")
    return directory, _sha256_bytes(manifest_path.read_bytes())


def _publish_immutable_artifact(
    output_path: Path,
    files: Dict[str, bytes],
    private_sentinel: str,
    complete_marker: str,
) -> None:
    output_path = output_path.resolve()
    if output_path.exists():
        raise FileExistsError("output already exists: %s" % output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(
        tempfile.mkdtemp(
            prefix=".%s.tmp-" % output_path.name, dir=str(output_path.parent)
        )
    )
    staging.chmod(0o700)
    try:
        _write_bytes(
            staging / private_sentinel,
            b"Private local Codex history operation data. Do not publish or commit.\n",
        )
        manifest_entries = []
        for name, payload in sorted(files.items()):
            _write_bytes(staging / name, payload)
            manifest_entries.append(
                {"name": name, "size": len(payload), "sha256": _sha256_bytes(payload)}
            )
        _write_json(
            staging / MANIFEST_FILENAME,
            {"schema_version": "1.0", "complete": True, "files": manifest_entries},
        )
        _write_bytes(staging / complete_marker, b"complete\n")
        _fsync_directory(staging)
        staging.rename(output_path)
    # Cleanup must include KeyboardInterrupt because staging contains private
    # plan data before it is atomically published.
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def codex_home_fingerprint(codex_home: Path) -> str:
    return _sha256_bytes(str(codex_home.resolve()).encode("utf-8"))


def _audit_home_matches(audit: Dict[str, Any], codex_home: Path) -> bool:
    displayed = audit.get("source", {}).get("codex_home")
    if not isinstance(displayed, str) or not displayed:
        return False
    if displayed.startswith("~/"):
        return Path(displayed).expanduser().resolve() == codex_home.resolve()
    candidate = Path(displayed)
    if candidate.is_absolute():
        return candidate.resolve() == codex_home.resolve()
    # Older/private audit output intentionally reduces non-home paths to a basename.
    return candidate.name == codex_home.name


def _configured_automation_from_payload(
    payload: Dict[str, Any], label: str
) -> Tuple[str, ...]:
    classification = payload.get("classification")
    if not isinstance(classification, dict):
        raise UnsafeArchiveState("%s has no classifier configuration" % label)
    names = classification.get("automated_originators")
    if not isinstance(names, list) or any(
        not isinstance(name, str) for name in names
    ):
        raise UnsafeArchiveState("%s classifier configuration is invalid" % label)
    try:
        normalized = normalize_automated_originators(names)
    except ValueError as exc:
        raise UnsafeArchiveState(
            "%s classifier configuration is invalid" % label
        ) from exc
    if names != list(normalized):
        raise UnsafeArchiveState("%s classifier configuration is not normalized" % label)
    return normalized


def _assert_safe_audit_payload(audit: Dict[str, Any], codex_home: Path) -> None:
    if not isinstance(audit, dict):
        raise UnsafeArchiveState("audit.json is not an object")
    if audit.get("schema_version") != AUDIT_SCHEMA_VERSION:
        raise UnsafeArchiveState("unsupported audit schema version")
    tool = audit.get("tool") or {}
    if tool.get("name") != "codex-history-audit":
        raise UnsafeArchiveState("audit was not produced by codex-history-audit")
    if str(tool.get("ruleset_version")) != RULESET_VERSION:
        raise UnsafeArchiveState("audit classifier ruleset does not match this tool")
    _configured_automation_from_payload(audit, "audit")
    if not audit.get("run", {}).get("stable") or not audit.get("summary", {}).get(
        "stable"
    ):
        raise UnsafeArchiveState("audit is not a stable snapshot")
    source = audit.get("source") or {}
    if source.get("database_error"):
        raise UnsafeArchiveState("audit could not read the Codex state database")
    if source.get("database_quick_check") != ["ok"]:
        raise UnsafeArchiveState("audit SQLite quick_check was not ok")
    database = source.get("database")
    if not isinstance(database, str) or Path(database).name != database:
        raise UnsafeArchiveState("audit does not identify one safe state database")
    if not _audit_home_matches(audit, codex_home):
        raise UnsafeArchiveState("audit was produced for a different CODEX_HOME")
    blocker_codes = {
        item.get("code")
        for item in audit.get("anomalies", [])
        if isinstance(item, dict)
    } & GLOBAL_BLOCKER_CODES
    if blocker_codes:
        raise UnsafeArchiveState(
            "audit contains global safety blockers: %s"
            % ", ".join(sorted(str(code) for code in blocker_codes))
        )


def load_verified_audit(report_directory: Path, codex_home: Path) -> Tuple[Dict[str, Any], str]:
    report_directory, manifest_sha256 = _verify_manifest(
        report_directory,
        AUDIT_REPORT_FILES,
        AUDIT_PRIVATE_SENTINEL,
        "COMPLETE",
    )
    audit = _load_json(report_directory / "audit.json", "audit.json")
    _assert_safe_audit_payload(audit, codex_home)
    return audit, manifest_sha256


def _selection_matches(record: Dict[str, Any], selections: Sequence[str]) -> bool:
    selected = set(selections)
    return (
        ("automated" in selected and record.get("origin_class") == "automated")
        or ("guardian" in selected and record.get("surface") == "guardian")
        or (
            "empty-shell" in selected
            and record.get("origin_class") == "interactive"
            and record.get("activity_state") == "empty_shell"
        )
    )


def _relative_db_path(database: Dict[str, Any]) -> Optional[str]:
    value = database.get("relative_rollout_path")
    return os.path.normpath(value) if isinstance(value, str) else None


def _candidate_reason(record: Dict[str, Any]) -> Optional[str]:
    if record.get("record_type") != "rollout":
        return "not_rollout"
    if not is_valid_thread_id(record.get("id")):
        return "invalid_thread_id"
    if record.get("confidence") != "high":
        return "not_high_confidence"
    if record.get("storage_state") == "archived":
        return "already_archived"
    if record.get("storage_state") != "active":
        return "not_active"
    if record.get("file_stable") is not True:
        return "file_not_stable"
    if record.get("anomalies"):
        return "thread_has_anomalies"
    relative_path = record.get("relative_path")
    if not isinstance(relative_path, str) or not relative_path.startswith("sessions/"):
        return "unsafe_active_path"
    database = record.get("database") or {}
    if database.get("present") is not True:
        return "database_row_missing"
    if database.get("archived") is not False:
        return "database_not_active"
    if _relative_db_path(database) != os.path.normpath(relative_path):
        return "database_path_mismatch"
    return None


def _safe_rollout_path(codex_home: Path, relative_path: str, root: str) -> Path:
    relative = PurePosixPath(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.parts[0] != root
        or any(part in {"", ".", ".."} for part in relative.parts)
        or not relative.name.endswith(".jsonl")
    ):
        raise UnsafeArchiveState("unsafe rollout path in plan")
    path = codex_home.joinpath(*relative.parts)
    existing_parent = path.parent
    while not existing_parent.exists() and existing_parent != existing_parent.parent:
        existing_parent = existing_parent.parent
    try:
        resolved_parent = existing_parent.resolve(strict=True)
        resolved_parent.relative_to(codex_home.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise UnsafeArchiveState("rollout parent escapes CODEX_HOME") from exc
    if path.is_symlink():
        raise UnsafeArchiveState("rollout path is a symlink")
    return path


def _archived_relative_path(active_relative_path: str) -> str:
    relative = PurePosixPath(active_relative_path)
    if not relative.parts or relative.parts[0] != "sessions":
        raise UnsafeArchiveState("active rollout path is outside sessions")
    return str(PurePosixPath("archived_sessions") / relative.name)


def _file_snapshot(path: Path) -> Dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise UnsafeArchiveState("could not safely open planned rollout") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise UnsafeArchiveState("planned rollout is not a regular file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise UnsafeArchiveState("planned rollout changed while it was hashed")
        return {
            "size": after.st_size,
            "mtime_ns": after.st_mtime_ns,
            "inode": after.st_ino,
            "sha256": digest.hexdigest(),
        }
    finally:
        os.close(descriptor)


def _records_by_id(audit: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for record in audit.get("threads", []):
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            if record["id"] in result:
                raise UnsafeArchiveState("audit contains duplicate report thread IDs")
            result[record["id"]] = record
    return result


def _record_fields(record: Dict[str, Any]) -> Dict[str, Any]:
    database = record.get("database") or {}
    return {
        "relative_path": record.get("relative_path"),
        "file_size": record.get("file_size"),
        "mtime_ns": record.get("mtime_ns"),
        "storage_state": record.get("storage_state"),
        "origin_class": record.get("origin_class"),
        "surface": record.get("surface"),
        "activity_state": record.get("activity_state"),
        "rule_code": record.get("rule_code"),
        "confidence": record.get("confidence"),
        "file_stable": record.get("file_stable"),
        "anomalies": record.get("anomalies"),
        "database_present": database.get("present"),
        "database_archived": database.get("archived"),
        "database_relative_path": database.get("relative_rollout_path"),
    }


def _audit_snapshot_sha256(audit: Dict[str, Any]) -> str:
    rows = [
        [record.get("id"), record.get("record_type"), _record_fields(record)]
        for record in audit.get("threads", [])
        if isinstance(record, dict)
    ]
    rows.sort(key=lambda row: (str(row[0] or ""), str(row[1] or "")))
    return _sha256_bytes(_canonical_json(rows))


def _fresh_safe_audit(
    codex_home: Path,
    expected_database: str,
    include_titles: bool = False,
    include_cwd: bool = False,
    automated_originators: Iterable[str] = (),
) -> Dict[str, Any]:
    configured_automation = normalize_automated_originators(automated_originators)
    fresh = run_audit(
        codex_home,
        include_titles,
        include_cwd,
        configured_automation,
    )
    _assert_safe_audit_payload(fresh, codex_home)
    if (
        _configured_automation_from_payload(fresh, "fresh audit")
        != configured_automation
    ):
        raise UnsafeArchiveState("fresh audit classifier configuration changed")
    if fresh.get("source", {}).get("database") != expected_database:
        raise UnsafeArchiveState("selected Codex state database changed")
    return fresh


def _read_archive_states_safely(
    database_path: Path,
) -> Dict[str, Dict[str, Any]]:
    try:
        return read_archive_states(database_path)
    except CatalogError as exc:
        raise UnsafeArchiveState("could not read Codex archive state") from exc


def _read_spawn_edges_safely(database_path: Path) -> List[Tuple[str, str]]:
    try:
        return read_spawn_edges(database_path)
    except CatalogError as exc:
        raise UnsafeArchiveState("could not read Codex spawn relationships") from exc


def _spawn_edges_sha256(edges: Sequence[Tuple[str, str]]) -> str:
    return _sha256_bytes(_canonical_json(sorted([list(edge) for edge in edges])))


def _spawn_graph(edges: Sequence[Tuple[str, str]]) -> Dict[str, Tuple[str, ...]]:
    children: Dict[str, set] = {}
    for parent, child in edges:
        if not parent or not child:
            raise UnsafeArchiveState("spawn relationship contains an empty thread ID")
        children.setdefault(parent, set()).add(child)
    return {
        parent: tuple(sorted(child_ids))
        for parent, child_ids in children.items()
    }


def _spawn_descendants(root: str, graph: Dict[str, Tuple[str, ...]]) -> set:
    descendants = set()
    visiting = {root}

    def visit(node: str) -> None:
        for child in graph.get(node, ()):
            if child in visiting:
                raise UnsafeArchiveState("spawn relationships contain a cycle")
            if child in descendants:
                continue
            descendants.add(child)
            visiting.add(child)
            visit(child)
            visiting.remove(child)

    visit(root)
    return descendants


def _descendants_first_order(
    thread_ids: Iterable[str], graph: Dict[str, Tuple[str, ...]]
) -> List[str]:
    selected = set(thread_ids)
    state: Dict[str, int] = {}
    ordered: List[str] = []

    def visit(thread_id: str) -> None:
        marker = state.get(thread_id, 0)
        if marker == 1:
            raise UnsafeArchiveState("selected spawn relationships contain a cycle")
        if marker == 2:
            return
        state[thread_id] = 1
        for child in graph.get(thread_id, ()):
            if child in selected:
                visit(child)
        state[thread_id] = 2
        ordered.append(thread_id)

    for thread_id in sorted(selected):
        visit(thread_id)
    return ordered


def build_archive_plan(
    report_directory: Path,
    codex_home: Path,
    selections: Sequence[str],
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    codex_home = codex_home.expanduser().resolve()
    normalized_selections = sorted(set(selections))
    if not normalized_selections or not set(normalized_selections).issubset(
        ALLOWED_SELECTIONS
    ):
        raise UnsafeArchiveState("choose one or more supported archive selections")
    if limit is not None and limit <= 0:
        raise UnsafeArchiveState("archive plan limit must be positive")

    source_audit, source_manifest_sha256 = load_verified_audit(
        report_directory, codex_home
    )
    configured_automation = _configured_automation_from_payload(
        source_audit, "source audit"
    )
    source_info = source_audit.get("source") or {}
    if source_info.get("spawn_edge_table_available") is not True:
        raise UnsafeArchiveState(
            "Codex spawn relationships are unavailable; refusing archive planning"
        )
    database_name = source_info["database"]
    fresh = _fresh_safe_audit(
        codex_home,
        database_name,
        include_titles=source_info.get("include_titles") is True,
        include_cwd=source_info.get("include_cwd") is True,
        automated_originators=configured_automation,
    )
    fresh_info = fresh.get("source") or {}
    if fresh_info.get("spawn_edge_table_available") is not True:
        raise UnsafeArchiveState(
            "Codex spawn relationships became unavailable during planning"
        )
    if (
        source_info.get("database_signature_end")
        != fresh_info.get("database_signature_start")
        or _audit_snapshot_sha256(source_audit) != _audit_snapshot_sha256(fresh)
    ):
        raise UnsafeArchiveState(
            "source audit is stale; fully quit Codex activity and create a fresh stable audit"
        )

    _records_by_id(source_audit)
    selected_records = [
        record
        for record in source_audit.get("threads", [])
        if isinstance(record, dict)
        and _selection_matches(record, normalized_selections)
    ]
    excluded = Counter()
    eligible = []
    for record in selected_records:
        reason = _candidate_reason(record)
        if reason:
            excluded[reason] += 1
        else:
            eligible.append(record)

    database_path = _database_path(codex_home, database_name)
    states = _read_archive_states_safely(database_path)
    spawn_edges = _read_spawn_edges_safely(database_path)
    graph = _spawn_graph(spawn_edges)
    eligible_by_id = {record["id"]: record for record in eligible}
    if len(eligible_by_id) != len(eligible):
        raise UnsafeArchiveState("archive candidates contain duplicate thread IDs")
    eligible_ids = set(eligible_by_id)
    cascade_unsafe = set()
    for thread_id in eligible_ids:
        for descendant in _spawn_descendants(thread_id, graph):
            descendant_state = states.get(descendant)
            if descendant_state is None or (
                not descendant_state["archived"] and descendant not in eligible_ids
            ):
                cascade_unsafe.add(thread_id)
                break
    if cascade_unsafe:
        excluded["active_descendant_not_selected"] += len(cascade_unsafe)
        eligible_ids.difference_update(cascade_unsafe)

    ordered_ids = _descendants_first_order(eligible_ids, graph)
    eligible_before_descendant_safety = len(eligible)
    eligible_before_limit = len(ordered_ids)
    if limit is not None:
        excluded["beyond_limit"] += max(0, len(ordered_ids) - limit)
        ordered_ids = ordered_ids[:limit]
    eligible = [eligible_by_id[thread_id] for thread_id in ordered_ids]
    if not eligible:
        raise UnsafeArchiveState("archive selection produced no eligible active threads")

    fresh_by_id = _records_by_id(fresh)
    targets = []
    for source_record in eligible:
        thread_id = source_record["id"]
        current = fresh_by_id.get(thread_id)
        if current is None or _record_fields(current) != _record_fields(source_record):
            raise UnsafeArchiveState(
                "source audit is stale for a selected thread; create a fresh stable audit"
            )
        if _candidate_reason(current) is not None:
            raise UnsafeArchiveState("selected thread no longer meets archive invariants")
        path = _safe_rollout_path(
            codex_home, current["relative_path"], "sessions"
        )
        snapshot = _file_snapshot(path)
        if snapshot["size"] != current["file_size"] or snapshot["mtime_ns"] != current[
            "mtime_ns"
        ]:
            raise UnsafeArchiveState("selected rollout changed after the fresh audit")
        archived_relative = _archived_relative_path(current["relative_path"])
        archived_path = codex_home / archived_relative
        if archived_path.exists() or archived_path.is_symlink():
            raise UnsafeArchiveState("planned archive destination already exists")
        database_state = states.get(thread_id)
        if (
            database_state is None
            or database_state["archived"]
            or not _path_matches(database_state["rollout_path"], path, codex_home)
            or not _is_sha256(database_state.get("metadata_sha256"))
        ):
            raise UnsafeArchiveState("selected database row changed during planning")
        targets.append(
            {
                "id": thread_id,
                "relative_path": current["relative_path"],
                "archived_relative_path": archived_relative,
                "file_size": snapshot["size"],
                "mtime_ns": snapshot["mtime_ns"],
                "inode": snapshot["inode"],
                "sha256": snapshot["sha256"],
                "database_metadata_sha256": database_state["metadata_sha256"],
                "origin_class": current["origin_class"],
                "surface": current["surface"],
                "activity_state": current["activity_state"],
                "rule_code": current["rule_code"],
            }
        )

    by_origin = Counter(target["origin_class"] for target in targets)
    by_surface = Counter(target["surface"] for target in targets)
    by_activity = Counter(target["activity_state"] for target in targets)
    created_at = utc_now()
    base = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "tool": {
            "name": "codex-history-audit",
            "version": __version__,
            "ruleset_version": RULESET_VERSION,
        },
        "classification": {
            "automated_originators": list(configured_automation),
        },
        "created_at": created_at,
        "codex_home_sha256": codex_home_fingerprint(codex_home),
        "database": database_name,
        "source_audit_manifest_sha256": source_manifest_sha256,
        "source_snapshot_sha256": _audit_snapshot_sha256(source_audit),
        "spawn_edges_sha256": _spawn_edges_sha256(spawn_edges),
        "execution_order": "spawned_descendants_first",
        "selections": normalized_selections,
        "summary": {
            "selected_records": len(selected_records),
            "eligible_before_descendant_safety": eligible_before_descendant_safety,
            "eligible_before_limit": eligible_before_limit,
            "target_count": len(targets),
            "target_bytes": sum(target["file_size"] for target in targets),
            "guardian_count": by_surface.get("guardian", 0),
            "by_origin_class": dict(sorted(by_origin.items())),
            "by_surface": dict(sorted(by_surface.items())),
            "by_activity_state": dict(sorted(by_activity.items())),
            "excluded_by_reason": dict(sorted(excluded.items())),
        },
        "targets": targets,
    }
    plan_id = _sha256_bytes(_canonical_json(base))
    return dict(base, plan_id=plan_id, confirmation_token=plan_id[:16])


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64 or value.lower() != value:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_plan_rollout_path(value: Any, root: str) -> bool:
    if not isinstance(value, str):
        return False
    relative = PurePosixPath(value)
    return (
        not relative.is_absolute()
        and bool(relative.parts)
        and relative.parts[0] == root
        and all(part not in {"", ".", ".."} for part in relative.parts)
        and relative.name.endswith(".jsonl")
    )


def _validate_plan_object(plan: Dict[str, Any]) -> None:
    if not isinstance(plan, dict) or plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise UnsafeArchiveState("unsupported archive plan schema")
    plan_id = plan.get("plan_id")
    token = plan.get("confirmation_token")
    if not _is_sha256(plan_id) or token != plan_id[:16]:
        raise UnsafeArchiveState("archive plan identity is invalid")
    base = dict(plan)
    base.pop("plan_id", None)
    base.pop("confirmation_token", None)
    if _sha256_bytes(_canonical_json(base)) != plan_id:
        raise UnsafeArchiveState("archive plan content does not match its identity")
    tool = plan.get("tool") or {}
    if (
        tool.get("name") != "codex-history-audit"
        or str(tool.get("ruleset_version")) != RULESET_VERSION
    ):
        raise UnsafeArchiveState("archive plan tool identity is incompatible")
    _configured_automation_from_payload(plan, "archive plan")
    database = plan.get("database")
    if not isinstance(database, str) or STATE_DB_RE.fullmatch(database) is None:
        raise UnsafeArchiveState("archive plan database name is invalid")
    for field in (
        "codex_home_sha256",
        "source_audit_manifest_sha256",
        "source_snapshot_sha256",
        "spawn_edges_sha256",
    ):
        if not _is_sha256(plan.get(field)):
            raise UnsafeArchiveState("archive plan %s is invalid" % field)
    if plan.get("execution_order") != "spawned_descendants_first":
        raise UnsafeArchiveState("archive plan execution order is unsafe")
    selections = plan.get("selections")
    if (
        not isinstance(selections, list)
        or not selections
        or any(not isinstance(value, str) for value in selections)
        or selections != sorted(set(selections))
        or not set(selections).issubset(ALLOWED_SELECTIONS)
    ):
        raise UnsafeArchiveState("archive plan selections are invalid")
    targets = plan.get("targets")
    if not isinstance(targets, list) or not targets:
        raise UnsafeArchiveState("archive plan contains no targets")
    seen = set()
    seen_paths = set()
    for target in targets:
        if not isinstance(target, dict) or not is_valid_thread_id(target.get("id")):
            raise UnsafeArchiveState("archive plan contains an invalid target")
        if target["id"] in seen:
            raise UnsafeArchiveState("archive plan contains a duplicate target")
        seen.add(target["id"])
        if not _valid_plan_rollout_path(target.get("relative_path"), "sessions"):
            raise UnsafeArchiveState("archive plan contains an unsafe active path")
        if not _valid_plan_rollout_path(
            target.get("archived_relative_path"), "archived_sessions"
        ):
            raise UnsafeArchiveState("archive plan contains an unsafe archive path")
        if target.get("archived_relative_path") != _archived_relative_path(
            target["relative_path"]
        ):
            raise UnsafeArchiveState("archive plan destination is inconsistent")
        for path in (target["relative_path"], target["archived_relative_path"]):
            if path in seen_paths:
                raise UnsafeArchiveState("archive plan contains a duplicate path")
            seen_paths.add(path)
        for field in ("file_size", "mtime_ns", "inode"):
            if not _is_nonnegative_int(target.get(field)):
                raise UnsafeArchiveState("archive plan target %s is invalid" % field)
        for field in ("sha256", "database_metadata_sha256"):
            if not _is_sha256(target.get(field)):
                raise UnsafeArchiveState("archive plan target %s is invalid" % field)
        for field in ("origin_class", "surface", "activity_state", "rule_code"):
            if not isinstance(target.get(field), str) or not target[field]:
                raise UnsafeArchiveState(
                    "archive plan target classification is invalid"
                )
        if not _selection_matches(target, selections):
            raise UnsafeArchiveState(
                "archive plan target does not match its declared selections"
            )

    summary = plan.get("summary")
    if not isinstance(summary, dict):
        raise UnsafeArchiveState("archive plan summary is invalid")
    expected_counters = {
        "by_origin_class": Counter(target["origin_class"] for target in targets),
        "by_surface": Counter(target["surface"] for target in targets),
        "by_activity_state": Counter(target["activity_state"] for target in targets),
    }
    if summary.get("target_count") != len(targets) or summary.get(
        "target_bytes"
    ) != sum(target["file_size"] for target in targets):
        raise UnsafeArchiveState("archive plan summary does not match its targets")
    if summary.get("guardian_count") != sum(
        target["surface"] == "guardian" for target in targets
    ):
        raise UnsafeArchiveState("archive plan guardian count is inconsistent")
    for key, counter in expected_counters.items():
        if summary.get(key) != dict(sorted(counter.items())):
            raise UnsafeArchiveState("archive plan summary counter is inconsistent")
    for key in (
        "selected_records",
        "eligible_before_descendant_safety",
        "eligible_before_limit",
    ):
        if not _is_nonnegative_int(summary.get(key)):
            raise UnsafeArchiveState("archive plan summary count is invalid")
    excluded = summary.get("excluded_by_reason")
    if not isinstance(excluded, dict) or any(
        not isinstance(key, str) or not _is_nonnegative_int(value)
        for key, value in excluded.items()
    ):
        raise UnsafeArchiveState("archive plan exclusion counts are invalid")


def render_plan_summary(plan: Dict[str, Any]) -> str:
    summary = plan["summary"]
    lines = [
        "Codex History Archive Plan",
        "==========================",
        "",
        "This plan has not changed CODEX_HOME.",
        "Plan ID: %s" % plan["plan_id"],
        "Confirmation token: %s" % plan["confirmation_token"],
        "Created: %s" % plan["created_at"],
        "Selections: %s" % ", ".join(plan["selections"]),
        "Execution order: spawned descendants before parents",
        "Targets: %s" % summary["target_count"],
        "Target bytes: %s" % summary["target_bytes"],
        "Guardian targets (included in automated): %s" % summary["guardian_count"],
        "",
        "Targets by surface",
    ]
    for key, value in sorted(summary["by_surface"].items()):
        lines.append("  %-12s %s" % (key, value))
    lines.extend(["", "Excluded selected records"])
    if summary["excluded_by_reason"]:
        for key, value in sorted(summary["excluded_by_reason"].items()):
            lines.append("  %-24s %s" % (key, value))
    else:
        lines.append("  (none)")
    lines.extend(
        [
            "",
            "Apply only after reviewing this summary. The apply command will require",
            "the confirmation token and will verify every database/file transition.",
        ]
    )
    return "\n".join(lines) + "\n"


def publish_archive_plan(plan: Dict[str, Any], output_path: Path) -> None:
    _validate_plan_object(plan)
    plan_payload = json.dumps(plan, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    summary_payload = render_plan_summary(plan).encode("utf-8")
    _publish_immutable_artifact(
        output_path,
        {PLAN_FILENAME: plan_payload, PLAN_SUMMARY_FILENAME: summary_payload},
        PLAN_PRIVATE_SENTINEL,
        PLAN_COMPLETE,
    )


def load_verified_plan(plan_directory: Path) -> Dict[str, Any]:
    plan_directory, _ = _verify_manifest(
        plan_directory,
        (PLAN_FILENAME, PLAN_SUMMARY_FILENAME),
        PLAN_PRIVATE_SENTINEL,
        PLAN_COMPLETE,
    )
    plan = _load_json(plan_directory / PLAN_FILENAME, "archive plan")
    _validate_plan_object(plan)
    return plan


def _database_path(codex_home: Path, expected_name: str) -> Path:
    selected, _ = find_state_database(codex_home)
    if selected is None or selected.name != expected_name:
        raise UnsafeArchiveState("Codex state database changed since planning")
    return selected


def _path_matches(value: str, expected: Path, codex_home: Path) -> bool:
    if not value:
        return False
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = codex_home / candidate
    try:
        return candidate.resolve(strict=False) == expected.resolve(strict=False)
    except OSError:
        return False


def _target_matches_fresh_record(target: Dict[str, Any], record: Dict[str, Any]) -> bool:
    database = record.get("database") or {}
    return (
        record.get("record_type") == "rollout"
        and record.get("id") == target["id"]
        and record.get("relative_path") == target["relative_path"]
        and record.get("storage_state") == "active"
        and record.get("file_size") == target["file_size"]
        and record.get("mtime_ns") == target["mtime_ns"]
        and record.get("file_stable") is True
        and record.get("origin_class") == target["origin_class"]
        and record.get("surface") == target["surface"]
        and record.get("activity_state") == target["activity_state"]
        and record.get("rule_code") == target["rule_code"]
        and record.get("confidence") == "high"
        and not record.get("anomalies")
        and database.get("present") is True
        and database.get("archived") is False
        and os.path.normpath(database.get("relative_rollout_path") or "")
        == os.path.normpath(target["relative_path"])
    )


def _validate_cascade_preconditions(
    plan: Dict[str, Any],
    states: Dict[str, Dict[str, Any]],
    spawn_edges: Sequence[Tuple[str, str]],
) -> None:
    if _spawn_edges_sha256(spawn_edges) != plan.get("spawn_edges_sha256"):
        raise UnsafeArchiveState("spawn relationships changed since planning")
    graph = _spawn_graph(spawn_edges)
    target_ids = [target["id"] for target in plan["targets"]]
    if _descendants_first_order(target_ids, graph) != target_ids:
        raise UnsafeArchiveState("archive targets are not ordered descendants first")
    target_set = set(target_ids)
    for thread_id in target_ids:
        for descendant in _spawn_descendants(thread_id, graph):
            state = states.get(descendant)
            if state is None or (not state["archived"] and descendant not in target_set):
                raise UnsafeArchiveState(
                    "archive target has an active spawned descendant outside the plan"
                )


def validate_plan_preconditions(plan: Dict[str, Any], codex_home: Path) -> None:
    _validate_plan_object(plan)
    codex_home = codex_home.expanduser().resolve()
    if codex_home_fingerprint(codex_home) != plan.get("codex_home_sha256"):
        raise UnsafeArchiveState("archive plan belongs to a different CODEX_HOME")
    fresh = _fresh_safe_audit(
        codex_home,
        plan["database"],
        automated_originators=_configured_automation_from_payload(
            plan, "archive plan"
        ),
    )
    current = _records_by_id(fresh)
    database_path = _database_path(codex_home, plan["database"])
    states = _read_archive_states_safely(database_path)
    spawn_edges = _read_spawn_edges_safely(database_path)
    _validate_cascade_preconditions(plan, states, spawn_edges)
    for target in plan["targets"]:
        record = current.get(target["id"])
        if (
            record is None
            or not _target_matches_fresh_record(target, record)
            or _candidate_reason(record) is not None
            or not _selection_matches(record, plan["selections"])
        ):
            raise UnsafeArchiveState(
                "archive plan is stale; create a new stable audit and plan"
            )
        active_path = _safe_rollout_path(
            codex_home, target["relative_path"], "sessions"
        )
        snapshot = _file_snapshot(active_path)
        for key in ("size", "mtime_ns", "inode", "sha256"):
            planned_key = "file_size" if key == "size" else key
            if snapshot[key] != target[planned_key]:
                raise UnsafeArchiveState("planned rollout changed after planning")
        archived_path = _safe_rollout_path(
            codex_home,
            target["archived_relative_path"],
            "archived_sessions",
        )
        if archived_path.exists() or archived_path.is_symlink():
            raise UnsafeArchiveState("planned archive destination now exists")
        state = states.get(target["id"])
        if (
            state is None
            or state["archived"]
            or not _path_matches(state["rollout_path"], active_path, codex_home)
            or state.get("metadata_sha256")
            != target["database_metadata_sha256"]
        ):
            raise UnsafeArchiveState("planned database row is no longer active")


def _inventory_digest(
    facts: Dict[str, FileFact], excluded_paths: Iterable[str]
) -> str:
    excluded = set(excluded_paths)
    rows = [
        [path, fact.size, fact.mtime_ns, fact.inode]
        for path, fact in sorted(facts.items())
        if path not in excluded
    ]
    return _sha256_bytes(_canonical_json(rows))


def _states_digest(
    states: Dict[str, Dict[str, Any]], excluded_ids: Iterable[str]
) -> str:
    excluded = set(excluded_ids)
    rows = [
        [
            thread_id,
            state["rollout_path"],
            state["archived"],
            state["archived_at"],
            state["metadata_sha256"],
        ]
        for thread_id, state in sorted(states.items())
        if thread_id not in excluded
    ]
    return _sha256_bytes(_canonical_json(rows))


def create_archive_run(
    plan: Dict[str, Any], codex_home: Path, output_path: Path
) -> Path:
    validate_plan_preconditions(plan, codex_home)
    codex_home = codex_home.expanduser().resolve()
    database_path = _database_path(codex_home, plan["database"])
    facts, anomalies = inventory(codex_home)
    if anomalies:
        raise UnsafeArchiveState("filesystem inventory contains unsafe entries")
    states = _read_archive_states_safely(database_path)
    spawn_edges = _read_spawn_edges_safely(database_path)
    _validate_cascade_preconditions(plan, states, spawn_edges)
    target_paths = {
        path
        for target in plan["targets"]
        for path in (target["relative_path"], target["archived_relative_path"])
    }
    target_ids = {target["id"] for target in plan["targets"]}
    run = {
        "schema_version": RUN_SCHEMA_VERSION,
        "created_at": utc_now(),
        "plan_id": plan["plan_id"],
        "plan_sha256": _sha256_bytes(_canonical_json(plan)),
        "codex_home_sha256": plan["codex_home_sha256"],
        "database": plan["database"],
        "target_count": len(plan["targets"]),
        "spawn_edges_sha256": _spawn_edges_sha256(spawn_edges),
        "unrelated_inventory_sha256": _inventory_digest(facts, target_paths),
        "unrelated_archive_state_sha256": _states_digest(states, target_ids),
    }

    output_path = output_path.expanduser().resolve()
    if output_path.exists():
        raise FileExistsError("output already exists: %s" % output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(
        tempfile.mkdtemp(
            prefix=".%s.tmp-" % output_path.name, dir=str(output_path.parent)
        )
    )
    staging.chmod(0o700)
    try:
        _write_bytes(
            staging / RUN_PRIVATE_SENTINEL,
            b"Private local Codex archive journal. Do not publish or commit.\n",
        )
        _write_json(staging / RUN_FILENAME, run)
        _write_json(staging / RUN_PLAN_FILENAME, plan)
        _write_bytes(staging / JOURNAL_FILENAME, b"")
        _fsync_directory(staging)
        staging.rename(output_path)
        return output_path
    # Cleanup must include KeyboardInterrupt because staging contains the
    # private run plan and journal.
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


class Journal:
    def __init__(self, path: Path, recover_torn_tail: bool = False):
        self.path = path
        _regular_file(path, "archive journal")
        if not hasattr(os, "O_NOFOLLOW"):
            raise UnsafeArchiveState("this platform cannot safely open the archive journal")
        self.entries: List[Dict[str, Any]] = []
        self.recovered_tail: Optional[Dict[str, Any]] = None
        previous = "0" * 64
        descriptor = -1
        try:
            flags = os.O_RDWR | os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            descriptor = os.open(path, flags)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafeArchiveState("archive journal is not a safe regular file")
            self._identity = (info.st_dev, info.st_ino)
            payload = b""
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                payload += chunk
            complete_payload = payload
            torn_tail: Optional[bytes] = None
            if payload and not payload.endswith(b"\n"):
                last_newline = payload.rfind(b"\n")
                complete_payload = payload[: last_newline + 1]
                tail = payload[last_newline + 1 :]
                if not recover_torn_tail:
                    raise UnsafeArchiveState(
                        "archive journal has an unterminated final record; use archive resume"
                    )
                torn_tail = tail

            lines = complete_payload.split(b"\n")
            if lines and lines[-1] == b"":
                lines.pop()
            for sequence, line in enumerate(lines, start=1):
                if not line:
                    raise UnsafeArchiveState("archive journal contains an empty record")
                entry = json.loads(line.decode("utf-8"))
                if not isinstance(entry, dict) or entry.get("sequence") != sequence:
                    raise UnsafeArchiveState("archive journal sequence is invalid")
                claimed = entry.get("entry_sha256")
                base = dict(entry)
                base.pop("entry_sha256", None)
                if base.get("previous_sha256") != previous:
                    raise UnsafeArchiveState("archive journal hash chain is broken")
                actual = _sha256_bytes(_canonical_json(base))
                if claimed != actual:
                    raise UnsafeArchiveState("archive journal entry checksum is invalid")
                previous = actual
                self.entries.append(entry)
            if torn_tail is not None:
                os.ftruncate(descriptor, len(complete_payload))
                os.fsync(descriptor)
                self.recovered_tail = {
                    "size": len(torn_tail),
                    "sha256": _sha256_bytes(torn_tail),
                }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UnsafeArchiveState("could not read archive journal") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self.previous_sha256 = previous

    def append(self, event: str, **values: Any) -> Dict[str, Any]:
        base = {
            "sequence": len(self.entries) + 1,
            "timestamp": utc_now(),
            "previous_sha256": self.previous_sha256,
            "event": event,
        }
        base.update(values)
        entry = dict(base, entry_sha256=_sha256_bytes(_canonical_json(base)))
        payload = _canonical_json(entry) + b"\n"
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = -1
        try:
            descriptor = os.open(self.path, flags)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != self._identity
            ):
                raise UnsafeArchiveState("archive journal changed during the run")
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short archive journal write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self.entries.append(entry)
        self.previous_sha256 = entry["entry_sha256"]
        return entry


def _load_run(
    run_directory: Path, recover_torn_tail: bool = False
) -> Tuple[Dict[str, Any], Dict[str, Any], Journal]:
    run_directory = _safe_existing_directory(run_directory, "archive run directory")
    _regular_file(run_directory / RUN_PRIVATE_SENTINEL, "run private sentinel")
    run = _load_json(run_directory / RUN_FILENAME, "archive run metadata")
    plan = _load_json(run_directory / RUN_PLAN_FILENAME, "archive run plan")
    _validate_plan_object(plan)
    if (
        not isinstance(run, dict)
        or run.get("schema_version") != RUN_SCHEMA_VERSION
        or run.get("plan_id") != plan["plan_id"]
        or run.get("plan_sha256") != _sha256_bytes(_canonical_json(plan))
        or run.get("target_count") != len(plan["targets"])
        or run.get("spawn_edges_sha256") != plan.get("spawn_edges_sha256")
        or not _is_sha256(run.get("unrelated_inventory_sha256"))
        or not _is_sha256(run.get("unrelated_archive_state_sha256"))
    ):
        raise UnsafeArchiveState("archive run metadata does not match its plan")
    journal = Journal(
        run_directory / JOURNAL_FILENAME,
        recover_torn_tail=recover_torn_tail,
    )
    return run, plan, journal


def _target_paths(plan: Dict[str, Any]) -> set:
    return {
        path
        for target in plan["targets"]
        for path in (target["relative_path"], target["archived_relative_path"])
    }


def _verify_unrelated_baseline(
    run: Dict[str, Any],
    plan: Dict[str, Any],
    codex_home: Path,
    completed: Optional[set] = None,
    incomplete: Optional[set] = None,
) -> Tuple[
    Dict[str, FileFact], Dict[str, Dict[str, Any]], List[Tuple[str, str]]
]:
    facts, states, spawn_edges = _read_inventory_and_states(plan, codex_home)
    if not _unrelated_baseline_holds(run, plan, facts, states, spawn_edges):
        raise UnsafeArchiveState("unrelated Codex history changed during the run")
    _assert_target_expectations(
        plan,
        codex_home,
        facts,
        states,
        completed or set(),
        incomplete or set(),
    )
    return facts, states, spawn_edges


def _read_inventory_and_states(
    plan: Dict[str, Any], codex_home: Path
) -> Tuple[
    Dict[str, FileFact], Dict[str, Dict[str, Any]], List[Tuple[str, str]]
]:
    facts, anomalies = inventory(codex_home)
    if anomalies:
        raise UnsafeArchiveState("filesystem inventory contains unsafe entries")
    database_path = _database_path(codex_home, plan["database"])
    states = _read_archive_states_safely(database_path)
    spawn_edges = _read_spawn_edges_safely(database_path)
    return facts, states, spawn_edges


def _unrelated_baseline_holds(
    run: Dict[str, Any],
    plan: Dict[str, Any],
    facts: Dict[str, FileFact],
    states: Dict[str, Dict[str, Any]],
    spawn_edges: Sequence[Tuple[str, str]],
) -> bool:
    return (
        _inventory_digest(facts, _target_paths(plan))
        == run.get("unrelated_inventory_sha256")
        and _states_digest(states, {target["id"] for target in plan["targets"]})
        == run.get("unrelated_archive_state_sha256")
        and _spawn_edges_sha256(spawn_edges) == run.get("spawn_edges_sha256")
    )


def _mapping_change_counts(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, int]:
    before_keys = set(before)
    after_keys = set(after)
    shared = before_keys & after_keys
    return {
        "added": len(after_keys - before_keys),
        "removed": len(before_keys - after_keys),
        "changed": sum(before[key] != after[key] for key in shared),
    }


def _unrelated_change_details(
    before_facts: Dict[str, FileFact],
    after_facts: Dict[str, FileFact],
    before_states: Dict[str, Dict[str, Any]],
    after_states: Dict[str, Dict[str, Any]],
    before_spawn_edges: Sequence[Tuple[str, str]],
    after_spawn_edges: Sequence[Tuple[str, str]],
    target_postcondition_verified: bool,
) -> Dict[str, Any]:
    return {
        "rollout_files": _mapping_change_counts(before_facts, after_facts),
        "catalog_rows": _mapping_change_counts(before_states, after_states),
        "spawn_relationships_changed": sorted(before_spawn_edges)
        != sorted(after_spawn_edges),
        "target_postcondition_verified": target_postcondition_verified,
    }


def _format_unrelated_change(details: Dict[str, Any]) -> str:
    files = details["rollout_files"]
    rows = details["catalog_rows"]
    return (
        "rollout files added=%s removed=%s changed=%s; "
        "catalog rows added=%s removed=%s changed=%s; "
        "spawn relationships changed=%s; target archived correctly=%s"
        % (
            files["added"],
            files["removed"],
            files["changed"],
            rows["added"],
            rows["removed"],
            rows["changed"],
            str(details["spawn_relationships_changed"]).lower(),
            str(details["target_postcondition_verified"]).lower(),
        )
    )


def _precondition_holds(
    target: Dict[str, Any], codex_home: Path, states: Dict[str, Dict[str, Any]]
) -> bool:
    active = _safe_rollout_path(codex_home, target["relative_path"], "sessions")
    archived = _safe_rollout_path(
        codex_home, target["archived_relative_path"], "archived_sessions"
    )
    if not active.is_file() or active.is_symlink() or archived.exists() or archived.is_symlink():
        return False
    try:
        snapshot = _file_snapshot(active)
    except UnsafeArchiveState:
        return False
    state = states.get(target["id"])
    return (
        snapshot["size"] == target["file_size"]
        and snapshot["mtime_ns"] == target["mtime_ns"]
        and snapshot["inode"] == target["inode"]
        and snapshot["sha256"] == target["sha256"]
        and state is not None
        and not state["archived"]
        and _path_matches(state["rollout_path"], active, codex_home)
        and state.get("metadata_sha256")
        == target["database_metadata_sha256"]
    )


def _postcondition_holds(
    target: Dict[str, Any], codex_home: Path, states: Dict[str, Dict[str, Any]]
) -> bool:
    active = _safe_rollout_path(codex_home, target["relative_path"], "sessions")
    archived = _safe_rollout_path(
        codex_home, target["archived_relative_path"], "archived_sessions"
    )
    if active.exists() or active.is_symlink() or not archived.is_file() or archived.is_symlink():
        return False
    try:
        snapshot = _file_snapshot(archived)
    except UnsafeArchiveState:
        return False
    state = states.get(target["id"])
    return (
        snapshot["size"] == target["file_size"]
        and snapshot["sha256"] == target["sha256"]
        and state is not None
        and state["archived"]
        and _path_matches(state["rollout_path"], archived, codex_home)
    )


def _metadata_precondition_holds(
    target: Dict[str, Any],
    codex_home: Path,
    facts: Dict[str, FileFact],
    states: Dict[str, Dict[str, Any]],
) -> bool:
    active_relative = target["relative_path"]
    archived_relative = target["archived_relative_path"]
    fact = facts.get(active_relative)
    state = states.get(target["id"])
    active = codex_home / active_relative
    return (
        fact is not None
        and archived_relative not in facts
        and fact.size == target["file_size"]
        and fact.mtime_ns == target["mtime_ns"]
        and fact.inode == target["inode"]
        and state is not None
        and not state["archived"]
        and _path_matches(state["rollout_path"], active, codex_home)
        and state.get("metadata_sha256")
        == target["database_metadata_sha256"]
    )


def _metadata_postcondition_holds(
    target: Dict[str, Any],
    codex_home: Path,
    facts: Dict[str, FileFact],
    states: Dict[str, Dict[str, Any]],
) -> bool:
    active_relative = target["relative_path"]
    archived_relative = target["archived_relative_path"]
    fact = facts.get(archived_relative)
    state = states.get(target["id"])
    archived = codex_home / archived_relative
    return (
        active_relative not in facts
        and fact is not None
        and fact.size == target["file_size"]
        and fact.mtime_ns == target["mtime_ns"]
        and fact.inode == target["inode"]
        and state is not None
        and state["archived"]
        and _path_matches(state["rollout_path"], archived, codex_home)
    )


def _assert_target_expectations(
    plan: Dict[str, Any],
    codex_home: Path,
    facts: Dict[str, FileFact],
    states: Dict[str, Dict[str, Any]],
    completed: set,
    incomplete: set,
) -> None:
    if completed & incomplete:
        raise UnsafeArchiveState("archive journal target state is contradictory")
    for target in plan["targets"]:
        thread_id = target["id"]
        if thread_id in completed:
            valid = _metadata_postcondition_holds(
                target, codex_home, facts, states
            )
            expectation = "archived"
        elif thread_id in incomplete:
            valid = _metadata_precondition_holds(
                target, codex_home, facts, states
            ) or _metadata_postcondition_holds(target, codex_home, facts, states)
            expectation = "active or archived after an interrupted attempt"
        else:
            valid = _metadata_precondition_holds(
                target, codex_home, facts, states
            )
            expectation = "active and unchanged"
        if not valid:
            raise UnsafeArchiveState(
                "target %s is no longer %s" % (thread_id, expectation)
            )


def _default_command_runner(
    codex_binary: str, thread_id: str, codex_home: Path, timeout: float
) -> CommandOutcome:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    if threading.current_thread() is not threading.main_thread():
        raise UnsafeArchiveState(
            "the Codex command runner must execute on the main thread"
        )
    previous_handlers: Dict[int, Any] = {}

    def interrupted_by_termination(_signum, _frame):
        raise KeyboardInterrupt()

    for signum in (
        signal.SIGINT,
        signal.SIGHUP,
        signal.SIGTERM,
        signal.SIGQUIT,
        signal.SIGTSTP,
    ):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, interrupted_by_termination)

    process: Optional[subprocess.Popen] = None

    def terminate_and_reap() -> Tuple[bytes, bytes]:
        if process is None:
            return b"", b""
        # Do not let a repeated terminal signal interrupt cleanup halfway.
        for signum in previous_handlers:
            signal.signal(signum, signal.SIG_IGN)
        try:
            # start_new_session makes the leader PID the process-group ID.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return process.communicate()

    try:
        process = subprocess.Popen(
            [codex_binary, "archive", thread_id],
            cwd=str(codex_home),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            stdout, stderr = terminate_and_reap()
            return CommandOutcome(
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
            )
        except BaseException:
            terminate_and_reap()
            raise
        return CommandOutcome(
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def resolve_codex_binary(value: str) -> str:
    if os.sep in value or (os.altsep and os.altsep in value):
        candidate = Path(value).expanduser().resolve()
        resolved = str(candidate)
    else:
        resolved = shutil.which(value) or ""
    if not resolved or not Path(resolved).is_file() or not os.access(resolved, os.X_OK):
        raise UnsafeArchiveState("could not find an executable Codex CLI")
    return resolved


def _completed_and_incomplete(journal: Journal) -> Tuple[set, set]:
    completed = set()
    open_attempts = set()
    for entry in journal.entries:
        thread_id = entry.get("thread_id")
        if entry.get("event") == "attempt_started" and isinstance(thread_id, str):
            open_attempts.add(thread_id)
        elif entry.get("event") == "attempt_result" and isinstance(thread_id, str):
            open_attempts.discard(thread_id)
            if entry.get("status") in SUCCESS_STATUSES:
                completed.add(thread_id)
    return completed, open_attempts


def _write_result(run_directory: Path, value: Dict[str, Any]) -> None:
    path = run_directory / RESULT_FILENAME
    _replace_json(path, value)
    _fsync_directory(run_directory)


def _result_payload(
    plan: Dict[str, Any],
    journal: Journal,
    status: str,
    failure_code: Optional[str] = None,
    failure_details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    completed, open_attempts = _completed_and_incomplete(journal)
    payload = {
        "schema_version": RUN_SCHEMA_VERSION,
        "plan_id": plan["plan_id"],
        "status": status,
        "finished_at": utc_now(),
        "target_count": len(plan["targets"]),
        "verified_success_count": len(completed),
        "incomplete_attempt_count": len(open_attempts),
        "journal_tail_sha256": journal.previous_sha256,
    }
    if failure_code:
        payload["failure_code"] = failure_code
    if failure_details:
        payload["failure_details"] = failure_details
    return payload


def _open_run_lock(run_directory: Path):
    if not hasattr(os, "O_NOFOLLOW"):
        raise UnsafeArchiveState("this platform cannot safely create a run lock")
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = -1
    try:
        descriptor = os.open(run_directory / RUN_LOCK, flags, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UnsafeArchiveState("archive run lock is not a safe regular file")
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        descriptor = -1
        return handle
    except OSError as exc:
        raise UnsafeArchiveState("could not safely open the archive run lock") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def execute_archive_run(
    run_directory: Path,
    codex_home: Path,
    codex_binary: str = "codex",
    timeout: float = 60.0,
    command_runner: Optional[CommandRunner] = None,
    progress: Optional[ProgressReporter] = None,
    resume: bool = False,
) -> Dict[str, Any]:
    if timeout <= 0:
        raise UnsafeArchiveState("Codex command timeout must be positive")
    run_directory = _safe_existing_directory(
        run_directory, "archive run directory"
    )
    lock_handle = _open_run_lock(run_directory)
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lock_handle.close()
        raise UnsafeArchiveState("another process is using this archive run") from exc

    try:
        existing_result = None
        if (run_directory / RESULT_FILENAME).exists():
            existing_result = _load_json(
                run_directory / RESULT_FILENAME, "archive result"
            )
            if (
                not isinstance(existing_result, dict)
                or existing_result.get("status")
                not in {"complete", "interrupted", "stopped"}
            ):
                raise UnsafeArchiveState("archive result is invalid")
            if resume and existing_result.get("status") not in {
                "complete",
                "interrupted",
            }:
                raise UnsafeArchiveState(
                    "this stopped archive run is not safely resumable"
                )
        run, plan, journal = _load_run(
            run_directory,
            recover_torn_tail=(
                resume
                and (
                    existing_result is None
                    or existing_result.get("status") == "interrupted"
                )
            ),
        )
        codex_home = codex_home.expanduser().resolve()
        if codex_home_fingerprint(codex_home) != run.get("codex_home_sha256"):
            raise UnsafeArchiveState("archive run belongs to a different CODEX_HOME")
        if existing_result is not None and (
            existing_result.get("schema_version") != RUN_SCHEMA_VERSION
            or existing_result.get("plan_id") != plan["plan_id"]
            or existing_result.get("target_count") != len(plan["targets"])
        ):
            raise UnsafeArchiveState("archive result does not match its run")
        if resume is False and journal.entries:
            raise UnsafeArchiveState(
                "archive run has already started; use archive resume"
            )

        runner = command_runner or _default_command_runner
    except BaseException:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()
        raise

    try:
        completed, incomplete = _completed_and_incomplete(journal)
        if existing_result and existing_result.get("status") == "complete":
            if (
                completed != {target["id"] for target in plan["targets"]}
                or incomplete
                or existing_result.get("verified_success_count")
                != len(plan["targets"])
                or existing_result.get("incomplete_attempt_count") != 0
                or existing_result.get("journal_tail_sha256")
                != journal.previous_sha256
            ):
                raise UnsafeArchiveState(
                    "completed archive result does not match its journal"
                )
            complete_path = run_directory / RUN_COMPLETE
            if complete_path.exists() or complete_path.is_symlink():
                _regular_file(complete_path, "archive run completion marker")
            else:
                _write_bytes(complete_path, b"complete\n")
                _fsync_directory(run_directory)
            return existing_result

        binary = codex_binary if command_runner else resolve_codex_binary(
            codex_binary
        )

        if not journal.entries:
            journal.append("run_started", plan_id=plan["plan_id"])
        if journal.recovered_tail is not None:
            journal.append(
                "journal_tail_recovered",
                discarded_size=journal.recovered_tail["size"],
                discarded_sha256=journal.recovered_tail["sha256"],
            )

        try:
            facts, states, spawn_edges = _verify_unrelated_baseline(
                run,
                plan,
                codex_home,
                completed=completed,
                incomplete=incomplete,
            )
            total_targets = len(plan["targets"])
            for target_index, target in enumerate(plan["targets"], start=1):
                thread_id = target["id"]
                if thread_id in completed:
                    if not _postcondition_holds(target, codex_home, states):
                        raise UnsafeArchiveState(
                            "a previously completed target no longer satisfies its postcondition"
                        )
                    continue

                if thread_id in incomplete:
                    if _postcondition_holds(target, codex_home, states):
                        journal.append(
                            "attempt_result",
                            thread_id=thread_id,
                            status="recovered_verified_success",
                            cli_exit_code=None,
                            cli_timed_out=False,
                            stdout_size=0,
                            stdout_sha256=_sha256_bytes(b""),
                            stderr_size=0,
                            stderr_sha256=_sha256_bytes(b""),
                        )
                        completed.add(thread_id)
                        incomplete.discard(thread_id)
                        facts, states, spawn_edges = _verify_unrelated_baseline(
                            run,
                            plan,
                            codex_home,
                            completed=completed,
                            incomplete=incomplete,
                        )
                        if progress:
                            progress(
                                target_index,
                                total_targets,
                                thread_id,
                                "recovered_verified_success",
                            )
                        continue
                    if not _precondition_holds(target, codex_home, states):
                        raise UnsafeArchiveState(
                            "interrupted target is neither safely active nor safely archived"
                        )
                    # The prior attempt definitely made no durable state change.
                    journal.append(
                        "attempt_result",
                        thread_id=thread_id,
                        status="interrupted_no_state_change",
                        cli_exit_code=None,
                        cli_timed_out=False,
                        stdout_size=0,
                        stdout_sha256=_sha256_bytes(b""),
                        stderr_size=0,
                        stderr_sha256=_sha256_bytes(b""),
                    )
                    incomplete.discard(thread_id)

                facts, states, spawn_edges = _verify_unrelated_baseline(
                    run,
                    plan,
                    codex_home,
                    completed=completed,
                    incomplete=incomplete,
                )
                if not _precondition_holds(target, codex_home, states):
                    raise UnsafeArchiveState("target precondition changed before archive")
                before_facts = facts
                before_states = states
                journal.append("attempt_started", thread_id=thread_id)
                outcome = runner(binary, thread_id, codex_home, timeout)
                after_facts, after_states, after_spawn_edges = _read_inventory_and_states(
                    plan, codex_home
                )

                active_relative = target["relative_path"]
                archived_relative = target["archived_relative_path"]
                before_other_files = {
                    key: value
                    for key, value in before_facts.items()
                    if key not in {active_relative, archived_relative}
                }
                after_other_files = {
                    key: value
                    for key, value in after_facts.items()
                    if key not in {active_relative, archived_relative}
                }
                before_other_states = {
                    key: value
                    for key, value in before_states.items()
                    if key != thread_id
                }
                after_other_states = {
                    key: value for key, value in after_states.items() if key != thread_id
                }
                unrelated_changed = (
                    not _unrelated_baseline_holds(
                        run, plan, after_facts, after_states, after_spawn_edges
                    )
                    or
                    before_other_files != after_other_files
                    or before_other_states != after_other_states
                )
                postcondition = _postcondition_holds(
                    target, codex_home, after_states
                )
                failure_details = None
                if postcondition and not unrelated_changed:
                    if outcome.timed_out:
                        status_value = "verified_success_after_timeout"
                    elif outcome.exit_code == 0:
                        status_value = "verified_success"
                    else:
                        status_value = "verified_success_with_cli_error"
                elif unrelated_changed:
                    status_value = "unrelated_concurrent_change"
                    failure_details = _unrelated_change_details(
                        before_other_files,
                        after_other_files,
                        before_other_states,
                        after_other_states,
                        spawn_edges,
                        after_spawn_edges,
                        postcondition,
                    )
                elif outcome.exit_code == 0:
                    status_value = "cli_success_without_verified_postcondition"
                else:
                    status_value = "cli_failure_without_verified_postcondition"

                attempt_values = {
                    "thread_id": thread_id,
                    "status": status_value,
                    "cli_exit_code": outcome.exit_code,
                    "cli_timed_out": outcome.timed_out,
                    "stdout_size": len(outcome.stdout),
                    "stdout_sha256": _sha256_bytes(outcome.stdout),
                    "stderr_size": len(outcome.stderr),
                    "stderr_sha256": _sha256_bytes(outcome.stderr),
                }
                if failure_details:
                    attempt_values["failure_details"] = failure_details
                journal.append(
                    "attempt_result",
                    **attempt_values,
                )
                if status_value not in SUCCESS_STATUSES:
                    result = _result_payload(
                        plan,
                        journal,
                        "stopped",
                        failure_code=status_value,
                        failure_details=failure_details,
                    )
                    _write_result(run_directory, result)
                    explanation = ""
                    if failure_details:
                        explanation = "; " + _format_unrelated_change(
                            failure_details
                        )
                    raise ArchiveOperationStopped(
                        "archive run stopped at target %s/%s after %s verified successes: %s%s"
                        % (
                            target_index,
                            total_targets,
                            len(completed),
                            status_value,
                            explanation,
                        )
                    )
                completed.add(thread_id)
                facts, states, spawn_edges = _verify_unrelated_baseline(
                    run,
                    plan,
                    codex_home,
                    completed=completed,
                    incomplete=incomplete,
                )
                if progress:
                    progress(
                        target_index,
                        total_targets,
                        thread_id,
                        status_value,
                    )

            _final_facts, final_states, _final_spawn_edges = _verify_unrelated_baseline(
                run,
                plan,
                codex_home,
                completed=completed,
                incomplete=incomplete,
            )
            for target in plan["targets"]:
                if not _postcondition_holds(target, codex_home, final_states):
                    raise UnsafeArchiveState(
                        "final byte-level verification failed for a completed target"
                    )
            _verify_unrelated_baseline(
                run,
                plan,
                codex_home,
                completed=completed,
                incomplete=incomplete,
            )
            result = _result_payload(plan, journal, "complete")
            _write_result(run_directory, result)
            complete_path = run_directory / RUN_COMPLETE
            if not complete_path.exists():
                _write_bytes(complete_path, b"complete\n")
            _fsync_directory(run_directory)
            return result
        except ArchiveOperationStopped:
            raise
        except UnsafeArchiveState:
            _write_result(
                run_directory,
                _result_payload(plan, journal, "stopped", failure_code="unsafe_state"),
            )
            raise
        except KeyboardInterrupt:
            _write_result(run_directory, _result_payload(plan, journal, "interrupted"))
            raise
        except Exception:
            _write_result(run_directory, _result_payload(plan, journal, "interrupted"))
            raise
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_handle.close()
