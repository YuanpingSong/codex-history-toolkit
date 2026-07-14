from contextlib import redirect_stderr, redirect_stdout
import json
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_history.archive import (
    ArchiveOperationStopped,
    CommandOutcome,
    JOURNAL_FILENAME,
    Journal,
    PLAN_COMPLETE,
    PLAN_FILENAME,
    PLAN_PRIVATE_SENTINEL,
    PLAN_SUMMARY_FILENAME,
    RESULT_FILENAME,
    RUN_COMPLETE,
    RUN_LOCK,
    UnsafeArchiveState,
    build_archive_plan,
    create_archive_run,
    execute_archive_run,
    load_verified_plan,
    publish_archive_plan,
)
from codex_history.audit import run_audit
from codex_history.catalog import read_archive_states
from codex_history.cli import main
from codex_history.reports import publish_reports


AUTOMATED_ID = "019e1000-0000-7000-8000-000000000001"
GUARDIAN_ID = "019e1000-0000-7000-8000-000000000002"
EMPTY_ID = "019e1000-0000-7000-8000-000000000003"
MEANINGFUL_ID = "019e1000-0000-7000-8000-000000000004"
AMBIGUOUS_ID = "019e1000-0000-7000-8000-000000000005"
ALREADY_ARCHIVED_ID = "019e1000-0000-7000-8000-000000000006"
SPAWN_PARENT_ID = "019e1000-0000-7000-8000-000000000007"
SPAWN_CHILD_ID = "019e1000-0000-7000-8000-000000000008"
CANARY = "PRIVATE_CLI_OUTPUT_MUST_NOT_ENTER_THE_JOURNAL"
CUSTOM_AUTOMATION = "example-orchestrator"


def write_rollout(
    codex_home,
    thread_id,
    originator,
    source,
    archived=False,
    meaningful=False,
):
    folder = codex_home / ("archived_sessions" if archived else "sessions/2026/07/13")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("rollout-2026-07-13T12-00-00-%s.jsonl" % thread_id)
    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "timestamp": "2026-07-13T16:00:00Z",
                "cwd": "/private/not-reported",
                "originator": originator,
                "cli_version": "0.144.0",
                "source": source,
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
                    {"type": "input_text", "text": "<environment_context>private</environment_context>"}
                ],
            },
        },
    ]
    if meaningful:
        records.append(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "private prompt"},
            }
        )
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
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
    for index, definition in enumerate(definitions, start=1):
        source = definition["source"]
        stored_source = source if isinstance(source, str) else json.dumps(source)
        preview = "present" if definition["meaningful"] else ""
        connection.execute(
            """
            INSERT INTO threads (
                id, rollout_path, source, archived, archived_at, model_provider,
                thread_source, cli_version, created_at, updated_at, created_at_ms,
                updated_at_ms, first_user_message, preview, title, cwd
            ) VALUES (?, ?, ?, ?, ?, 'openai', 'user', '0.144.0', ?, ?, ?, ?, ?, ?, '', '')
            """,
            (
                definition["id"],
                str(definition["path"]),
                stored_source,
                int(definition["archived"]),
                index if definition["archived"] else None,
                index,
                index,
                index * 1000,
                index * 1000,
                preview,
                preview,
            ),
        )
    connection.commit()
    connection.close()
    return database


def insert_catalog_thread(database, definition, index=99):
    source = definition["source"]
    stored_source = source if isinstance(source, str) else json.dumps(source)
    preview = "present" if definition["meaningful"] else ""
    connection = sqlite3.connect(database)
    connection.execute(
        """
        INSERT INTO threads (
            id, rollout_path, source, archived, archived_at, model_provider,
            thread_source, cli_version, created_at, updated_at, created_at_ms,
            updated_at_ms, first_user_message, preview, title, cwd
        ) VALUES (?, ?, ?, ?, ?, 'openai', 'user', '0.144.0', ?, ?, ?, ?, ?, ?, '', '')
        """,
        (
            definition["id"],
            str(definition["path"]),
            stored_source,
            int(definition["archived"]),
            index if definition["archived"] else None,
            index,
            index,
            index * 1000,
            index * 1000,
            preview,
            preview,
        ),
    )
    connection.commit()
    connection.close()


class ArchiveRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.codex_home = self.root / ".codex"
        self.codex_home.mkdir()
        definitions = [
            (AUTOMATED_ID, CUSTOM_AUTOMATION, "vscode", False, False),
            (
                GUARDIAN_ID,
                "Codex Desktop",
                {"subagent": {"other": "guardian"}},
                False,
                False,
            ),
            (EMPTY_ID, "codex_cli_rs", "cli", False, False),
            (MEANINGFUL_ID, "Codex Desktop", "vscode", False, True),
            (AMBIGUOUS_ID, "future-origin", "vscode", False, False),
            (ALREADY_ARCHIVED_ID, CUSTOM_AUTOMATION, "vscode", True, False),
        ]
        rows = []
        self.paths = {}
        for thread_id, originator, source, archived, meaningful in definitions:
            path = write_rollout(
                self.codex_home,
                thread_id,
                originator,
                source,
                archived=archived,
                meaningful=meaningful,
            )
            self.paths[thread_id] = path
            rows.append(
                {
                    "id": thread_id,
                    "source": source,
                    "archived": archived,
                    "meaningful": meaningful,
                    "path": path,
                }
            )
        self.database = create_catalog(self.codex_home, rows)
        self.report = self.root / "private-audit"
        publish_reports(self.audit(), self.report)

    def tearDown(self):
        self.temporary.cleanup()

    def audit(self):
        return run_audit(
            self.codex_home, automated_originators=[CUSTOM_AUTOMATION]
        )

    def make_plan(self, limit=None):
        return build_archive_plan(
            self.report,
            self.codex_home,
            ["automated", "empty-shell"],
            limit=limit,
        )

    def add_thread(
        self,
        thread_id,
        originator=CUSTOM_AUTOMATION,
        source="vscode",
        meaningful=False,
    ):
        path = write_rollout(
            self.codex_home,
            thread_id,
            originator,
            source,
            meaningful=meaningful,
        )
        self.paths[thread_id] = path
        insert_catalog_thread(
            self.database,
            {
                "id": thread_id,
                "source": source,
                "archived": False,
                "meaningful": meaningful,
                "path": path,
            },
        )

    def add_spawn_edge(self, parent, child):
        connection = sqlite3.connect(self.database)
        connection.execute(
            "INSERT INTO thread_spawn_edges "
            "(parent_thread_id, child_thread_id, status) VALUES (?, ?, 'completed')",
            (parent, child),
        )
        connection.commit()
        connection.close()

    def fresh_report(self, name="fresh-audit"):
        report = self.root / name
        publish_reports(self.audit(), report)
        return report

    def mutate_archive(self, thread_id, update_database=True, mutate_unrelated=False):
        connection = sqlite3.connect(self.database)
        row = connection.execute(
            "SELECT rollout_path FROM threads WHERE id=?", (thread_id,)
        ).fetchone()
        active = Path(row[0])
        archived = self.codex_home / "archived_sessions" / active.name
        active.rename(archived)
        if update_database:
            connection.execute(
                "UPDATE threads SET archived=1, archived_at=999, rollout_path=? WHERE id=?",
                (str(archived), thread_id),
            )
        connection.commit()
        connection.close()
        if mutate_unrelated:
            with self.paths[MEANINGFUL_ID].open("a", encoding="utf-8") as handle:
                handle.write("\n")

    def mutate_unarchive(self, thread_id):
        connection = sqlite3.connect(self.database)
        row = connection.execute(
            "SELECT rollout_path FROM threads WHERE id=?", (thread_id,)
        ).fetchone()
        archived = Path(row[0])
        active = self.paths[thread_id]
        archived.rename(active)
        connection.execute(
            "UPDATE threads SET archived=0, archived_at=NULL, rollout_path=? WHERE id=?",
            (str(active), thread_id),
        )
        connection.commit()
        connection.close()

    def runner(self, exit_code=0, update_database=True, mutate_unrelated=False):
        def run(_binary, thread_id, _codex_home, _timeout):
            self.mutate_archive(
                thread_id,
                update_database=update_database,
                mutate_unrelated=mutate_unrelated,
            )
            return CommandOutcome(exit_code, CANARY.encode(), CANARY.encode())

        return run

    def test_plan_selects_automated_guardian_and_empty_shell(self):
        plan = self.make_plan()
        self.assertEqual(
            [target["id"] for target in plan["targets"]],
            sorted([AUTOMATED_ID, GUARDIAN_ID, EMPTY_ID]),
        )
        self.assertEqual(plan["summary"]["guardian_count"], 1)
        self.assertEqual(
            plan["classification"]["automated_originators"],
            [CUSTOM_AUTOMATION],
        )
        by_id = {target["id"]: target for target in plan["targets"]}
        self.assertEqual(by_id[AUTOMATED_ID]["surface"], "custom_automation")
        self.assertEqual(plan["summary"]["excluded_by_reason"]["already_archived"], 1)
        self.assertEqual(len(plan["confirmation_token"]), 16)
        serialized = json.dumps(plan)
        self.assertNotIn("private prompt", serialized)
        self.assertNotIn("/private/not-reported", serialized)

    def test_plan_artifact_is_private_complete_and_checksum_verified(self):
        plan = self.make_plan(limit=1)
        output = self.root / "archive-plan"
        publish_archive_plan(plan, output)
        self.assertTrue((output / PLAN_PRIVATE_SENTINEL).is_file())
        self.assertTrue((output / PLAN_COMPLETE).is_file())
        self.assertTrue((output / PLAN_FILENAME).is_file())
        self.assertTrue((output / PLAN_SUMMARY_FILENAME).is_file())
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        for path in output.iterdir():
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_interrupt_during_plan_publication_removes_private_staging(self):
        plan = self.make_plan(limit=1)
        output = self.root / "interrupt-plan"
        with patch(
            "codex_history.archive._write_json",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                publish_archive_plan(plan, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".interrupt-plan.tmp-*")), [])

    def test_interrupt_during_run_creation_removes_private_staging(self):
        plan = self.make_plan(limit=1)
        output = self.root / "interrupt-run"
        with patch(
            "codex_history.archive._write_json",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                create_archive_run(plan, self.codex_home, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".interrupt-run.tmp-*")), [])

    def test_archive_plan_defaults_to_newest_completed_audit(self):
        audit_root = self.root / "audit-root"
        audit_root.mkdir()
        latest = audit_root / "audit-20260713T220000Z"
        publish_reports(self.audit(), latest)
        archive_root = self.root / "archive-root"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(
            os.environ,
            {
                "CODEX_HISTORY_AUDIT_DIR": str(audit_root),
                "CODEX_HISTORY_ARCHIVE_DIR": str(archive_root),
            },
        ):
            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    main(
                        [
                            "archive",
                            "plan",
                            "--codex-home",
                            str(self.codex_home),
                            "--include",
                            "automated",
                            "--limit",
                            "1",
                        ]
                    )
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("Using audit report: %s" % latest, stderr.getvalue())
        self.assertEqual(len(list(archive_root.glob("archive-plan-*"))), 1)

    def test_archive_plan_does_not_fall_back_past_incomplete_newest_audit(self):
        audit_root = self.root / "audit-root"
        audit_root.mkdir()
        publish_reports(
            self.audit(),
            audit_root / "audit-20260713T220000Z",
        )
        (audit_root / "audit-20260713T220001Z").mkdir()
        archive_root = self.root / "archive-root"
        stderr = io.StringIO()
        with patch.dict(
            os.environ,
            {
                "CODEX_HISTORY_AUDIT_DIR": str(audit_root),
                "CODEX_HISTORY_ARCHIVE_DIR": str(archive_root),
            },
        ):
            with self.assertRaises(SystemExit) as raised:
                with redirect_stderr(stderr):
                    main(
                        [
                            "archive",
                            "plan",
                            "--codex-home",
                            str(self.codex_home),
                            "--include",
                            "automated",
                        ]
                    )
        self.assertEqual(raised.exception.code, 6)
        self.assertIn("newest audit report is incomplete", stderr.getvalue())
        self.assertFalse(archive_root.exists())

    def test_archive_plan_does_not_ignore_unsafe_newest_audit_entry(self):
        for unsafe_kind in ("symlink", "file"):
            with self.subTest(unsafe_kind=unsafe_kind):
                audit_root = self.root / ("audit-root-" + unsafe_kind)
                audit_root.mkdir()
                older = audit_root / "audit-20260713T220000Z"
                publish_reports(self.audit(), older)
                newest = audit_root / "audit-20260713T220001Z"
                if unsafe_kind == "symlink":
                    newest.symlink_to(older, target_is_directory=True)
                else:
                    newest.write_text("not a report\n", encoding="utf-8")
                archive_root = self.root / ("archive-root-" + unsafe_kind)
                stderr = io.StringIO()
                with patch.dict(
                    os.environ,
                    {
                        "CODEX_HISTORY_AUDIT_DIR": str(audit_root),
                        "CODEX_HISTORY_ARCHIVE_DIR": str(archive_root),
                    },
                ):
                    with self.assertRaises(SystemExit) as raised:
                        with redirect_stderr(stderr):
                            main(
                                [
                                    "archive",
                                    "plan",
                                    "--codex-home",
                                    str(self.codex_home),
                                    "--include",
                                    "automated",
                                ]
                            )
                self.assertEqual(raised.exception.code, 6)
                self.assertIn(
                    "newest audit entry is not a safe report directory",
                    stderr.getvalue(),
                )
                self.assertFalse(archive_root.exists())

    def test_tampered_plan_artifact_and_directory_symlink_are_rejected(self):
        plan = self.make_plan(limit=1)
        output = self.root / "archive-plan"
        publish_archive_plan(plan, output)
        alias = self.root / "archive-plan-alias"
        alias.symlink_to(output, target_is_directory=True)
        with self.assertRaises(UnsafeArchiveState):
            load_verified_plan(alias)

        plan_path = output / PLAN_FILENAME
        plan_path.write_text(
            plan_path.read_text(encoding="utf-8") + " ", encoding="utf-8"
        )
        with self.assertRaises(UnsafeArchiveState):
            load_verified_plan(output)

    def test_self_consistent_plan_cannot_bypass_declared_selection(self):
        plan = self.make_plan(limit=1)
        plan["selections"] = ["guardian"]
        base = dict(plan)
        base.pop("plan_id")
        base.pop("confirmation_token")
        payload = json.dumps(
            base,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        plan["plan_id"] = hashlib.sha256(payload).hexdigest()
        plan["confirmation_token"] = plan["plan_id"][:16]
        with self.assertRaises(UnsafeArchiveState):
            publish_archive_plan(plan, self.root / "forged-plan")

    def test_spawned_descendants_are_planned_and_applied_before_parents(self):
        self.add_thread(SPAWN_PARENT_ID)
        self.add_thread(SPAWN_CHILD_ID)
        self.add_spawn_edge(SPAWN_PARENT_ID, SPAWN_CHILD_ID)
        report = self.fresh_report()
        plan = build_archive_plan(
            report,
            self.codex_home,
            ["automated", "empty-shell"],
        )
        target_ids = [target["id"] for target in plan["targets"]]
        self.assertLess(
            target_ids.index(SPAWN_CHILD_ID), target_ids.index(SPAWN_PARENT_ID)
        )
        self.assertEqual(plan["execution_order"], "spawned_descendants_first")
        pilot = build_archive_plan(
            report,
            self.codex_home,
            ["automated", "empty-shell"],
            limit=4,
        )
        pilot_ids = [target["id"] for target in pilot["targets"]]
        self.assertIn(SPAWN_CHILD_ID, pilot_ids)
        self.assertNotIn(SPAWN_PARENT_ID, pilot_ids)

        calls = []

        def cascading_runner(_binary, thread_id, _codex_home, _timeout):
            calls.append(thread_id)
            if thread_id == SPAWN_PARENT_ID:
                states = read_archive_states(self.database)
                if not states[SPAWN_CHILD_ID]["archived"]:
                    self.mutate_archive(SPAWN_CHILD_ID)
            self.mutate_archive(thread_id)
            return CommandOutcome(0, b"", b"")

        run_directory = create_archive_run(
            plan, self.codex_home, self.root / "cascade-run"
        )
        result = execute_archive_run(
            run_directory,
            self.codex_home,
            command_runner=cascading_runner,
        )
        self.assertEqual(result["status"], "complete")
        self.assertLess(calls.index(SPAWN_CHILD_ID), calls.index(SPAWN_PARENT_ID))

    def test_parent_with_unselected_active_descendant_is_excluded(self):
        self.add_thread(SPAWN_PARENT_ID)
        self.add_thread(
            SPAWN_CHILD_ID,
            originator="Codex Desktop",
            source="vscode",
            meaningful=True,
        )
        self.add_spawn_edge(SPAWN_PARENT_ID, SPAWN_CHILD_ID)
        report = self.fresh_report()
        plan = build_archive_plan(
            report,
            self.codex_home,
            ["automated", "empty-shell"],
        )
        self.assertNotIn(SPAWN_PARENT_ID, [item["id"] for item in plan["targets"]])
        self.assertEqual(
            plan["summary"]["excluded_by_reason"][
                "active_descendant_not_selected"
            ],
            1,
        )

    def test_changed_spawn_relationships_invalidate_a_plan(self):
        plan = self.make_plan(limit=1)
        self.add_spawn_edge(AUTOMATED_ID, MEANINGFUL_ID)
        with self.assertRaises(UnsafeArchiveState):
            create_archive_run(plan, self.codex_home, self.root / "run")

    def test_tampered_audit_is_rejected(self):
        audit_path = self.report / "audit.json"
        audit_path.write_text(audit_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaises(UnsafeArchiveState):
            self.make_plan(limit=1)

    def test_stale_audit_is_rejected_before_plan_creation(self):
        with self.paths[AUTOMATED_ID].open("a", encoding="utf-8") as handle:
            handle.write("\n")
        with self.assertRaises(UnsafeArchiveState):
            self.make_plan(limit=1)

    def test_apply_archives_every_target_and_journals_only_digests(self):
        plan = self.make_plan()
        run_directory = create_archive_run(plan, self.codex_home, self.root / "run")
        result = execute_archive_run(
            run_directory,
            self.codex_home,
            command_runner=self.runner(),
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["verified_success_count"], 3)
        self.assertTrue((run_directory / RUN_COMPLETE).is_file())
        states = read_archive_states(self.database)
        for target in plan["targets"]:
            self.assertTrue(states[target["id"]]["archived"])
            self.assertTrue(
                (self.codex_home / target["archived_relative_path"]).is_file()
            )
        journal_text = (run_directory / JOURNAL_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn(CANARY, journal_text)

    def test_nonzero_cli_exit_is_success_when_postcondition_is_verified(self):
        plan = self.make_plan(limit=1)
        run_directory = create_archive_run(plan, self.codex_home, self.root / "run")
        result = execute_archive_run(
            run_directory,
            self.codex_home,
            command_runner=self.runner(exit_code=9),
        )
        self.assertEqual(result["status"], "complete")
        entries = Journal(run_directory / JOURNAL_FILENAME).entries
        statuses = [entry.get("status") for entry in entries]
        self.assertIn("verified_success_with_cli_error", statuses)

    def test_zero_exit_without_state_change_stops(self):
        plan = self.make_plan(limit=1)
        run_directory = create_archive_run(plan, self.codex_home, self.root / "run")

        def noop(_binary, _thread_id, _codex_home, _timeout):
            return CommandOutcome(0, b"ok", b"")

        with self.assertRaises(ArchiveOperationStopped):
            execute_archive_run(
                run_directory, self.codex_home, command_runner=noop
            )
        result = json.loads((run_directory / RESULT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(
            result["failure_code"], "cli_success_without_verified_postcondition"
        )

    def test_partial_file_only_archive_stops(self):
        plan = self.make_plan(limit=1)
        run_directory = create_archive_run(plan, self.codex_home, self.root / "run")
        with self.assertRaises(ArchiveOperationStopped):
            execute_archive_run(
                run_directory,
                self.codex_home,
                command_runner=self.runner(exit_code=1, update_database=False),
            )
        result = json.loads((run_directory / RESULT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(
            result["failure_code"], "cli_failure_without_verified_postcondition"
        )

    def test_unrelated_rollout_change_stops(self):
        plan = self.make_plan(limit=1)
        run_directory = create_archive_run(plan, self.codex_home, self.root / "run")
        with self.assertRaises(ArchiveOperationStopped) as raised:
            execute_archive_run(
                run_directory,
                self.codex_home,
                command_runner=self.runner(mutate_unrelated=True),
            )
        result = json.loads((run_directory / RESULT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(result["failure_code"], "unrelated_concurrent_change")
        self.assertEqual(
            result["failure_details"]["rollout_files"],
            {"added": 0, "changed": 1, "removed": 0},
        )
        self.assertTrue(result["failure_details"]["target_postcondition_verified"])
        self.assertIn("target 1/1 after 0 verified successes", str(raised.exception))

    def test_unrelated_database_metadata_change_stops(self):
        plan = self.make_plan(limit=1)
        run_directory = create_archive_run(plan, self.codex_home, self.root / "run")

        def mutate_database(_binary, thread_id, _codex_home, _timeout):
            self.mutate_archive(thread_id)
            connection = sqlite3.connect(self.database)
            connection.execute(
                "UPDATE threads SET preview='changed' WHERE id=?",
                (MEANINGFUL_ID,),
            )
            connection.commit()
            connection.close()
            return CommandOutcome(0, b"", b"")

        with self.assertRaises(ArchiveOperationStopped):
            execute_archive_run(
                run_directory,
                self.codex_home,
                command_runner=mutate_database,
            )
        result = json.loads((run_directory / RESULT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(result["failure_code"], "unrelated_concurrent_change")
        self.assertEqual(
            result["failure_details"]["catalog_rows"],
            {"added": 0, "changed": 1, "removed": 0},
        )

    def test_pending_target_metadata_change_between_commands_stops(self):
        plan = self.make_plan()
        run_directory = create_archive_run(plan, self.codex_home, self.root / "run")
        pending_id = plan["targets"][1]["id"]
        calls = []

        def counted_runner(_binary, thread_id, _codex_home, _timeout):
            calls.append(thread_id)
            self.mutate_archive(thread_id)
            return CommandOutcome(0, b"", b"")

        def mutate_pending_after_first(index, _total, _thread_id, _status):
            if index == 1:
                connection = sqlite3.connect(self.database)
                connection.execute(
                    "UPDATE threads SET preview='changed' WHERE id=?",
                    (pending_id,),
                )
                connection.commit()
                connection.close()

        with self.assertRaises(UnsafeArchiveState):
            execute_archive_run(
                run_directory,
                self.codex_home,
                command_runner=counted_runner,
                progress=mutate_pending_after_first,
            )
        self.assertEqual(len(calls), 1)
        result = json.loads((run_directory / RESULT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(result["failure_code"], "unsafe_state")

    def test_completed_target_is_rechecked_before_the_next_command(self):
        plan = self.make_plan()
        run_directory = create_archive_run(plan, self.codex_home, self.root / "run")
        calls = []

        def counted_runner(_binary, thread_id, _codex_home, _timeout):
            calls.append(thread_id)
            self.mutate_archive(thread_id)
            return CommandOutcome(0, b"", b"")

        def mutate_after_first(index, _total, thread_id, _status):
            if index == 1:
                self.mutate_unarchive(thread_id)

        with self.assertRaises(UnsafeArchiveState):
            execute_archive_run(
                run_directory,
                self.codex_home,
                command_runner=counted_runner,
                progress=mutate_after_first,
            )
        self.assertEqual(len(calls), 1)
        result = json.loads((run_directory / RESULT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(result["failure_code"], "unsafe_state")

    def test_lock_symlink_is_rejected_without_touching_its_target(self):
        plan = self.make_plan(limit=1)
        run_directory = create_archive_run(plan, self.codex_home, self.root / "run")
        victim = self.root / "victim"
        victim.write_bytes(b"do not touch")
        victim.chmod(0o644)
        (run_directory / RUN_LOCK).symlink_to(victim)
        with self.assertRaises(UnsafeArchiveState):
            execute_archive_run(
                run_directory,
                self.codex_home,
                command_runner=self.runner(),
            )
        self.assertEqual(victim.read_bytes(), b"do not touch")
        self.assertEqual(victim.stat().st_mode & 0o777, 0o644)

    def test_torn_journal_tail_requires_resume_and_is_recovered(self):
        plan = self.make_plan(limit=1)
        run_directory = create_archive_run(plan, self.codex_home, self.root / "run")
        torn = b'{"sequence":1,"event":"run_started"'
        journal_path = run_directory / JOURNAL_FILENAME
        journal_path.write_bytes(torn)
        with self.assertRaises(UnsafeArchiveState):
            execute_archive_run(
                run_directory,
                self.codex_home,
                command_runner=self.runner(),
            )
        self.assertEqual(journal_path.read_bytes(), torn)

        result = execute_archive_run(
            run_directory,
            self.codex_home,
            command_runner=self.runner(),
            resume=True,
        )
        self.assertEqual(result["status"], "complete")
        entries = Journal(journal_path).entries
        recovery = [
            entry for entry in entries if entry["event"] == "journal_tail_recovered"
        ]
        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0]["discarded_size"], len(torn))
        self.assertEqual(recovery[0]["discarded_sha256"], hashlib.sha256(torn).hexdigest())

    def test_newline_terminated_corrupt_journal_is_never_repaired(self):
        plan = self.make_plan(limit=1)
        run_directory = create_archive_run(plan, self.codex_home, self.root / "run")
        corrupt = b"{not-json}\n"
        journal_path = run_directory / JOURNAL_FILENAME
        journal_path.write_bytes(corrupt)
        with self.assertRaises(UnsafeArchiveState):
            execute_archive_run(
                run_directory,
                self.codex_home,
                command_runner=self.runner(),
                resume=True,
            )
        self.assertEqual(journal_path.read_bytes(), corrupt)

    def test_resume_recovers_mutation_after_attempt_was_fsynced(self):
        plan = self.make_plan(limit=1)
        run_directory = create_archive_run(plan, self.codex_home, self.root / "run")
        journal = Journal(run_directory / JOURNAL_FILENAME)
        journal.append("run_started", plan_id=plan["plan_id"])
        journal.append("attempt_started", thread_id=plan["targets"][0]["id"])
        self.mutate_archive(plan["targets"][0]["id"])

        result = execute_archive_run(
            run_directory,
            self.codex_home,
            command_runner=self.runner(),
            resume=True,
        )
        self.assertEqual(result["status"], "complete")
        statuses = [
            entry.get("status")
            for entry in Journal(run_directory / JOURNAL_FILENAME).entries
        ]
        self.assertIn("recovered_verified_success", statuses)


if __name__ == "__main__":
    unittest.main()
