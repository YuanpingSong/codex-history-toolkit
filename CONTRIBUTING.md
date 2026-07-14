# Contributing to Codex History Toolkit

Thanks for helping make local Codex history maintenance safer and easier to
understand.

## Development setup

The project supports Python 3.9 and newer and has no third-party runtime
dependencies.

```sh
git clone <your-fork>
cd codex-history-toolkit
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
make test check
```

Install the optional pre-commit safety hook with:

```sh
make install-hooks
```

## Never commit real Codex data

Conversation history can contain personal information, proprietary code,
credentials, filesystem paths, and account identifiers. Do not commit any
copy of a Codex home directory, `auth.json`, a state SQLite database, session
or archived-session directories, rollout JSONL files, `.env` files, or output
from audit, archive, or export operations.

Use small, hand-written synthetic records for tests. Give fixtures explicitly
synthetic names such as `synthetic-rollout.jsonl`, `synthetic-state.sqlite`, or
`synthetic-auth.json`; do not copy a real record and merely redact the fields
you notice. The artifact guard intentionally rejects filenames and directory
layouts that resemble live Codex data.

Before opening a pull request, run:

```sh
make test check
```

Release artifacts should be built in a clean CI environment. Locally generated
source archives can carry the build account name in tar ownership metadata;
never upload an uninspected `dist/` directory. CI validates the source archive
contents and installs both the source and wheel distributions.

The history check scans every local ref, so it can catch private artifacts that
were committed and later deleted. If it reports one, do not push the branch.
Purge the object from Git history and rotate any exposed credential first.

## Change guidelines

- Keep audit operations read-only.
- Make mutating operations explicit, resumable, and independently verifiable.
- Preserve unknown input rather than guessing how to rewrite it.
- Add synthetic regression tests for every behavior change.
- Keep the command-line interface useful without optional dependencies.
- Explain data-loss and privacy risks in user-facing documentation.

By contributing, you agree that your work is licensed under the MIT License.
