# Classification model

Classification is conservative because it can feed an archive plan. A novel
or partially understood source stays `ambiguous`; the toolkit does not guess
that it is safe to archive.

Rules are versioned and use the first `session_meta` record in each rollout.
SQLite is used to reconcile storage and archive state, not to infer intent from
message content.

## Origin classes

### Interactive

The built-in interactive rules recognize current Codex surfaces:

| Surface | Required metadata |
| --- | --- |
| Codex desktop app | `originator="Codex Desktop"` and string source `vscode` |
| VS Code | `originator="codex_vscode"` and string source `vscode` |
| CLI | `originator="codex_cli_rs"` and string source `cli` |
| TUI | `originator="codex-tui"` and string source `cli` |

An interactive rollout is `meaningful` only when it contains a top-level
`event_msg` whose payload type is `user_message`. A structurally valid,
unchanged interactive rollout without such an event is an `empty_shell`.
Injected environment context and `AGENTS.md` records do not count as user
messages.

### Automated

The built-in automated rules recognize structural signals rather than local
project names:

| Surface | Required metadata |
| --- | --- |
| Non-interactive execution | string source `exec` |
| Spawned subagent | the complete supported `subagent.thread_spawn` object shape |
| Guardian | the complete supported guardian source object |
| Configured automation | exact originator supplied with `--automated-originator`; recognized structural object sources still take precedence |

Guardian and spawned-subagent records are included in the `automated` origin
class while retaining separate surface counts.

In audit summaries, `guardian` means a structurally recognized Codex helper
thread and `subagent` means a thread spawned from another Codex thread. `tui`
is the terminal user interface. These surface labels describe provenance; they
are not extra origin classes.

The meaningful-versus-empty test only applies to interactive records.
Automated and ambiguous records therefore show `activity_state=not_evaluated`:
the toolkit is intentionally declining to infer their value from message
content or the absence of a user event.

### Ambiguous

Anything that does not match a complete versioned rule remains `ambiguous`.
Examples include unknown originators, new object shapes, partial spawn metadata,
and contradictory originator/source combinations.

## Custom orchestrators

Project-specific orchestrator names are not built into the public classifier.
To classify a known local orchestrator explicitly, repeat the option as needed:

```sh
codex-history audit --require-stable \
  --automated-originator my-local-orchestrator \
  --automated-originator another-known-runner
```

Matching is normalized and exact; it is not a substring or regular-expression
match. Configured names are recorded in the private audit so later archive and
export operations can reproduce the same classification. Because those labels
may themselves be sensitive, do not publish the audit directory.

An archive plan reuses the classification configuration from its source audit
when it performs its fresh safety audit. Changing the configuration requires a
new audit and a new plan.

## Why not infer from activity?

The toolkit deliberately does not infer origin from any of these alone:

- `source=vscode`;
- `thread_source=user`;
- account identity;
- SQLite's `has_user_event`; or
- the absence of user-message events.

Automation can contain user-like messages, and interactive startup shells can
contain injected context. Structural provenance is a safer boundary.

## Updating rules

A Codex update can introduce new source shapes. If previously known threads
become ambiguous, audit first and add a tested classifier rule only after the
new metadata is understood. Do not weaken the ambiguous fallback merely to
recover an expected count.
