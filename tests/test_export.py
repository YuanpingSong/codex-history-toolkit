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

import codex_history.export as export_module
from codex_history.audit import run_audit
from codex_history.cli import main
from codex_history.export import (
    EXPORT_COMPLETE,
    EXPORT_MANIFEST,
    EXPORT_PRIVATE_SENTINEL,
    EXPORT_RESTORE,
    EXPORT_SUMMARY,
    UnsafeExportState,
    export_interactive_history,
)
from codex_history.reports import publish_reports


ACTIVE_ID = "019e2000-0000-7000-8000-000000000001"
ARCHIVED_ID = "019e2000-0000-7000-8000-000000000002"
EMPTY_ID = "019e2000-0000-7000-8000-000000000003"
PRIVATE_TEXT = "PRIVATE_EXPORT_FIXTURE_CONTENT"


def write_rollout(codex_home, thread_id, archived=False, meaningful=True):
    folder = codex_home / ("archived_sessions" if archived else "sessions/2026/07/13")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("rollout-2026-07-13T12-00-00-%s.jsonl" % thread_id)
    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "timestamp": "2026-07-13T16:00:00Z",
                "cwd": "/private/export-fixture",
                "originator": "Codex Desktop",
                "cli_version": "0.test",
                "source": "vscode",
                "thread_source": "user",
                "model_provider": "openai",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "<environment_context>fixture</environment_context>"}
                ],
            },
        },
    ]
    if meaningful:
        records.append(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": PRIVATE_TEXT},
            }
        )
    path.write_bytes(b"".join((json.dumps(item) + "\n").encode() for item in records))
    return path


def create_catalog(codex_home, definitions):
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
        CREATE TABLE thread_spawn_edges (
            parent_thread_id TEXT NOT NULL,
            child_thread_id TEXT NOT NULL,
            status TEXT
        );
        """
    )
    for index, item in enumerate(definitions, start=1):
        first = "present" if item["meaningful"] else ""
        connection.execute(
            """
            INSERT INTO threads (
                id, rollout_path, source, archived, archived_at, model_provider,
                thread_source, cli_version, created_at, updated_at, created_at_ms,
                updated_at_ms, first_user_message, preview, title, cwd
            ) VALUES (?, ?, 'vscode', ?, ?, 'openai', 'user', '0.test',
                      ?, ?, ?, ?, ?, ?, '', '')
            """,
            (
                item["id"],
                str(item["path"]),
                int(item["archived"]),
                index if item["archived"] else None,
                index,
                index,
                index * 1000,
                index * 1000,
                first,
                first,
            ),
        )
    connection.commit()
    connection.close()
    return database


def source_tree_snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            metadata = path.stat()
            result[path.relative_to(root).as_posix()] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                metadata.st_size,
                metadata.st_mtime_ns,
            )
    return result


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.codex_home = self.root / ".codex"
        self.codex_home.mkdir()
        self.active_path = write_rollout(self.codex_home, ACTIVE_ID)
        self.archived_path = write_rollout(
            self.codex_home, ARCHIVED_ID, archived=True
        )
        self.empty_path = write_rollout(
            self.codex_home, EMPTY_ID, meaningful=False
        )
        create_catalog(
            self.codex_home,
            [
                {
                    "id": ACTIVE_ID,
                    "path": self.active_path,
                    "archived": False,
                    "meaningful": True,
                },
                {
                    "id": ARCHIVED_ID,
                    "path": self.archived_path,
                    "archived": True,
                    "meaningful": True,
                },
                {
                    "id": EMPTY_ID,
                    "path": self.empty_path,
                    "archived": False,
                    "meaningful": False,
                },
            ],
        )
        self.report = self.root / "private-audit"
        publish_reports(run_audit(self.codex_home), self.report)

    def tearDown(self):
        self.temporary.cleanup()

    def test_export_preserves_active_and_archived_rollouts_without_mutating_source(self):
        before = source_tree_snapshot(self.codex_home)
        output = self.root / "private-export"
        manifest = export_interactive_history(
            self.report, self.codex_home, output
        )
        after = source_tree_snapshot(self.codex_home)

        self.assertEqual(before, after)
        self.assertEqual(manifest["summary"]["conversation_count"], 2)
        self.assertEqual(
            manifest["summary"]["by_storage_state"],
            {"active": 1, "archived": 1},
        )
        self.assertEqual(
            (output / self.active_path.relative_to(self.codex_home)).read_bytes(),
            self.active_path.read_bytes(),
        )
        self.assertEqual(
            (output / self.archived_path.relative_to(self.codex_home)).read_bytes(),
            self.archived_path.read_bytes(),
        )
        self.assertFalse((output / self.empty_path.relative_to(self.codex_home)).exists())
        self.assertFalse(any(path.name.endswith(".sqlite") for path in output.rglob("*")))

        self.assertTrue((output / EXPORT_PRIVATE_SENTINEL).is_file())
        self.assertTrue((output / EXPORT_COMPLETE).is_file())
        self.assertTrue((output / EXPORT_MANIFEST).is_file())
        self.assertTrue((output / EXPORT_SUMMARY).is_file())
        self.assertTrue((output / EXPORT_RESTORE).is_file())
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        for path in output.rglob("*"):
            if path.is_dir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            elif path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

        restore = (output / EXPORT_RESTORE).read_text(encoding="utf-8")
        self.assertIn("before Codex", restore)
        self.assertIn("excludes the Codex SQLite state database", restore)
        self.assertIn("no import or restore command", restore)
        self.assertIn("not a ChatGPT account transfer", restore)

        for entry in manifest["files"]:
            payload = (output / entry["path"]).read_bytes()
            self.assertEqual(entry["size"], len(payload))
            self.assertEqual(entry["sha256"], hashlib.sha256(payload).hexdigest())

    def test_include_empty_shells_is_explicit(self):
        output = self.root / "private-export-with-shells"
        manifest = export_interactive_history(
            self.report,
            self.codex_home,
            output,
            include_empty_shells=True,
        )
        self.assertEqual(manifest["summary"]["conversation_count"], 3)
        self.assertEqual(
            manifest["summary"]["by_activity_state"],
            {"empty_shell": 1, "meaningful": 2},
        )
        self.assertEqual(
            (output / self.empty_path.relative_to(self.codex_home)).read_bytes(),
            self.empty_path.read_bytes(),
        )

    def test_stale_audit_fails_closed_without_publishing(self):
        output = self.root / "stale-export"
        with self.active_path.open("ab") as handle:
            handle.write(b"changed\n")
        with self.assertRaises(UnsafeExportState):
            export_interactive_history(self.report, self.codex_home, output)
        self.assertFalse(output.exists())

    def test_symlink_replacement_fails_closed(self):
        output = self.root / "symlink-export"
        moved = self.active_path.with_suffix(".saved")
        self.active_path.rename(moved)
        self.active_path.symlink_to(moved)
        with self.assertRaises(UnsafeExportState):
            export_interactive_history(self.report, self.codex_home, output)
        self.assertFalse(output.exists())

    def test_change_during_copy_removes_staging_and_publishes_nothing(self):
        output = self.root / "changed-during-export"
        original = export_module._copy_verified_file
        changed = False

        def copy_then_change(source, destination, expected):
            nonlocal changed
            result = original(source, destination, expected)
            if not changed:
                with source.open("ab") as handle:
                    handle.write(b"changed-during-copy\n")
                changed = True
            return result

        with patch(
            "codex_history.export._copy_verified_file",
            side_effect=copy_then_change,
        ):
            with self.assertRaises(UnsafeExportState):
                export_interactive_history(self.report, self.codex_home, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".changed-during-export.tmp-*")), [])

    def test_interrupt_during_copy_removes_private_staging(self):
        output = self.root / "interrupt-export"
        with patch(
            "codex_history.export._copy_verified_file",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                export_interactive_history(self.report, self.codex_home, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".interrupt-export.tmp-*")), [])

    def test_cli_defaults_to_latest_completed_audit_and_private_export_root(self):
        audit_root = self.root / "audit-root"
        audit_root.mkdir()
        latest = audit_root / "audit-20260713T220000Z"
        publish_reports(run_audit(self.codex_home), latest)
        export_root = self.root / "export-root"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(
            os.environ,
            {
                "CODEX_HISTORY_AUDIT_DIR": str(audit_root),
                "CODEX_HISTORY_EXPORT_DIR": str(export_root),
            },
        ):
            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    main(["export", "--codex-home", str(self.codex_home)])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("Using audit report: %s" % latest, stderr.getvalue())
        self.assertIn("Export complete: 2 conversations", stdout.getvalue())
        bundles = list(export_root.glob("export-*"))
        self.assertEqual(len(bundles), 1)
        self.assertTrue((bundles[0] / EXPORT_COMPLETE).is_file())

    def test_cli_rejects_output_inside_git_worktree(self):
        worktree = self.root / "public-repository"
        (worktree / ".git").mkdir(parents=True)
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            with redirect_stderr(stderr):
                main(
                    [
                        "export",
                        "--from-audit",
                        str(self.report),
                        "--codex-home",
                        str(self.codex_home),
                        "--out",
                        str(worktree / "private-export"),
                    ]
                )
        self.assertEqual(raised.exception.code, 3)
        self.assertIn("Git worktree", stderr.getvalue())

    def test_export_help_states_scope(self):
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            with redirect_stdout(stdout):
                main(["export", "--help"])
        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("SQLite, auth, and config are not exported", help_text)
        self.assertIn("--include-empty-shells", help_text)


if __name__ == "__main__":
    unittest.main()
