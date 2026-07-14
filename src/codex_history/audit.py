"""Audit orchestration and database/filesystem reconciliation."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import json
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import __version__
from .catalog import (
    CatalogError,
    find_state_database,
    read_catalog,
    read_catalog_signature,
)
from .classifier import (
    RULESET_VERSION,
    cli_version_for_report,
    model_provider_for_report,
    normalize_automated_originators,
    normalize_source,
    parse_database_source,
    source_for_report,
    thread_id_for_report,
    thread_source_for_report,
)
from .rollouts import FileFact, inspect_rollout, inventory


SCHEMA_VERSION = "1.1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalized_path(path: str) -> str:
    return os.path.normpath(path)


def _display_path(path: Path) -> str:
    try:
        relative = path.resolve(strict=False).relative_to(Path.home().resolve(strict=False))
        return (Path("~") / relative).as_posix()
    except (OSError, ValueError):
        return path.name


def _db_row_for_report(row: Dict[str, Any], codex_home: Path) -> Dict[str, Any]:
    rollout_path = str(row.get("rollout_path") or "")
    relative_path = None
    try:
        relative_path = Path(rollout_path).resolve(strict=False).relative_to(
            codex_home.resolve(strict=False)
        ).as_posix()
    except (OSError, ValueError):
        pass

    result = {
        "present": True,
        "archived": bool(row.get("archived")),
        "relative_rollout_path": relative_path,
        "source": source_for_report(parse_database_source(row.get("source"))),
        "model_provider": model_provider_for_report(row.get("model_provider")),
        "thread_source": thread_source_for_report(row.get("thread_source")),
        "has_user_message": (
            bool(row.get("db_has_user_message"))
            if row.get("db_has_user_message") is not None
            else None
        ),
        "preview_length": row.get("preview_length"),
    }
    if "title" in row:
        result["title"] = row.get("title")
    if "cwd" in row:
        result["cwd"] = row.get("cwd")
    return result


def _attach_catalog(
    records: List[Dict[str, Any]], db_rows: List[Dict[str, Any]], codex_home: Path
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    by_id: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("_raw_id"):
            by_id[str(record["_raw_id"])].append(record)

    duplicates = {thread_id for thread_id, values in by_id.items() if len(values) > 1}
    for thread_id in duplicates:
        for record in by_id[thread_id]:
            record["anomalies"].append("DUPLICATE_METADATA_ID")

    database_only: List[Dict[str, Any]] = []
    seen_db_ids = set()
    for row in db_rows:
        raw_thread_id = str(row.get("id"))
        report_thread_id = thread_id_for_report(raw_thread_id)
        seen_db_ids.add(raw_thread_id)
        matched = by_id.get(raw_thread_id, [])
        db_report = _db_row_for_report(row, codex_home)
        if not matched:
            parsed_source = parse_database_source(row.get("source"))
            normalized_source = normalize_source(parsed_source)
            database_only.append(
                {
                    "record_type": "database_only",
                    "id": report_thread_id,
                    "_raw_id": raw_thread_id,
                    "relative_path": db_report.get("relative_rollout_path"),
                    "storage_state": "archived" if db_report["archived"] else "active",
                    "origin_class": "ambiguous",
                    "surface": "unknown",
                    "activity_state": "unknown",
                    "rule_code": "AMBIGUOUS_DATABASE_ONLY",
                    "confidence": "low",
                    "file_size": None,
                    "file_stable": None,
                    "originator": "unknown",
                    "source": source_for_report(parsed_source),
                    "source_type": "database",
                    "source_kind": normalized_source["kind"],
                    "thread_source": thread_source_for_report(row.get("thread_source")),
                    "parent_thread_id": None,
                    "model_provider": model_provider_for_report(row.get("model_provider")),
                    "created_at": None,
                    "cli_version": cli_version_for_report(row.get("cli_version")),
                    "database": db_report,
                    "anomalies": ["DATABASE_ROW_MISSING_ROLLOUT"],
                }
            )
            continue

        for record in matched:
            record["database"] = db_report
            if bool(row.get("archived")) != (record["storage_state"] == "archived"):
                record["anomalies"].append("DATABASE_ARCHIVE_PATH_MISMATCH")

            db_relative = db_report.get("relative_rollout_path")
            if db_relative and _normalized_path(db_relative) != _normalized_path(record["relative_path"]):
                record["anomalies"].append("ROLLOUT_PATH_MISMATCH")

            source_raw = record.get("_source_raw")
            database_source = parse_database_source(row.get("source"))
            if source_raw is not None and database_source != source_raw:
                record["anomalies"].append("DATABASE_METADATA_SOURCE_MISMATCH")

            db_has_user = db_report.get("has_user_message")
            if record["origin_class"] == "interactive" and db_has_user is not None:
                event_has_user = record["activity_state"] == "meaningful"
                if record["activity_state"] in {"meaningful", "empty_shell"} and event_has_user != db_has_user:
                    record["activity_state"] = "unknown"
                    record["anomalies"].append("USER_SIGNAL_MISMATCH")

    for record in records:
        if record.get("_raw_id") not in seen_db_ids:
            record["database"] = {"present": False}
            record["anomalies"].append("ROLLOUT_MISSING_DATABASE_ROW")
        record["anomalies"] = sorted(set(record["anomalies"]))

    return records, database_only


def _check_spawn_edges(
    records: List[Dict[str, Any]], edges: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    anomalies: List[Dict[str, Any]] = []
    record_ids = {record.get("_raw_id") for record in records if record.get("_raw_id")}
    edge_children = {edge.get("child_thread_id") for edge in edges}
    expected_children = {
        record.get("_raw_id")
        for record in records
        if record.get("source_kind") == "subagent_thread_spawn" and record.get("_raw_id")
    }

    for child in sorted(expected_children - edge_children):
        anomalies.append(
            {"code": "SPAWN_EDGE_MISSING", "thread_id": thread_id_for_report(child)}
        )
    for edge in edges:
        parent = edge.get("parent_thread_id")
        child = edge.get("child_thread_id")
        if parent not in record_ids:
            anomalies.append(
                {"code": "SPAWN_PARENT_MISSING", "thread_id": thread_id_for_report(parent)}
            )
        if child not in record_ids:
            anomalies.append(
                {"code": "SPAWN_CHILD_MISSING", "thread_id": thread_id_for_report(child)}
            )
    return anomalies


def _summary(records: List[Dict[str, Any]], db_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    rollout_records = [record for record in records if record["record_type"] == "rollout"]
    by_origin = Counter(record["origin_class"] for record in records)
    by_surface = Counter(record["surface"] for record in records)
    by_activity = Counter(record["activity_state"] for record in records)
    by_storage = Counter(record["storage_state"] for record in records)
    bytes_by_origin = Counter()
    anomaly_counts = Counter()
    by_rule = Counter()
    by_origin_storage = Counter()
    interactive_activity_by_surface: Dict[str, Counter] = defaultdict(Counter)
    for record in records:
        by_rule[record["rule_code"]] += 1
        by_origin_storage[(record["origin_class"], record["storage_state"])] += 1
        if record["origin_class"] == "interactive":
            interactive_activity_by_surface[record["surface"]][record["activity_state"]] += 1
        if record["record_type"] == "rollout":
            bytes_by_origin[record["origin_class"]] += int(record.get("file_size") or 0)
        for code in record.get("anomalies", []):
            anomaly_counts[code] += 1

    unique_ids = {record.get("id") for record in records if record.get("id")}
    return {
        "rollout_files": len(rollout_records),
        "database_rows": len(db_rows),
        "unique_thread_ids": len(unique_ids),
        "by_origin_class": dict(sorted(by_origin.items())),
        "by_surface": dict(sorted(by_surface.items())),
        "by_activity_state": dict(sorted(by_activity.items())),
        "by_storage_state": dict(sorted(by_storage.items())),
        "by_rule_code": dict(sorted(by_rule.items())),
        "by_origin_and_storage": {
            origin: {
                storage: by_origin_storage[(origin, storage)]
                for storage in ("active", "archived")
                if by_origin_storage[(origin, storage)]
            }
            for origin in sorted({key[0] for key in by_origin_storage})
        },
        "interactive_activity_by_surface": {
            surface: dict(sorted(counts.items()))
            for surface, counts in sorted(interactive_activity_by_surface.items())
        },
        "bytes_by_origin_class": dict(sorted(bytes_by_origin.items())),
        "anomaly_counts": dict(sorted(anomaly_counts.items())),
    }


def _deduplicate_anomalies(anomalies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    unique: Dict[str, Dict[str, Any]] = {}
    for anomaly in anomalies:
        key = json.dumps(anomaly, sort_keys=True, separators=(",", ":"), default=str)
        unique[key] = anomaly
    return [unique[key] for key in sorted(unique)]


def run_audit(
    codex_home: Path,
    include_titles: bool = False,
    include_cwd: bool = False,
    automated_originators: Iterable[str] = (),
) -> Dict[str, Any]:
    configured_automation = normalize_automated_originators(automated_originators)
    started_at = _utc_now()
    start_inventory, inventory_anomalies = inventory(codex_home)

    database_path, database_candidates = find_state_database(codex_home)
    database_error: Optional[str] = None
    db_data: Dict[str, Any] = {
        "rows": [],
        "spawn_edges": [],
        "spawn_edges_available": False,
        "quick_check": [],
        "signature": None,
        "columns": [],
    }
    if database_path is None:
        database_error = "No state_N.sqlite database containing a threads table was found."
    else:
        try:
            db_data = read_catalog(database_path, include_titles, include_cwd)
        except CatalogError as exc:
            database_error = str(exc).replace(str(codex_home), _display_path(codex_home))

    records = []
    for relative_path in sorted(start_inventory):
        records.append(
            inspect_rollout(
                codex_home,
                start_inventory[relative_path],
                configured_automation,
            )
        )

    if database_error is None:
        records, database_only = _attach_catalog(records, db_data["rows"], codex_home)
        records.extend(database_only)
    else:
        for record in records:
            record["database"] = {"present": None}

    structural_anomalies = list(inventory_anomalies)
    if db_data.get("spawn_edges_available"):
        structural_anomalies.extend(_check_spawn_edges(records, db_data["spawn_edges"]))
    if db_data["quick_check"] and db_data["quick_check"] != ["ok"]:
        structural_anomalies.append(
            {"code": "DATABASE_INTEGRITY_FAILURE", "details": db_data["quick_check"]}
        )
    if database_error:
        structural_anomalies.append({"code": "DATABASE_READ_FAILURE", "details": database_error})

    end_inventory, end_inventory_anomalies = inventory(codex_home)
    structural_anomalies.extend(end_inventory_anomalies)
    start_paths = set(start_inventory)
    end_paths = set(end_inventory)
    for relative_path in sorted(end_paths - start_paths):
        structural_anomalies.append({"code": "FILE_ADDED_DURING_SCAN", "path": relative_path})
    for relative_path in sorted(start_paths - end_paths):
        structural_anomalies.append({"code": "FILE_REMOVED_DURING_SCAN", "path": relative_path})

    inventory_changed = start_inventory != end_inventory
    end_database_path, end_database_candidates = find_state_database(codex_home)
    start_database_identity = database_path.resolve() if database_path else None
    end_database_identity = end_database_path.resolve() if end_database_path else None
    database_selection_changed = (
        start_database_identity != end_database_identity
        or database_candidates != end_database_candidates
    )
    database_changed = database_selection_changed
    end_db_signature = None
    if database_selection_changed:
        structural_anomalies.append(
            {
                "code": "STATE_DATABASE_SELECTION_CHANGED",
                "database_start": database_path.name if database_path else None,
                "database_end": end_database_path.name if end_database_path else None,
            }
        )
    if (
        database_path
        and end_database_path
        and start_database_identity == end_database_identity
        and database_error is None
    ):
        try:
            end_db_signature = read_catalog_signature(
                end_database_path, include_titles, include_cwd
            )
            database_changed = database_changed or end_db_signature != db_data["signature"]
        except CatalogError as exc:
            database_changed = True
            structural_anomalies.append(
                {
                    "code": "DATABASE_RECHECK_FAILURE",
                    "details": str(exc).replace(
                        str(codex_home), _display_path(codex_home)
                    ),
                }
            )
    if inventory_changed or database_changed:
        structural_anomalies.append(
            {
                "code": "CONCURRENT_HISTORY_CHANGE",
                "filesystem_changed": inventory_changed,
                "database_changed": database_changed,
            }
        )

    records.sort(key=lambda record: (str(record.get("id") or ""), record.get("relative_path") or ""))
    for record in records:
        for code in record.get("anomalies", []):
            structural_anomalies.append(
                {
                    "code": code,
                    "thread_id": record.get("id"),
                    "path": record.get("relative_path"),
                }
            )
        record.pop("_source_raw", None)
        record.pop("_raw_id", None)

    structural_anomalies = _deduplicate_anomalies(structural_anomalies)

    summary = _summary(records, db_data["rows"])
    db_signature = db_data.get("signature") or {}
    summary["database_active_rows"] = (
        db_signature.get("row_count", 0) - db_signature.get("archived_count", 0)
        if db_signature
        else None
    )
    summary["database_archived_rows"] = (
        db_signature.get("archived_count") if db_signature else None
    )
    summary["structural_anomaly_count"] = len(structural_anomalies)
    summary["stable"] = not inventory_changed and not database_changed and all(
        record.get("file_stable") is not False for record in records if record["record_type"] == "rollout"
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "codex-history-audit", "version": __version__, "ruleset_version": RULESET_VERSION},
        "classification": {
            "automated_originators": list(configured_automation),
        },
        "run": {
            "started_at": started_at,
            "finished_at": _utc_now(),
            "snapshot_model": "per-file metadata with live-change detection",
            "stable": summary["stable"],
        },
        "source": {
            "codex_home": _display_path(codex_home),
            "database": str(database_path.relative_to(codex_home)) if database_path else None,
            "database_candidates": database_candidates,
            "database_end": str(end_database_path.relative_to(codex_home))
            if end_database_path
            else None,
            "database_candidates_end": end_database_candidates,
            "database_error": database_error,
            "database_signature_start": db_data["signature"],
            "database_signature_end": end_db_signature,
            "database_columns": db_data["columns"],
            "database_quick_check": db_data["quick_check"],
            "spawn_edge_table_available": db_data["spawn_edges_available"],
            "spawn_edge_count": len(db_data["spawn_edges"]),
            "include_titles": include_titles,
            "include_cwd": include_cwd,
        },
        "summary": summary,
        "threads": records,
        "anomalies": structural_anomalies,
    }
