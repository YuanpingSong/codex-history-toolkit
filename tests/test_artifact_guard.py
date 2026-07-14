import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_history.artifact_guard import forbidden_reason, violations


class ArtifactGuardTests(unittest.TestCase):
    def test_rejects_generated_report_paths(self):
        paths = [
            "audits/audit-20260713T202355Z/audit.json",
            "private/audit-20260713T202355Z/renamed.data",
            "private/archive-plan-20260713T202355Z-deadbeef/renamed.data",
            "private/export-20260713T202355Z-deadbeef/renamed.data",
            "private/AUDIT-20260713T202355Z/renamed.data",
            "EXPORTS/portable-history/renamed.data",
            "exports/portable-history/renamed.data",
            "private/codex-history-archive-journal.jsonl",
            "private/.codex-history-archive-private",
            "private/.codex-history-export-private",
            "custom/.codex-history-audit-private",
            "custom/threads.csv",
            "custom/AUDIT.JSON",
        ]
        self.assertEqual([item[0] for item in violations(paths)], sorted(paths))

    def test_allows_source_and_synthetic_fixture_names(self):
        for path in (
            "README.md",
            "src/codex_history/reports.py",
            "tests/fixtures/synthetic-audit.json",
            "tests/fixtures/sessions/synthetic-rollout.jsonl",
            "tests/fixtures/synthetic-state.sqlite",
            "tests/fixtures/synthetic-auth.json",
            "src/sessions/manager.py",
            ".env.example",
            ".env.sample",
            ".env.template",
        ):
            with self.subTest(path=path):
                self.assertIsNone(forbidden_reason(path))

    def test_rejects_likely_copies_of_private_codex_data(self):
        rollout = (
            "rollout-2026-07-13T20-23-55-"
            "00000000-0000-4000-8000-000000000099.jsonl"
        )
        paths = [
            ".codex/config.toml",
            "backup/.codex/history.jsonl",
            "auth.json",
            "backup/auth.json",
            "backup/history.jsonl",
            "backup/.codex-global-state.json",
            "state.sqlite",
            "backup/state_5.sqlite",
            "backup/state_5.sqlite-wal",
            "backup/state_5.sqlite-shm",
            "backup/state_5.sqlite-journal",
            "backup/state_5.sqlite.backup",
            "backup/state_5.sqlite-snapshot-20260713",
            "backup/goals.sqlite",
            "backup/logs_2.sqlite-wal",
            "sessions/README.txt",
            "backup/archived_sessions/index.txt",
            "sessions/2026/07/13/" + rollout,
            "backup/archived_sessions/" + rollout,
            "copied/" + rollout,
            ".env",
            "config/.env.local",
            "dist/codex_history_toolkit-0.3.0.tar.gz",
            "build/lib/codex_history/cli.py",
            "src/codex_history_toolkit.egg-info/PKG-INFO",
        ]
        self.assertEqual([item[0] for item in violations(paths)], sorted(paths))

    def test_staged_and_history_scopes_reject_a_real_git_path(self):
        source_root = Path(__file__).resolve().parents[1] / "src"
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Synthetic Test"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "synthetic@example.invalid"],
                cwd=repository,
                check=True,
            )
            (repository / "audit.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "add", "audit.json"], cwd=repository, check=True)

            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(source_root)
            staged = subprocess.run(
                ["python3", "-m", "codex_history.artifact_guard", "staged"],
                cwd=repository,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(staged.returncode, 1)

            subprocess.run(
                ["git", "commit", "-q", "-m", "synthetic private artifact"],
                cwd=repository,
                check=True,
            )
            history = subprocess.run(
                ["python3", "-m", "codex_history.artifact_guard", "history"],
                cwd=repository,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(history.returncode, 1)


if __name__ == "__main__":
    unittest.main()
