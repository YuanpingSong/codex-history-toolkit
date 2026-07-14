"""Streaming, read-only inventory of Codex rollout JSONL files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .classifier import (
    cli_version_for_report,
    classify_origin,
    filename_thread_id,
    is_user_message_event,
    is_valid_thread_id,
    model_provider_for_report,
    normalize_source,
    originator_for_report,
    source_for_report,
    thread_id_for_report,
    thread_source_for_report,
    timestamp_for_report,
)


@dataclass(frozen=True)
class FileFact:
    relative_path: str
    size: int
    mtime_ns: int
    inode: int


def _iter_jsonl_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames if not (Path(directory) / name).is_symlink()
        )
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if filename.endswith(".jsonl") and not path.is_symlink():
                yield path


def inventory(codex_home: Path) -> Tuple[Dict[str, FileFact], List[Dict[str, str]]]:
    facts: Dict[str, FileFact] = {}
    skipped: List[Dict[str, str]] = []
    for folder in ("sessions", "archived_sessions"):
        root = codex_home / folder
        if not root.exists():
            continue
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            safe_dirnames = []
            for name in sorted(dirnames):
                candidate = Path(directory) / name
                if candidate.is_symlink():
                    skipped.append(
                        {"code": "SYMLINK_SKIPPED", "path": candidate.relative_to(codex_home).as_posix()}
                    )
                else:
                    safe_dirnames.append(name)
            dirnames[:] = safe_dirnames
            for filename in sorted(filenames):
                if not filename.endswith(".jsonl"):
                    continue
                path = Path(directory) / filename
                relative = path.relative_to(codex_home).as_posix()
                if path.is_symlink():
                    skipped.append({"code": "SYMLINK_SKIPPED", "path": relative})
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    skipped.append({"code": "UNREADABLE_FILE", "path": relative})
                    continue
                facts[relative] = FileFact(relative, stat.st_size, stat.st_mtime_ns, stat.st_ino)
    return facts, skipped


def _safe_json(line: bytes) -> Optional[Dict[str, Any]]:
    value = json.loads(line.decode("utf-8"))
    return value if isinstance(value, dict) else None


def inspect_rollout(
    codex_home: Path,
    fact: FileFact,
    automated_originators: Iterable[str] = (),
) -> Dict[str, Any]:
    path = codex_home / fact.relative_path
    storage_state = "archived" if fact.relative_path.startswith("archived_sessions/") else "active"
    anomalies: List[str] = []
    session_meta: Optional[Dict[str, Any]] = None
    user_event_count = 0
    parsed_line_count = 0
    parse_error_count = 0
    scan_complete = True

    try:
        with path.open("rb") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = _safe_json(line)
                    parsed_line_count += 1
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parse_error_count += 1
                    anomalies.append("INVALID_JSON_LINE")
                    continue

                if session_meta is None:
                    if not record or record.get("type") != "session_meta":
                        anomalies.append("MISSING_OR_INVALID_SESSION_META")
                        break
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        anomalies.append("MISSING_OR_INVALID_SESSION_META")
                        break
                    session_meta = payload
                    origin = classify_origin(session_meta, automated_originators)
                    if origin.origin_class != "interactive":
                        scan_complete = False
                        break
                    continue

                if is_user_message_event(record):
                    user_event_count += 1
                    scan_complete = False
                    break
    except OSError:
        anomalies.append("UNREADABLE_FILE")

    try:
        after = path.stat()
        file_stable = (
            after.st_size == fact.size
            and after.st_mtime_ns == fact.mtime_ns
            and after.st_ino == fact.inode
        )
    except OSError:
        file_stable = False
    if not file_stable:
        anomalies.append("FILE_CHANGED_DURING_SCAN")

    filename_id = filename_thread_id(path.name)
    if session_meta is None:
        origin = None
        raw_thread_id = filename_id
        activity_state = "unknown"
        if "MISSING_OR_INVALID_SESSION_META" not in anomalies:
            anomalies.append("MISSING_OR_INVALID_SESSION_META")
        source_description = "unknown"
        source_raw = None
    else:
        origin = classify_origin(session_meta, automated_originators)
        raw_id = session_meta.get("id")
        raw_thread_id = str(raw_id) if raw_id is not None else filename_id
        source_description = source_for_report(session_meta.get("source"))
        source_raw = session_meta.get("source")

        if origin.rule_code == "AMBIGUOUS_UNKNOWN_SOURCE_OBJECT":
            anomalies.append("UNKNOWN_SOURCE_OBJECT_SHAPE")
        elif origin.origin_class == "ambiguous":
            anomalies.append("UNKNOWN_ORIGINATOR_SOURCE")

        if origin.origin_class == "interactive":
            if not file_stable or parse_error_count:
                activity_state = "unknown"
            elif user_event_count:
                activity_state = "meaningful"
            elif scan_complete:
                activity_state = "empty_shell"
            else:
                activity_state = "unknown"
        else:
            activity_state = "not_evaluated"

        if filename_id and raw_thread_id and filename_id != raw_thread_id:
            anomalies.append("FILENAME_METADATA_ID_MISMATCH")

    if raw_thread_id and not is_valid_thread_id(raw_thread_id):
        anomalies.append("INVALID_UUID_SHAPE")

    raw_parent_thread_id = session_meta.get("parent_thread_id") if session_meta else None
    parent_thread_id = thread_id_for_report(raw_parent_thread_id)
    if raw_parent_thread_id is not None and parent_thread_id and parent_thread_id.startswith("invalid:"):
        anomalies.append("INVALID_PARENT_THREAD_ID")

    raw_timestamp = session_meta.get("timestamp") if session_meta else None
    created_at = timestamp_for_report(raw_timestamp)
    if raw_timestamp is not None and created_at is None:
        anomalies.append("INVALID_SESSION_TIMESTAMP")

    thread_id = thread_id_for_report(raw_thread_id)

    source_normalized = normalize_source(session_meta.get("source")) if session_meta else {
        "type": "unknown",
        "kind": "unknown",
    }

    return {
        "record_type": "rollout",
        "id": thread_id,
        "_raw_id": raw_thread_id,
        "filename_id": filename_id,
        "relative_path": fact.relative_path,
        "storage_state": storage_state,
        "file_size": fact.size,
        "mtime_ns": fact.mtime_ns,
        "file_stable": file_stable,
        "scan_complete": scan_complete,
        "parsed_line_count": parsed_line_count,
        "parse_error_count": parse_error_count,
        "user_event_count": user_event_count,
        "activity_state": activity_state,
        "origin_class": origin.origin_class if origin else "ambiguous",
        "surface": origin.surface if origin else "unknown",
        "rule_code": origin.rule_code if origin else "AMBIGUOUS_MISSING_SESSION_META",
        "confidence": origin.confidence if origin else "low",
        "originator": originator_for_report(
            session_meta.get("originator"), automated_originators
        )
        if session_meta
        else "unknown",
        "source": source_description,
        "source_type": source_normalized["type"],
        "source_kind": source_normalized["kind"],
        "_source_raw": source_raw,
        "thread_source": thread_source_for_report(session_meta.get("thread_source"))
        if session_meta
        else None,
        "parent_thread_id": parent_thread_id,
        "model_provider": model_provider_for_report(session_meta.get("model_provider"))
        if session_meta
        else None,
        "created_at": created_at,
        "cli_version": cli_version_for_report(session_meta.get("cli_version"))
        if session_meta
        else None,
        "anomalies": sorted(set(anomalies)),
    }
