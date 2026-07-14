from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_history.audit import run_audit
from codex_history.catalog import read_catalog_signature
from codex_history.cli import _default_audit_root, main
from codex_history.reports import publish_reports


DESKTOP_ID = "019e0000-0000-7000-8000-000000000001"
EMPTY_ID = "019e0000-0000-7000-8000-000000000002"
AGENT_ID = "019e0000-0000-7000-8000-000000000003"
GUARDIAN_ID = "019e0000-0000-7000-8000-000000000004"
AMBIGUOUS_ID = "019e0000-0000-7000-8000-000000000005"
MALFORMED_ID = "019e0000-0000-7000-80000-000000000006"
DB_ONLY_ID = "019e0000-0000-7000-8000-000000000007"
SPAWN_ID = "019e0000-0000-7000-8000-000000000008"
CANARY = "CANARY_PROMPT_MUST_NOT_LEAK"
CUSTOM_AUTOMATION = "example-orchestrator"
UNKNOWN_ORIGINATOR = "unconfigured-product"


def spawn_source(parent_thread_id=DESKTOP_ID):
    return {
        "subagent": {
            "thread_spawn": {
                "agent_nickname": "worker",
                "agent_path": None,
                "agent_role": "review",
                "depth": 1,
                "parent_thread_id": parent_thread_id,
            }
        }
    }


def tree_snapshot(root):
    snapshot = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            stat = path.stat()
            snapshot[path.relative_to(root).as_posix()] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                stat.st_size,
                stat.st_mtime_ns,
            )
    return snapshot


def write_rollout(
    codex_home,
    thread_id,
    originator,
    source,
    archived=False,
    meaningful=False,
    parent_thread_id=None,
    extra_meta=None,
):
    folder = codex_home / ("archived_sessions" if archived else "sessions/2026/07/13")
    folder.mkdir(parents=True, exist_ok=True)
    filename = "rollout-2026-07-13T12-00-00-%s.jsonl" % thread_id
    path = folder / filename
    payload = {
        "id": thread_id,
        "timestamp": "2026-07-13T16:00:00Z",
        "cwd": "/private/%s" % CANARY,
        "originator": originator,
        "cli_version": "0.test",
        "source": source,
        "thread_source": "user",
        "model_provider": "openai",
    }
    if parent_thread_id:
        payload["parent_thread_id"] = parent_thread_id
    if extra_meta:
        payload.update(extra_meta)
    records = [{"type": "session_meta", "payload": payload}]
    records.append(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<environment_context>%s</environment_context>" % CANARY}],
            },
        }
    )
    if meaningful:
        records.append(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": CANARY},
            }
        )
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def create_catalog(codex_home, rollouts, include_edge_table=True):
    database = codex_home / "state_5.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            rollout_path TEXT NOT NULL,
            source TEXT NOT NULL,
            archived INTEGER NOT NULL,
            archived_at INTEGER,
            model_provider TEXT,
            thread_source TEXT,
            cli_version TEXT,
            created_at INTEGER,
            updated_at INTEGER,
            created_at_ms INTEGER,
            updated_at_ms INTEGER,
            first_user_message TEXT NOT NULL DEFAULT '',
            preview TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL DEFAULT ''
        );
        """
    )
    if include_edge_table:
        connection.execute(
            """
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT NOT NULL,
                child_thread_id TEXT NOT NULL,
                status TEXT
            )
            """
        )
    for index, item in enumerate(rollouts, start=1):
        source = item["source"]
        stored_source = source if isinstance(source, str) else json.dumps(source, separators=(",", ":"))
        first = "present" if item["meaningful"] else ""
        connection.execute(
            """
            INSERT INTO threads (
                id, rollout_path, source, archived, model_provider, thread_source,
                cli_version, created_at, updated_at, created_at_ms, updated_at_ms,
                first_user_message, preview, title, cwd
            ) VALUES (?, ?, ?, ?, 'openai', 'user', '0.test', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"],
                str(item["path"]),
                stored_source,
                int(item["archived"]),
                index,
                index,
                index * 1000,
                index * 1000,
                first,
                first,
                CANARY,
                "/private/%s" % CANARY,
            ),
        )
    if include_edge_table:
        connection.execute(
            "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status) VALUES (?, ?, 'completed')",
            (DESKTOP_ID, GUARDIAN_ID),
        )
    connection.commit()
    connection.close()
    return database


class AuditIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.codex_home = self.root / ".codex"
        self.codex_home.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def _fixture(self):
        definitions = [
            (DESKTOP_ID, "Codex Desktop", "vscode", False, True, None),
            (EMPTY_ID, "codex_cli_rs", "cli", False, False, None),
            (AGENT_ID, CUSTOM_AUTOMATION, "vscode", True, False, None),
            (
                GUARDIAN_ID,
                "Codex Desktop",
                {"subagent": {"other": "guardian"}},
                False,
                False,
                DESKTOP_ID,
            ),
            (AMBIGUOUS_ID, UNKNOWN_ORIGINATOR, "vscode", False, False, None),
            (MALFORMED_ID, "Codex Desktop", "vscode", False, True, None),
        ]
        rollouts = []
        for thread_id, originator, source, archived, meaningful, parent in definitions:
            path = write_rollout(
                self.codex_home,
                thread_id,
                originator,
                source,
                archived,
                meaningful,
                parent,
            )
            rollouts.append(
                {
                    "id": thread_id,
                    "source": source,
                    "archived": archived,
                    "meaningful": meaningful,
                    "path": path,
                }
            )
        create_catalog(self.codex_home, rollouts)

    def test_audit_classifies_reconciles_and_redacts(self):
        self._fixture()
        before = tree_snapshot(self.codex_home)
        result = run_audit(
            self.codex_home,
            automated_originators=[" Example-Orchestrator ", CUSTOM_AUTOMATION],
        )
        after = tree_snapshot(self.codex_home)

        self.assertTrue(result["run"]["stable"])
        self.assertEqual(before, after)
        self.assertEqual(result["summary"]["rollout_files"], 6)
        self.assertEqual(result["summary"]["database_rows"], 6)
        self.assertEqual(
            result["summary"]["by_origin_class"],
            {"ambiguous": 1, "automated": 2, "interactive": 3},
        )
        self.assertEqual(result["summary"]["by_activity_state"]["meaningful"], 2)
        self.assertEqual(result["summary"]["by_activity_state"]["empty_shell"], 1)
        self.assertEqual(result["source"]["spawn_edge_count"], 1)
        self.assertTrue(result["source"]["spawn_edge_table_available"])
        self.assertEqual(result["source"]["database_quick_check"], ["ok"])
        self.assertEqual(
            result["classification"]["automated_originators"],
            [CUSTOM_AUTOMATION],
        )

        by_id = {thread["id"]: thread for thread in result["threads"]}
        self.assertEqual(by_id[EMPTY_ID]["activity_state"], "empty_shell")
        self.assertEqual(by_id[GUARDIAN_ID]["surface"], "guardian")
        self.assertEqual(by_id[AGENT_ID]["surface"], "custom_automation")
        self.assertEqual(by_id[AGENT_ID]["originator"], "custom_automation")
        self.assertNotIn(CUSTOM_AUTOMATION, json.dumps(by_id[AGENT_ID]))
        self.assertIn("UNKNOWN_ORIGINATOR_SOURCE", by_id[AMBIGUOUS_ID]["anomalies"])
        self.assertIn("INVALID_UUID_SHAPE", by_id[MALFORMED_ID]["anomalies"])
        self.assertNotIn("title", by_id[DESKTOP_ID]["database"])
        self.assertNotIn("cwd", by_id[DESKTOP_ID]["database"])

        serialized = json.dumps(result)
        self.assertNotIn(CANARY, serialized)
        self.assertNotIn(str(self.root), serialized)

    def test_reports_are_private_complete_and_do_not_overwrite(self):
        self._fixture()
        result = run_audit(
            self.codex_home, automated_originators=[CUSTOM_AUTOMATION]
        )
        output = self.root / "audit-output"
        manifest = publish_reports(result, output)

        self.assertTrue((output / "COMPLETE").is_file())
        self.assertTrue((output / ".codex-history-audit-private").is_file())
        self.assertEqual(len(manifest["files"]), 4)
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        for filename in (
            ".codex-history-audit-private",
            "audit.json",
            "threads.csv",
            "anomalies.json",
            "summary.txt",
        ):
            self.assertEqual((output / filename).stat().st_mode & 0o777, 0o600)
            self.assertNotIn(CANARY, (output / filename).read_text(encoding="utf-8"))

        with self.assertRaises(FileExistsError):
            publish_reports(result, output)

    def test_interrupt_during_report_publication_removes_private_staging(self):
        self._fixture()
        result = run_audit(
            self.codex_home, automated_originators=[CUSTOM_AUTOMATION]
        )
        output = self.root / "interrupt-report"
        with patch(
            "codex_history.reports._write_json",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                publish_reports(result, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".interrupt-report.tmp-*")), [])

    def test_untrusted_metadata_is_normalized_out_of_default_report(self):
        path = write_rollout(
            self.codex_home,
            DESKTOP_ID,
            CANARY,
            CANARY,
            extra_meta={
                "cli_version": CANARY,
                "thread_source": CANARY,
                "model_provider": CANARY,
            },
        )
        database = create_catalog(
            self.codex_home,
            [
                {
                    "id": DESKTOP_ID,
                    "source": CANARY,
                    "archived": False,
                    "meaningful": False,
                    "path": path,
                }
            ],
        )
        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE threads SET model_provider=?, thread_source=?, cli_version=?",
            (CANARY, CANARY, CANARY),
        )
        connection.commit()
        connection.close()

        result = run_audit(
            self.codex_home, automated_originators=[CUSTOM_AUTOMATION]
        )
        serialized = json.dumps(result)
        self.assertNotIn(CANARY, serialized)
        thread = result["threads"][0]
        self.assertEqual(thread["originator"], "other")
        self.assertEqual(thread["source"], "string:other")
        self.assertEqual(thread["thread_source"], "other")
        self.assertEqual(thread["model_provider"], "other")
        self.assertEqual(thread["cli_version"], "other")

    def test_cli_rejects_report_output_anywhere_inside_codex_home(self):
        output = self.codex_home / "audit-output"
        with self.assertRaises(SystemExit) as raised:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                main(
                    [
                        "audit",
                        "--codex-home",
                        str(self.codex_home),
                        "--out",
                        str(output),
                    ]
                )
        self.assertEqual(raised.exception.code, 3)
        self.assertFalse(output.exists())

    def test_cli_rejects_report_output_inside_any_git_worktree(self):
        repository = self.root / "public-repository"
        (repository / ".git").mkdir(parents=True)
        output = repository / "custom-report"
        with self.assertRaises(SystemExit) as raised:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                main(
                    [
                        "audit",
                        "--codex-home",
                        str(self.codex_home),
                        "--out",
                        str(output),
                    ]
                )
        self.assertEqual(raised.exception.code, 3)
        self.assertFalse(output.exists())

    def test_default_audit_root_can_be_kept_outside_the_repository(self):
        configured = self.root / "private-audits"
        with patch.dict(os.environ, {"CODEX_HISTORY_AUDIT_DIR": str(configured)}):
            self.assertEqual(_default_audit_root(), configured)

    def test_cli_accepts_repeatable_custom_automation_originators(self):
        self._fixture()
        output = self.root / "private-audit"
        with self.assertRaises(SystemExit) as raised:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                main(
                    [
                        "audit",
                        "--codex-home",
                        str(self.codex_home),
                        "--out",
                        str(output),
                        "--automated-originator",
                        " Example-Orchestrator ",
                        "--automated-originator",
                        CUSTOM_AUTOMATION,
                    ]
                )
        self.assertEqual(raised.exception.code, 0)
        audit = json.loads((output / "audit.json").read_text(encoding="utf-8"))
        self.assertEqual(
            audit["classification"]["automated_originators"],
            [CUSTOM_AUTOMATION],
        )
        by_id = {thread["id"]: thread for thread in audit["threads"]}
        self.assertEqual(by_id[AGENT_ID]["surface"], "custom_automation")

    def test_cli_rejects_official_interactive_originator_as_automation(self):
        output = self.root / "private-audit"
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                main(
                    [
                        "audit",
                        "--codex-home",
                        str(self.codex_home),
                        "--out",
                        str(output),
                        "--automated-originator",
                        "Codex Desktop",
                    ]
                )
        self.assertEqual(raised.exception.code, 3)
        self.assertIn("official interactive Codex originators", stderr.getvalue())
        self.assertFalse(output.exists())

    def test_missing_spawn_edge_table_does_not_create_false_missing_edges(self):
        parent = write_rollout(
            self.codex_home, DESKTOP_ID, "Codex Desktop", "vscode", meaningful=True
        )
        child_source = spawn_source()
        child = write_rollout(
            self.codex_home,
            SPAWN_ID,
            "Codex Desktop",
            child_source,
            parent_thread_id=DESKTOP_ID,
        )
        create_catalog(
            self.codex_home,
            [
                {
                    "id": DESKTOP_ID,
                    "source": "vscode",
                    "archived": False,
                    "meaningful": True,
                    "path": parent,
                },
                {
                    "id": SPAWN_ID,
                    "source": child_source,
                    "archived": False,
                    "meaningful": False,
                    "path": child,
                },
            ],
            include_edge_table=False,
        )

        result = run_audit(
            self.codex_home, automated_originators=[CUSTOM_AUTOMATION]
        )
        self.assertFalse(result["source"]["spawn_edge_table_available"])
        self.assertNotIn("SPAWN_EDGE_MISSING", {item["code"] for item in result["anomalies"]})

    def test_database_only_rows_are_in_classification_totals(self):
        self._fixture()
        database = self.codex_home / "state_5.sqlite"
        connection = sqlite3.connect(database)
        connection.execute(
            """
            INSERT INTO threads (
                id, rollout_path, source, archived, model_provider, thread_source,
                cli_version, created_at, updated_at, created_at_ms, updated_at_ms
            ) VALUES (?, ?, 'vscode', 0, 'openai', 'user', '0.1.0', 10, 10, 10000, 10000)
            """,
            (DB_ONLY_ID, str(self.codex_home / "sessions/missing.jsonl")),
        )
        connection.commit()
        connection.close()

        result = run_audit(
            self.codex_home, automated_originators=[CUSTOM_AUTOMATION]
        )
        self.assertEqual(result["summary"]["rollout_files"], 6)
        self.assertEqual(result["summary"]["database_rows"], 7)
        self.assertEqual(result["summary"]["unique_thread_ids"], 7)
        self.assertEqual(
            result["summary"]["by_origin_class"],
            {"ambiguous": 2, "automated": 2, "interactive": 3},
        )

    def test_database_signature_detects_metadata_changes_without_timestamp_change(self):
        self._fixture()
        database = self.codex_home / "state_5.sqlite"
        before = read_catalog_signature(database)
        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE threads SET source='exec' WHERE id=?",
            (DESKTOP_ID,),
        )
        connection.commit()
        connection.close()
        after = read_catalog_signature(database)
        self.assertNotEqual(before["metadata_sha256"], after["metadata_sha256"])

    def test_database_signature_covers_opted_in_title_and_cwd(self):
        self._fixture()
        database = self.codex_home / "state_5.sqlite"
        before = read_catalog_signature(database, include_titles=True, include_cwd=True)
        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE threads SET title='changed', cwd='/changed' WHERE id=?",
            (DESKTOP_ID,),
        )
        connection.commit()
        connection.close()
        after = read_catalog_signature(database, include_titles=True, include_cwd=True)
        self.assertNotEqual(before["metadata_sha256"], after["metadata_sha256"])

    def test_new_state_database_selection_marks_audit_unstable(self):
        self._fixture()
        state_5 = self.codex_home / "state_5.sqlite"
        state_6 = self.codex_home / "state_6.sqlite"
        with patch(
            "codex_history.audit.find_state_database",
            side_effect=[
                (state_5, ["state_5.sqlite"]),
                (state_6, ["state_6.sqlite", "state_5.sqlite"]),
            ],
        ):
            result = run_audit(
                self.codex_home, automated_originators=[CUSTOM_AUTOMATION]
            )

        self.assertFalse(result["run"]["stable"])
        self.assertIn(
            "STATE_DATABASE_SELECTION_CHANGED",
            {item["code"] for item in result["anomalies"]},
        )

    def test_persistent_inventory_anomalies_are_deduplicated(self):
        self._fixture()
        symlink = self.codex_home / "sessions" / "linked"
        symlink.symlink_to(self.codex_home / "archived_sessions", target_is_directory=True)
        result = run_audit(
            self.codex_home, automated_originators=[CUSTOM_AUTOMATION]
        )
        matches = [item for item in result["anomalies"] if item["code"] == "SYMLINK_SKIPPED"]
        self.assertEqual(len(matches), 1)


if __name__ == "__main__":
    unittest.main()
