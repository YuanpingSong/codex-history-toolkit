# Move interactive history to another Mac

Codex History Toolkit can create a private, file-level export of meaningful
interactive conversations. The bundle is intended for backup or for seeding a
fresh Codex home on another machine.

This is a best-effort migration of undocumented local files. It is not a
ChatGPT account transfer, a supported cross-account import, or a guaranteed
sidebar/index repair. Use the same ChatGPT account and a compatible app version
when possible, and keep both the source history and export bundle until you
have verified the destination.

This is deliberately separate from archiving. Archiving changes a thread's
local archive state. Exporting copies selected rollout files without changing
the source machine.

## What the bundle contains

By default, `codex-history export` includes meaningful interactive rollouts
from both `sessions/` and `archived_sessions/`. Add `--include-empty-shells` if
you also want interactive startup sessions that never received a persisted
user message.

The bundle includes:

- byte-for-byte copies of selected rollout JSONL files;
- their relative `sessions/` or `archived_sessions/` layout;
- hashes and a machine-readable manifest;
- a human-readable summary and restoration notes; and
- private-artifact and completion sentinels.

It does **not** include:

- automated or ambiguous threads;
- `state_N.sqlite` or any other Codex database;
- authentication, account, or configuration files;
- cloud tasks stored by the service; or
- Git repositories and working-directory contents referenced by a thread.

Conversation rollouts contain prompts, responses, tool output, and paths. Treat
the entire bundle as sensitive.

## Create the export

Fully quit ChatGPT/Codex and stop automated agents, then run:

```sh
codex-history audit --require-stable
codex-history export
```

The export command automatically uses the newest completed audit unless
`--from-audit` selects another one. It revalidates the audit against the live
Codex home and stops if a selected file changed.

The default destination is a new private directory under
`~/CodexHistoryExports/`. Use `--out` to choose another location outside both
`CODEX_HOME` and every Git worktree.

## Transfer it privately

Use encrypted storage or an end-to-end encrypted sync service. Do not put the
bundle in a source repository, public cloud folder, issue attachment, or CI
artifact. Keep the original bundle until the new machine has been verified.

## Restore on a fresh Mac

The safest restoration point is **before the first ChatGPT/Codex launch** on
the new Mac, while the destination Codex home has no existing session catalog.
The toolkit intentionally does not rewrite or merge Codex's internal SQLite
database.

1. Install ChatGPT/Codex, but do not launch it yet. If it is running, fully quit
   it.
2. Copy the export bundle to the new Mac and read its `RESTORE.md` and summary.
3. Set the destination if you use a non-default Codex home:

   ```sh
   export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
   umask 077
   install -d -m 700 "$CODEX_HOME/sessions" "$CODEX_HOME/archived_sessions"
   ```

4. With `BUNDLE` pointing at the exported directory, copy only the preserved
   rollout trees:

   ```sh
   BUNDLE="$HOME/CodexHistoryExports/export-YYYYMMDDTHHMMSSZ-1234abcd"
   rsync -a --ignore-existing "$BUNDLE/sessions/" "$CODEX_HOME/sessions/"
   rsync -a --ignore-existing "$BUNDLE/archived_sessions/" "$CODEX_HOME/archived_sessions/"
   ```

   If either source directory is absent, skip that command. On a genuinely
   fresh destination, `--ignore-existing` should not skip anything.

5. Launch ChatGPT/Codex and allow its first local-history scan to finish.

Archived rollouts remain archived. Active interactive rollouts are eligible
for the normal local task list. Service-hosted cloud tasks are account data and
are not created by this process.

## Existing Codex homes

Merging into an already initialized `CODEX_HOME` is version-sensitive. A
filename collision may represent different data, and an existing state
database may already have completed its historical backfill. The export
command therefore creates a portable bundle but does not offer an automatic
database import command.

For an existing destination:

1. back up the destination Codex home;
2. fully quit ChatGPT/Codex;
3. compare all colliding rollout files rather than overwriting them; and
4. retain the original bundle until every desired conversation is visible and
   readable.

If the app does not discover restored files, do not hand-edit its database.
Keep the bundle and use a fresh Codex home or a version-aware migration tool.
