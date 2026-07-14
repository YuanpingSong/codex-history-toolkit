"""Read-only access to Codex's SQLite thread catalog."""

from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple


STATE_DB_RE = re.compile(r"state_(\d+)\.sqlite$")

KNOWN_THREAD_COLUMNS = {
    "archived",
    "archived_at",
    "cli_version",
    "created_at",
    "created_at_ms",
    "cwd",
    "first_user_message",
    "id",
    "model_provider",
    "preview",
    "rollout_path",
    "source",
    "thread_source",
    "title",
    "updated_at",
    "updated_at_ms",
}


class CatalogError(RuntimeError):
    pass


def _has_threads_table(path: Path) -> bool:
    uri = "%s?mode=ro" % path.resolve().as_uri()
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as connection:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'"
            ).fetchone()
            return row is not None
    except sqlite3.Error:
        return False


def find_state_database(codex_home: Path) -> Tuple[Optional[Path], List[str]]:
    candidates = []
    for path in codex_home.glob("state_*.sqlite"):
        match = STATE_DB_RE.search(path.name)
        if match:
            candidates.append((int(match.group(1)), path))
    candidates.sort(reverse=True)

    alternatives = [str(path.relative_to(codex_home)) for _, path in candidates]
    for _, path in candidates:
        if _has_threads_table(path):
            return path, alternatives
    return None, alternatives


def _table_columns(connection: sqlite3.Connection, table: str) -> Sequence[str]:
    return [row[1] for row in connection.execute("PRAGMA table_info(%s)" % table)]


def _encode_signature_row(row: Sequence[Any]) -> bytes:
    encoded = bytearray()
    for value in row:
        if value is None:
            tag, payload = b"N", b""
        elif isinstance(value, bytes):
            tag, payload = b"B", value
        elif isinstance(value, int):
            tag, payload = b"I", str(value).encode("ascii")
        elif isinstance(value, float):
            tag, payload = b"F", repr(value).encode("ascii")
        else:
            tag, payload = b"T", str(value).encode("utf-8", errors="replace")
        encoded.extend(tag)
        encoded.extend(str(len(payload)).encode("ascii"))
        encoded.extend(b":")
        encoded.extend(payload)
        encoded.extend(b";")
    return bytes(encoded)


def _digest_rows(rows: Sequence[Sequence[Any]]) -> str:
    row_digests = sorted(hashlib.sha256(_encode_signature_row(row)).digest() for row in rows)
    digest = hashlib.sha256()
    for row_digest in row_digests:
        digest.update(row_digest)
    return digest.hexdigest()


def _db_signature(
    connection: sqlite3.Connection,
    columns: Sequence[str],
    include_titles: bool = False,
    include_cwd: bool = False,
) -> Dict[str, Any]:
    required = {"id", "rollout_path", "source", "archived"}
    if not required.issubset(columns):
        missing = sorted(required - set(columns))
        raise CatalogError("threads table is missing columns: %s" % ", ".join(missing))

    select_parts = ["id", "rollout_path", "source", "archived"]
    for optional in (
        "model_provider",
        "thread_source",
        "cli_version",
        "created_at",
        "updated_at",
        "created_at_ms",
        "updated_at_ms",
    ):
        if optional in columns:
            select_parts.append(optional)
    if "first_user_message" in columns:
        select_parts.append("CASE WHEN first_user_message <> '' THEN 1 ELSE 0 END")
    if "preview" in columns:
        select_parts.append("LENGTH(preview)")
    if include_titles and "title" in columns:
        select_parts.append("title")
    if include_cwd and "cwd" in columns:
        select_parts.append("cwd")

    rows = list(connection.execute("SELECT %s FROM threads" % ", ".join(select_parts)))
    archived_count = sum(1 for row in rows if bool(row[3]))

    edge_columns = _table_columns(connection, "thread_spawn_edges")
    spawn_edges_available = {"parent_thread_id", "child_thread_id"}.issubset(edge_columns)
    edge_rows: List[Sequence[Any]] = []
    if spawn_edges_available:
        edge_select = ["parent_thread_id", "child_thread_id"]
        if "status" in edge_columns:
            edge_select.append("status")
        edge_rows = list(
            connection.execute(
                "SELECT %s FROM thread_spawn_edges" % ", ".join(edge_select)
            )
        )

    return {
        "row_count": len(rows),
        "archived_count": archived_count,
        "metadata_sha256": _digest_rows(rows),
        "spawn_edges_available": spawn_edges_available,
        "spawn_edge_count": len(edge_rows),
        "spawn_edges_sha256": _digest_rows(edge_rows) if spawn_edges_available else None,
    }


def read_catalog(
    database_path: Path, include_titles: bool = False, include_cwd: bool = False
) -> Dict[str, Any]:
    """Read all relevant catalog facts within one short, query-only transaction."""
    uri = "%s?mode=ro" % database_path.resolve().as_uri()
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=10.0)
        connection.row_factory = sqlite3.Row
        with closing(connection):
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            columns = _table_columns(connection, "threads")
            if not columns:
                raise CatalogError("threads table is missing")

            required = ["id", "rollout_path", "source", "archived"]
            missing = [name for name in required if name not in columns]
            if missing:
                raise CatalogError("threads table is missing columns: %s" % ", ".join(missing))

            select_parts = ["id", "rollout_path", "source", "archived"]
            for optional in (
                "archived_at",
                "model_provider",
                "thread_source",
                "cli_version",
                "created_at",
                "updated_at",
                "created_at_ms",
                "updated_at_ms",
            ):
                if optional in columns:
                    select_parts.append(optional)

            if "first_user_message" in columns:
                select_parts.append(
                    "CASE WHEN first_user_message <> '' THEN 1 ELSE 0 END AS db_has_user_message"
                )
            else:
                select_parts.append("NULL AS db_has_user_message")
            if "preview" in columns:
                select_parts.append("LENGTH(preview) AS preview_length")
            if include_titles and "title" in columns:
                select_parts.append("title")
            if include_cwd and "cwd" in columns:
                select_parts.append("cwd")

            rows = [dict(row) for row in connection.execute(
                "SELECT %s FROM threads" % ", ".join(select_parts)
            )]

            edges = []
            edge_columns = _table_columns(connection, "thread_spawn_edges")
            spawn_edges_available = {"parent_thread_id", "child_thread_id"}.issubset(
                edge_columns
            )
            if spawn_edges_available:
                edge_select = ["parent_thread_id", "child_thread_id"]
                if "status" in edge_columns:
                    edge_select.append("status")
                edges = [dict(row) for row in connection.execute(
                    "SELECT %s FROM thread_spawn_edges" % ", ".join(edge_select)
                )]

            quick_check_rows = [row[0] for row in connection.execute("PRAGMA quick_check")]
            signature = _db_signature(connection, columns, include_titles, include_cwd)
            connection.execute("COMMIT")

            return {
                "rows": rows,
                "spawn_edges": edges,
                "spawn_edges_available": spawn_edges_available,
                "columns": sorted(set(columns) & KNOWN_THREAD_COLUMNS),
                "quick_check": quick_check_rows,
                "signature": signature,
            }
    except (sqlite3.Error, OSError) as exc:
        raise CatalogError("could not read %s: %s" % (database_path, exc)) from exc


def read_catalog_signature(
    database_path: Path, include_titles: bool = False, include_cwd: bool = False
) -> Dict[str, Any]:
    uri = "%s?mode=ro" % database_path.resolve().as_uri()
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as connection:
            connection.execute("PRAGMA query_only=ON")
            columns = _table_columns(connection, "threads")
            return _db_signature(connection, columns, include_titles, include_cwd)
    except (sqlite3.Error, OSError) as exc:
        raise CatalogError("could not re-read catalog signature: %s" % exc) from exc


def read_archive_states(database_path: Path) -> Dict[str, Dict[str, Any]]:
    """Read the archive-related state for every thread in one query-only snapshot.

    The archive runner uses this narrow projection to verify that the official
    Codex command changed exactly the requested row.  It deliberately does not
    write to SQLite or expose these values in public reports.
    """

    uri = "%s?mode=ro" % database_path.resolve().as_uri()
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=10.0)
        connection.row_factory = sqlite3.Row
        with closing(connection):
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            columns = _table_columns(connection, "threads")
            required = {"id", "rollout_path", "archived"}
            if not required.issubset(columns):
                missing = sorted(required - set(columns))
                raise CatalogError(
                    "threads table is missing columns: %s" % ", ".join(missing)
                )

            archived_at = "archived_at" if "archived_at" in columns else "NULL"
            archive_rows = list(connection.execute(
                "SELECT id, rollout_path, archived, %s AS archived_at FROM threads"
                % archived_at
            ))
            metadata_columns = ["id"] + sorted(
                (set(columns) & KNOWN_THREAD_COLUMNS) - {"id"}
            )
            metadata_hashes = {
                str(row[0]): _digest_rows([row])
                for row in connection.execute(
                    "SELECT %s FROM threads" % ", ".join(metadata_columns)
                )
            }
            result = {
                str(row["id"]): {
                    "rollout_path": str(row["rollout_path"] or ""),
                    "archived": bool(row["archived"]),
                    "archived_at": row["archived_at"],
                    "metadata_sha256": metadata_hashes[str(row["id"])],
                }
                for row in archive_rows
            }
            connection.execute("COMMIT")
            return result
    except (sqlite3.Error, OSError) as exc:
        raise CatalogError("could not read archive state: %s" % exc) from exc


def read_spawn_edges(database_path: Path) -> List[Tuple[str, str]]:
    """Return persisted parent/child edges used by Codex archive cascading."""

    uri = "%s?mode=ro" % database_path.resolve().as_uri()
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=10.0)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            columns = _table_columns(connection, "thread_spawn_edges")
            required = {"parent_thread_id", "child_thread_id"}
            if not required.issubset(columns):
                raise CatalogError(
                    "thread_spawn_edges table is unavailable or missing required columns"
                )
            rows = [
                (str(parent), str(child))
                for parent, child in connection.execute(
                    "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"
                )
            ]
            connection.execute("COMMIT")
            return sorted(rows)
    except (sqlite3.Error, OSError) as exc:
        raise CatalogError("could not read spawn edges: %s" % exc) from exc
