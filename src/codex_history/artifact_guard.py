"""Reject generated reports and likely copies of private Codex data."""

from __future__ import annotations

import argparse
from pathlib import PurePosixPath
import re
import subprocess
import sys
from typing import Iterable, List, Optional, Sequence, Tuple


AUDIT_DIRECTORY_RE = re.compile(r"^audit-\d{8}T\d{6}Z$", re.IGNORECASE)
ARCHIVE_DIRECTORY_RE = re.compile(
    r"^archive-(?:plan|run)-\d{8}T\d{6}Z(?:-[0-9a-f]{8})?$",
    re.IGNORECASE,
)
EXPORT_DIRECTORY_RE = re.compile(
    r"^export-\d{8}T\d{6}Z(?:-[0-9a-f]{8})?$",
    re.IGNORECASE,
)
STATE_DATABASE_RE = re.compile(
    r"^(?:state|goals|logs)(?:_\d+)?\.sqlite"
    r"(?:[-.](?:wal|shm|journal|backup|bak|snapshot)(?:[-._][a-z0-9]+)*)?$",
    re.IGNORECASE,
)
ROLLOUT_FILENAME_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-[0-9a-f-]{32,}\.jsonl$",
    re.IGNORECASE,
)
ENV_FILE_RE = re.compile(r"^\.env(?:\..+)?$", re.IGNORECASE)
ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template")
PRIVATE_DIRECTORY_NAMES = {".codex"}
CODEX_SESSION_DIRECTORY_NAMES = {"sessions", "archived_sessions"}
PACKAGE_OUTPUT_DIRECTORY_NAMES = {"build", "dist"}
PRIVATE_FILENAMES = {
    ".codex-global-state.json",
    "auth.json",
    "history.jsonl",
}
SOURCE_SUFFIXES = {".py", ".pyi"}
GENERATED_FILENAMES = {
    ".codex-history-audit-private",
    ".codex-history-archive-private",
    ".codex-history-archive-run.lock",
    ".codex-history-export-private",
    "COMPLETE",
    "CODEX_HISTORY_ARCHIVE_PLAN_COMPLETE",
    "CODEX_HISTORY_ARCHIVE_RUN_COMPLETE",
    "anomalies.json",
    "audit.json",
    "codex-history-archive-journal.jsonl",
    "codex-history-archive-plan-summary.txt",
    "codex-history-archive-plan.json",
    "codex-history-archive-result.json",
    "codex-history-archive-run-plan.json",
    "codex-history-archive-run.json",
    "manifest.json",
    "summary.txt",
    "threads.csv",
}
GENERATED_FILENAMES_LOWER = {name.lower() for name in GENERATED_FILENAMES}


def forbidden_reason(path: str) -> Optional[str]:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    parts = PurePosixPath(normalized).parts
    if not parts:
        return None
    filename = parts[-1]
    lowered_parts = tuple(part.lower() for part in parts)
    lowered_filename = filename.lower()
    if any(part in PRIVATE_DIRECTORY_NAMES for part in lowered_parts):
        return "path is inside a copied Codex home directory"
    if any(part in PACKAGE_OUTPUT_DIRECTORY_NAMES for part in lowered_parts[:-1]):
        return "path is inside a generated package-output directory"
    if any(part.endswith(".egg-info") for part in lowered_parts[:-1]):
        return "path is inside generated package metadata"
    if lowered_filename in PRIVATE_FILENAMES:
        return "filename is used for private Codex state or authentication data"
    if STATE_DATABASE_RE.fullmatch(filename):
        return "filename looks like a live Codex state database"
    if ENV_FILE_RE.fullmatch(filename) and not lowered_filename.endswith(
        ENV_TEMPLATE_SUFFIXES
    ):
        return "filename looks like a private environment file"
    if ROLLOUT_FILENAME_RE.fullmatch(filename):
        if any(part in CODEX_SESSION_DIRECTORY_NAMES for part in lowered_parts[:-1]):
            return "path looks like a copied Codex session rollout"
        return "filename looks like a private Codex rollout"
    if any(part in CODEX_SESSION_DIRECTORY_NAMES for part in lowered_parts[:-1]):
        is_python_source = (
            lowered_parts[0] == "src"
            and PurePosixPath(lowered_filename).suffix in SOURCE_SUFFIXES
        )
        is_named_synthetic_fixture = (
            len(lowered_parts) >= 3
            and lowered_parts[:2] == ("tests", "fixtures")
            and any(part.startswith("synthetic-") for part in lowered_parts[2:])
        )
        if not is_python_source and not is_named_synthetic_fixture:
            return "path is inside a copied Codex session directory"
    if "audits" in lowered_parts:
        return "path is inside an audits directory"
    if any(AUDIT_DIRECTORY_RE.fullmatch(part) for part in parts):
        return "path is inside a timestamped audit directory"
    if any(ARCHIVE_DIRECTORY_RE.fullmatch(part) for part in parts):
        return "path is inside a generated archive operation directory"
    if "exports" in lowered_parts:
        return "path is inside an exports directory"
    if any(EXPORT_DIRECTORY_RE.fullmatch(part) for part in parts):
        return "path is inside a generated history export directory"
    if lowered_filename in GENERATED_FILENAMES_LOWER:
        return "filename is reserved for generated private operation output"
    return None


def _run_git(arguments: Sequence[str]) -> bytes:
    completed = subprocess.run(
        ["git"] + list(arguments),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


def _nul_paths(payload: bytes) -> List[str]:
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in payload.split(b"\0")
        if item
    ]


def paths_for_scope(scope: str) -> List[str]:
    if scope == "staged":
        return _nul_paths(
            _run_git(
                [
                    "diff",
                    "--cached",
                    "--name-only",
                    "--diff-filter=ACMR",
                    "-z",
                ]
            )
        )
    if scope == "tracked":
        return _nul_paths(_run_git(["ls-files", "-z"]))
    if scope == "history":
        payload = _run_git(["log", "--all", "--format=", "--name-only", "-z"])
        return _nul_paths(payload.replace(b"\n", b""))
    raise ValueError("unknown scope: %s" % scope)


def violations(paths: Iterable[str]) -> List[Tuple[str, str]]:
    found = []
    for path in sorted(set(paths)):
        reason = forbidden_reason(path)
        if reason:
            found.append((path, reason))
    return found


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reject private Codex data and generated operation output in Git."
    )
    parser.add_argument(
        "scope",
        choices=("staged", "tracked", "history"),
        help="Git paths to inspect.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        found = violations(paths_for_scope(args.scope))
    except (OSError, subprocess.CalledProcessError) as exc:
        print("error: could not inspect Git paths: %s" % exc, file=sys.stderr)
        return 2
    if not found:
        print("Artifact guard: no private Codex data found in Git %s." % args.scope)
        return 0

    print(
        "ERROR: private Codex data or generated output must not be committed:",
        file=sys.stderr,
    )
    for path, reason in found:
        print("  %s (%s)" % (path, reason), file=sys.stderr)
    print(
        "Move the data outside the repository and unstage it before continuing.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
