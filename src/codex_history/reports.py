"""Private, atomic report rendering."""

from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, Iterable, List, Optional


REPORT_FILES = ("audit.json", "threads.csv", "anomalies.json", "summary.txt")
PRIVATE_SENTINEL = ".codex-history-audit-private"


def default_output_path(root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return root / ("audit-%s" % stamp)


def _write_bytes(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def _write_json(path: Path, value: Any) -> None:
    _write_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, separators=(",", ":"), sort_keys=True)
    else:
        text = str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _render_csv(threads: List[Dict[str, Any]]) -> bytes:
    from io import StringIO

    columns = [
        "id",
        "storage_state",
        "origin_class",
        "surface",
        "activity_state",
        "rule_code",
        "confidence",
        "originator",
        "source",
        "thread_source",
        "parent_thread_id",
        "model_provider",
        "created_at",
        "file_size",
        "relative_path",
        "file_stable",
        "database_present",
        "database_archived",
        "anomalies",
    ]
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for thread in threads:
        database = thread.get("database") or {}
        row = {
            "id": thread.get("id"),
            "storage_state": thread.get("storage_state"),
            "origin_class": thread.get("origin_class"),
            "surface": thread.get("surface"),
            "activity_state": thread.get("activity_state"),
            "rule_code": thread.get("rule_code"),
            "confidence": thread.get("confidence"),
            "originator": thread.get("originator"),
            "source": thread.get("source"),
            "thread_source": thread.get("thread_source"),
            "parent_thread_id": thread.get("parent_thread_id"),
            "model_provider": thread.get("model_provider"),
            "created_at": thread.get("created_at"),
            "file_size": thread.get("file_size"),
            "relative_path": thread.get("relative_path"),
            "file_stable": thread.get("file_stable"),
            "database_present": database.get("present"),
            "database_archived": database.get("archived"),
            "anomalies": ";".join(thread.get("anomalies", [])),
        }
        writer.writerow({key: _csv_cell(value) for key, value in row.items()})
    return buffer.getvalue().encode("utf-8")


def _format_counts(title: str, values: Dict[str, Any]) -> List[str]:
    lines = [title]
    if not values:
        lines.append("  (none)")
    else:
        width = max(len(str(key)) for key in values)
        for key, value in sorted(values.items()):
            lines.append("  %-*s  %s" % (width, key, value))
    return lines


def render_summary(audit: Dict[str, Any], output_path: Optional[Path] = None) -> str:
    summary = audit["summary"]
    run = audit["run"]
    source = audit["source"]
    lines = [
        "Codex History Audit",
        "===================",
        "",
        "Stable snapshot: %s" % ("yes" if run["stable"] else "no"),
        "Started: %s" % run["started_at"],
        "Finished: %s" % run["finished_at"],
        "CODEX_HOME: %s" % source["codex_home"],
        "State database: %s" % (source["database"] or "not found"),
        "SQLite quick_check: %s" % (", ".join(source["database_quick_check"]) or "not run"),
        "",
        "Inventory",
        "  rollout files      %s" % summary["rollout_files"],
        "  database rows      %s" % summary["database_rows"],
        "  DB active rows     %s" % summary["database_active_rows"],
        "  DB archived rows   %s" % summary["database_archived_rows"],
        "  unique thread IDs  %s" % summary["unique_thread_ids"],
        "  spawn edges        %s"
        % (
            source["spawn_edge_count"]
            if source["spawn_edge_table_available"]
            else "table unavailable"
        ),
        "",
    ]
    lines.extend(_format_counts("Origin classes (all records)", summary["by_origin_class"]))
    lines.append("")
    lines.extend(_format_counts("Surfaces", summary["by_surface"]))
    lines.append("")
    lines.extend(_format_counts("Activity states", summary["by_activity_state"]))
    lines.append("")
    lines.append("Interactive activity by surface")
    for surface, counts in sorted(summary["interactive_activity_by_surface"].items()):
        detail = ", ".join("%s=%s" % item for item in sorted(counts.items()))
        lines.append("  %-12s  %s" % (surface, detail))
    lines.append("")
    lines.extend(_format_counts("Storage states", summary["by_storage_state"]))
    lines.append("")
    lines.extend(
        _format_counts("Rollout bytes by origin class", summary["bytes_by_origin_class"])
    )
    lines.append("")
    lines.extend(_format_counts("Thread anomaly counts", summary["anomaly_counts"]))
    lines.extend(
        [
            "",
            "Structural anomaly entries: %s" % summary["structural_anomaly_count"],
            "",
            "Privacy: prompts, assistant messages, tool outputs, titles, cwd paths, auth data,",
            "and account identifiers are excluded unless an explicit include option was used.",
        ]
    )
    if not run["stable"]:
        lines.extend(
            [
                "",
                "WARNING: history changed during the audit. Use this run for review only;",
                "rerun after ChatGPT and automated agents are fully stopped before archiving.",
            ]
        )
    if output_path:
        lines.extend(["", "Report directory: %s" % output_path])
    return "\n".join(lines) + "\n"


def publish_reports(audit: Dict[str, Any], output_path: Path) -> Dict[str, Any]:
    output_path = output_path.resolve()
    if output_path.exists():
        raise FileExistsError("output already exists: %s" % output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    staging = Path(
        tempfile.mkdtemp(prefix=".%s.tmp-" % output_path.name, dir=str(output_path.parent))
    )
    staging.chmod(0o700)
    try:
        _write_bytes(
            staging / PRIVATE_SENTINEL,
            (
                b"Private local Codex history metadata. "
                b"Do not commit, publish, or share this directory.\n"
            ),
        )
        _write_json(staging / "audit.json", audit)
        _write_bytes(staging / "threads.csv", _render_csv(audit["threads"]))
        _write_json(staging / "anomalies.json", audit["anomalies"])
        _write_bytes(staging / "summary.txt", render_summary(audit).encode("utf-8"))

        manifest_files = []
        for filename in REPORT_FILES:
            payload = (staging / filename).read_bytes()
            manifest_files.append(
                {
                    "name": filename,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        manifest = {
            "schema_version": "1.0",
            "complete": True,
            "files": manifest_files,
        }
        _write_json(staging / "manifest.json", manifest)
        _write_bytes(staging / "COMPLETE", b"complete\n")

        directory_fd = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        staging.rename(output_path)
        return manifest
    # Cleanup must also run for KeyboardInterrupt so partial private reports do
    # not remain beside the intended output directory.
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
