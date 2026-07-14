#!/usr/bin/env python3
"""Validate the structure of a Codex History Toolkit source distribution."""

from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import sys
import tarfile
from typing import Optional, Sequence


REQUIRED_PATHS = {
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "docs/assets/codex-history-toolkit-banner.png",
    "docs/classification.md",
    "docs/migration.md",
    "pyproject.toml",
    "scripts/check_sdist.py",
    "src/codex_history/cli.py",
    "src/codex_history/export.py",
}
FORBIDDEN_PARTS = {".git", "__pycache__", "build", "dist"}


def validate_sdist(path: Path) -> None:
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
    if not members:
        raise ValueError("source distribution is empty")

    roots = set()
    relative_paths = set()
    for member in members:
        member_path = PurePosixPath(member.name)
        if member_path.is_absolute() or ".." in member_path.parts:
            raise ValueError("source distribution contains an unsafe path")
        if not member.isfile() and not member.isdir():
            raise ValueError("source distribution contains a non-file member")
        if not member_path.parts:
            continue
        roots.add(member_path.parts[0])
        relative = PurePosixPath(*member_path.parts[1:])
        if any(part.lower() in FORBIDDEN_PARTS for part in relative.parts):
            raise ValueError("source distribution contains generated build data")
        if relative.parts:
            relative_paths.add(relative.as_posix())

    if len(roots) != 1:
        raise ValueError("source distribution must have one top-level directory")
    missing = sorted(REQUIRED_PATHS - relative_paths)
    if missing:
        raise ValueError("source distribution is missing: %s" % ", ".join(missing))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="Path to the .tar.gz sdist")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_sdist(args.archive)
    except (OSError, tarfile.TarError, ValueError) as exc:
        print("source distribution check failed: %s" % exc, file=sys.stderr)
        return 1
    print("Source distribution structure is complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
