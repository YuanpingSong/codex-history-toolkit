"""Command-line interface."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shlex
import sys
from typing import Optional, Sequence

from . import __version__
from .archive import (
    ALLOWED_SELECTIONS,
    ArchiveError,
    ArchiveOperationStopped,
    build_archive_plan,
    create_archive_run,
    default_plan_path,
    default_run_path,
    execute_archive_run,
    load_verified_plan,
    publish_archive_plan,
    render_plan_summary,
)
from .audit import run_audit
from .export import (
    ExportError,
    default_export_path,
    export_interactive_history,
)
from .reports import default_output_path, publish_reports, render_summary


AUDIT_REPORT_DIRECTORY_RE = re.compile(r"^audit-\d{8}T\d{6}Z$")


def _default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _default_audit_root() -> Path:
    configured = os.environ.get("CODEX_HISTORY_AUDIT_DIR")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / "CodexHistoryAudits"
    )


def _default_archive_root() -> Path:
    configured = os.environ.get("CODEX_HISTORY_ARCHIVE_DIR")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / "CodexHistoryArchives"
    )


def _default_export_root() -> Path:
    configured = os.environ.get("CODEX_HISTORY_EXPORT_DIR")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / "CodexHistoryExports"
    )


def _latest_audit_report(root: Path) -> Path:
    root = root.expanduser()
    try:
        candidates = sorted(
            (
                path
                for path in root.iterdir()
                if AUDIT_REPORT_DIRECTORY_RE.fullmatch(path.name)
            ),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError as exc:
        raise ArchiveError("could not inspect the audit directory: %s" % exc) from exc
    if not candidates:
        raise ArchiveError(
            "no audit reports found; run 'codex-history audit --require-stable' first"
        )
    latest = candidates[0]
    if latest.is_symlink() or not latest.is_dir():
        raise ArchiveError(
            "the newest audit entry is not a safe report directory; "
            "run a fresh stable audit"
        )
    complete = latest / "COMPLETE"
    if complete.is_symlink() or not complete.is_file():
        raise ArchiveError(
            "the newest audit report is incomplete; run a fresh stable audit"
        )
    return latest


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _containing_git_worktree(path: Path) -> Optional[Path]:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    if current.is_file():
        current = current.parent
    for candidate in (current,) + tuple(current.parents):
        git_marker = candidate / ".git"
        if git_marker.is_dir() or git_marker.is_file():
            return candidate
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-history",
        description=(
            "Audit local Codex history, archive reviewed thread sets, and export "
            "interactive conversations for migration."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    audit = commands.add_parser(
        "audit", help="Inventory and classify local history without modifying CODEX_HOME."
    )
    audit.add_argument(
        "--codex-home",
        type=Path,
        default=_default_codex_home(),
        help="Codex state root (default: CODEX_HOME or ~/.codex).",
    )
    audit.add_argument(
        "--out",
        type=Path,
        help=(
            "Exact new report directory (default: "
            "~/CodexHistoryAudits/audit-<UTC timestamp>)."
        ),
    )
    audit.add_argument(
        "--require-stable",
        action="store_true",
        help="Return exit code 5 if history changes during the audit.",
    )
    audit.add_argument(
        "--include-titles",
        action="store_true",
        help="Include potentially sensitive thread titles in audit.json.",
    )
    audit.add_argument(
        "--include-cwd",
        action="store_true",
        help="Include potentially sensitive working-directory paths in audit.json.",
    )
    audit.add_argument(
        "--automated-originator",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Treat an exact custom originator name as automation; repeat for "
            "multiple orchestrators. Names are stored only in the private audit."
        ),
    )

    export = commands.add_parser(
        "export",
        help="Create a private, verified migration bundle of interactive history.",
        description=(
            "Copy meaningful interactive rollout files into a private migration "
            "bundle without modifying CODEX_HOME. SQLite, auth, and config are "
            "not exported."
        ),
    )
    export.add_argument(
        "--from-audit",
        type=Path,
        help=(
            "Completed stable audit report directory to consume. When omitted, "
            "use the newest report under CODEX_HISTORY_AUDIT_DIR or "
            "~/CodexHistoryAudits."
        ),
    )
    export.add_argument(
        "--include-empty-shells",
        action="store_true",
        help="Also export interactive startup shells that contain no user prompt.",
    )
    export.add_argument(
        "--codex-home",
        type=Path,
        default=_default_codex_home(),
        help="Codex state root (default: CODEX_HOME or ~/.codex).",
    )
    export.add_argument(
        "--out",
        type=Path,
        help=(
            "Exact new private bundle directory (default: "
            "~/CodexHistoryExports/export-<UTC>-<suffix>)."
        ),
    )

    archive = commands.add_parser(
        "archive",
        help="Plan, apply, or resume verified archives through the official Codex CLI.",
    )
    archive_commands = archive.add_subparsers(dest="archive_command", required=True)

    plan = archive_commands.add_parser(
        "plan", help="Create a private immutable archive plan without changing history."
    )
    plan.add_argument(
        "--from-audit",
        type=Path,
        help=(
            "Completed stable audit report directory to consume. When omitted, "
            "use the newest report under CODEX_HISTORY_AUDIT_DIR or "
            "~/CodexHistoryAudits."
        ),
    )
    plan.add_argument(
        "--include",
        action="append",
        choices=sorted(ALLOWED_SELECTIONS),
        required=True,
        help=(
            "Selection to include; repeat as needed. 'automated' already includes "
            "guardian threads."
        ),
    )
    plan.add_argument(
        "--limit",
        type=int,
        help="Plan only the first N eligible threads, useful for a pilot.",
    )
    plan.add_argument(
        "--codex-home",
        type=Path,
        default=_default_codex_home(),
        help="Codex state root (default: CODEX_HOME or ~/.codex).",
    )
    plan.add_argument(
        "--out",
        type=Path,
        help="Exact new private plan directory.",
    )

    apply_command = archive_commands.add_parser(
        "apply", help="Execute an immutable plan with per-thread verification."
    )
    apply_command.add_argument("--plan", type=Path, required=True)
    apply_command.add_argument(
        "--confirm-plan",
        required=True,
        help="Confirmation token printed by archive plan.",
    )
    apply_command.add_argument(
        "--codex-home",
        type=Path,
        default=_default_codex_home(),
        help="Codex state root (default: CODEX_HOME or ~/.codex).",
    )
    apply_command.add_argument("--out", type=Path, help="Exact new private run directory.")
    apply_command.add_argument(
        "--codex-bin",
        default="codex",
        help="Codex CLI executable (default: codex from PATH).",
    )
    apply_command.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Maximum seconds for each Codex archive command (default: 60).",
    )

    resume = archive_commands.add_parser(
        "resume", help="Safely continue an interrupted archive run."
    )
    resume.add_argument("--run", type=Path, required=True)
    resume.add_argument(
        "--codex-home",
        type=Path,
        default=_default_codex_home(),
        help="Codex state root (default: CODEX_HOME or ~/.codex).",
    )
    resume.add_argument(
        "--codex-bin",
        default="codex",
        help="Codex CLI executable (default: codex from PATH).",
    )
    resume.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Maximum seconds for each Codex archive command (default: 60).",
    )
    return parser


def _run_audit_command(args: argparse.Namespace) -> int:
    codex_home = args.codex_home.expanduser().resolve()
    if not codex_home.is_dir():
        print("error: CODEX_HOME is not a directory: %s" % codex_home, file=sys.stderr)
        return 3

    output_path = (
        args.out.expanduser()
        if args.out
        else default_output_path(_default_audit_root())
    )
    output_path = output_path.resolve()
    if _is_within(output_path, codex_home):
        print("error: report output cannot be inside CODEX_HOME", file=sys.stderr)
        return 3
    git_worktree = _containing_git_worktree(output_path)
    if git_worktree is not None:
        print(
            "error: report output cannot be inside a Git worktree: %s" % git_worktree,
            file=sys.stderr,
        )
        return 3

    print("Auditing %s (read-only)..." % codex_home, file=sys.stderr)
    try:
        audit = run_audit(
            codex_home,
            args.include_titles,
            args.include_cwd,
            args.automated_originator,
        )
    except ValueError as exc:
        print("error: invalid automated originator: %s" % exc, file=sys.stderr)
        return 3
    try:
        publish_reports(audit, output_path)
    except (OSError, ValueError) as exc:
        print("error: could not publish report: %s" % exc, file=sys.stderr)
        return 3

    print(render_summary(audit, output_path), end="")
    if audit["source"]["database_error"]:
        return 4
    if args.require_stable and not audit["run"]["stable"]:
        return 5
    return 0


def _resolved_codex_home(path: Path) -> Optional[Path]:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        print("error: CODEX_HOME is not a directory: %s" % resolved, file=sys.stderr)
        return None
    return resolved


def _private_artifact_location_error(path: Path, codex_home: Path) -> Optional[str]:
    resolved = path.expanduser().resolve()
    if _is_within(resolved, codex_home):
        return "private artifacts cannot be inside CODEX_HOME"
    git_worktree = _containing_git_worktree(resolved)
    if git_worktree is not None:
        return "private artifacts cannot be inside a Git worktree: %s" % git_worktree
    return None


def _archive_progress(index: int, total: int, _thread_id: str, status: str) -> None:
    if index == 1 or index == total or index % 10 == 0:
        print(
            "Verified archive progress: %s/%s (%s)" % (index, total, status),
            file=sys.stderr,
        )


def _export_progress(index: int, total: int, _relative_path: str) -> None:
    if index == 1 or index == total or index % 10 == 0:
        print("Verified export progress: %s/%s" % (index, total), file=sys.stderr)


def _run_export_command(args: argparse.Namespace) -> int:
    codex_home = _resolved_codex_home(args.codex_home)
    if codex_home is None:
        return 3
    try:
        report_directory = (
            args.from_audit.expanduser()
            if args.from_audit
            else _latest_audit_report(_default_audit_root())
        )
    except ArchiveError as exc:
        print("error: could not select an audit report: %s" % exc, file=sys.stderr)
        return 6
    location_error = _private_artifact_location_error(report_directory, codex_home)
    if location_error:
        print("error: %s" % location_error, file=sys.stderr)
        return 3
    output_path = (
        args.out.expanduser()
        if args.out
        else default_export_path(_default_export_root())
    ).resolve()
    location_error = _private_artifact_location_error(output_path, codex_home)
    if location_error:
        print("error: %s" % location_error, file=sys.stderr)
        return 3
    try:
        print("Using audit report: %s" % report_directory, file=sys.stderr)
        manifest = export_interactive_history(
            report_directory,
            codex_home,
            output_path,
            include_empty_shells=args.include_empty_shells,
            progress=_export_progress,
        )
    except ExportError as exc:
        print("error: export refused unsafe state: %s" % exc, file=sys.stderr)
        return 6
    except (OSError, ValueError) as exc:
        print("error: could not create export bundle: %s" % exc, file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("Export interrupted; no incomplete bundle was published.", file=sys.stderr)
        return 130

    summary = manifest["summary"]
    print(
        "Export complete: %s conversations, %s bytes."
        % (summary["conversation_count"], summary["payload_bytes"])
    )
    print("Bundle directory: %s" % output_path)
    print("Read RESTORE.md before moving this private bundle to another Mac.")
    return 0


def _run_archive_plan_command(args: argparse.Namespace) -> int:
    codex_home = _resolved_codex_home(args.codex_home)
    if codex_home is None:
        return 3
    try:
        report_directory = (
            args.from_audit.expanduser()
            if args.from_audit
            else _latest_audit_report(_default_audit_root())
        )
    except ArchiveError as exc:
        print("error: could not select an audit report: %s" % exc, file=sys.stderr)
        return 6
    location_error = _private_artifact_location_error(report_directory, codex_home)
    if location_error:
        print("error: %s" % location_error, file=sys.stderr)
        return 3
    output_path = (
        args.out.expanduser()
        if args.out
        else default_plan_path(_default_archive_root())
    ).resolve()
    location_error = _private_artifact_location_error(output_path, codex_home)
    if location_error:
        print("error: %s" % location_error, file=sys.stderr)
        return 3
    try:
        print("Using audit report: %s" % report_directory, file=sys.stderr)
        plan = build_archive_plan(
            report_directory,
            codex_home,
            args.include,
            limit=args.limit,
        )
        publish_archive_plan(plan, output_path)
    except ArchiveError as exc:
        print("error: could not create safe archive plan: %s" % exc, file=sys.stderr)
        return 6
    except (OSError, ValueError) as exc:
        print("error: could not publish archive plan: %s" % exc, file=sys.stderr)
        return 3

    print(render_plan_summary(plan), end="")
    print("Plan directory: %s" % output_path)
    print("")
    print("To apply this exact plan:")
    print(
        "  codex-history archive apply --plan %s --confirm-plan %s --codex-home %s"
        % (
            shlex.quote(str(output_path)),
            plan["confirmation_token"],
            shlex.quote(str(codex_home)),
        )
    )
    return 0


def _run_archive_apply_command(args: argparse.Namespace) -> int:
    codex_home = _resolved_codex_home(args.codex_home)
    if codex_home is None:
        return 3
    plan_directory = args.plan.expanduser()
    location_error = _private_artifact_location_error(plan_directory, codex_home)
    if location_error:
        print("error: %s" % location_error, file=sys.stderr)
        return 3
    output_path = (
        args.out.expanduser()
        if args.out
        else default_run_path(_default_archive_root())
    ).resolve()
    location_error = _private_artifact_location_error(output_path, codex_home)
    if location_error:
        print("error: %s" % location_error, file=sys.stderr)
        return 3
    try:
        plan = load_verified_plan(plan_directory)
        if args.confirm_plan != plan["confirmation_token"]:
            print(
                "error: confirmation token does not match the immutable plan",
                file=sys.stderr,
            )
            return 6
        run_directory = create_archive_run(plan, codex_home, output_path)
        print("Archive run directory: %s" % run_directory, file=sys.stderr)
        print(
            "Archiving %s verified threads sequentially..." % len(plan["targets"]),
            file=sys.stderr,
        )
        result = execute_archive_run(
            run_directory,
            codex_home,
            codex_binary=args.codex_bin,
            timeout=args.timeout,
            progress=_archive_progress,
        )
    except ArchiveOperationStopped as exc:
        print("error: %s" % exc, file=sys.stderr)
        print(
            "This run is intentionally not auto-resumable. Create a fresh stable "
            "audit and plan; completed archives will be excluded.",
            file=sys.stderr,
        )
        print("The private run journal contains the verified stopping point.", file=sys.stderr)
        return 7
    except ArchiveError as exc:
        print("error: archive apply refused unsafe state: %s" % exc, file=sys.stderr)
        return 6
    except (OSError, ValueError) as exc:
        print("error: could not create or write archive run: %s" % exc, file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("Interrupted; use archive resume with the printed run directory.", file=sys.stderr)
        return 130

    print("Archive complete: %s verified successes." % result["verified_success_count"])
    print("Run directory: %s" % run_directory)
    return 0


def _run_archive_resume_command(args: argparse.Namespace) -> int:
    codex_home = _resolved_codex_home(args.codex_home)
    if codex_home is None:
        return 3
    run_directory = args.run.expanduser()
    location_error = _private_artifact_location_error(run_directory, codex_home)
    if location_error:
        print("error: %s" % location_error, file=sys.stderr)
        return 3
    try:
        result = execute_archive_run(
            run_directory,
            codex_home,
            codex_binary=args.codex_bin,
            timeout=args.timeout,
            progress=_archive_progress,
            resume=True,
        )
    except ArchiveOperationStopped as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 7
    except ArchiveError as exc:
        print("error: archive resume refused unsafe state: %s" % exc, file=sys.stderr)
        return 6
    except (OSError, ValueError) as exc:
        print("error: could not read or update archive run: %s" % exc, file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("Archive run remains safely resumable.", file=sys.stderr)
        return 130
    print("Archive complete: %s verified successes." % result["verified_success_count"])
    print("Run directory: %s" % run_directory)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> None:
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "audit":
        raise SystemExit(_run_audit_command(args))
    if args.command == "export":
        raise SystemExit(_run_export_command(args))
    if args.command == "archive" and args.archive_command == "plan":
        raise SystemExit(_run_archive_plan_command(args))
    if args.command == "archive" and args.archive_command == "apply":
        raise SystemExit(_run_archive_apply_command(args))
    if args.command == "archive" and args.archive_command == "resume":
        raise SystemExit(_run_archive_resume_command(args))
    parser.error("unknown command")
