# Security policy

Codex History Toolkit works with local conversation records that can contain
prompts, source code, filesystem paths, account metadata, and credentials from
tool output. Treat every Codex history directory and generated operation
directory as private.

## Supported versions

Security fixes are made on the latest released minor version and on the
default branch. Older releases may not receive patches.

## Reporting a vulnerability

Use the repository's private **Report a vulnerability** feature when it is
available. Do not include conversation content, credentials, database copies,
rollout files, or generated reports in a public issue.

If private reporting is not available, open a minimal public issue asking the
maintainers to establish a private contact channel. Describe only the affected
version and the general class of problem.

Please include reproduction steps using synthetic data, the expected safety
property, and the observed behavior. Maintainers will acknowledge a report as
soon as practical and coordinate disclosure after a fix is available.

## If private data was committed

Removing a file in a later commit does not remove it from Git history. Stop
publishing, revoke or rotate exposed credentials, and purge the affected
objects from every published ref and clone before resuming development.
