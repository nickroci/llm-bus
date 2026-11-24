# Project coordination guidance index

The coordination rules live in the files below, split by role and capability.
The llm-bus CLI and hooks print the files that apply to a session in
`guidance_files`; the skill's `SKILL.md` dispatch says when to load them. Files
sit under `references/coordination/` and are loaded relative to this file.

A full read of this reference means every file in the table, in order. Use it
when the installed skill and the CLI disagree, when a named file is missing, or
when guidance output does not name files. `core.md` starts with a
guidance-version marker that must match the CLI's `guidance_version`.

| File | Load when |
| --- | --- |
| [coordination/core.md](coordination/core.md) | Always, first, in every role and state. |
| [coordination/coordinator.md](coordination/coordinator.md) | Active coordinator, and before `coordinate`. |
| [coordination/worker.md](coordination/worker.md) | Active worker, and before `join`. |
| [coordination/gates.md](coordination/gates.md) | Gates are delegated or bound. |
| [coordination/auto-merge.md](coordination/auto-merge.md) | `auto-merge` is delegated or bound. |
| [coordination/owner-decisions.md](coordination/owner-decisions.md) | Owner decisions or decision defaults are delegated or bound. |
| [coordination/remote-coordination.md](coordination/remote-coordination.md) | A worker's coordinator is remote, a coordinator has a configured peer, or a receiver `setup` import. |
| [coordination/memory-snapshot.md](coordination/memory-snapshot.md) | Before a coordinator rewrites the memory ticket (named by `coordinator.md`). |
| [coordination/recovery.md](coordination/recovery.md) | Inactive state, recovery, and before `replace`, `revoke` or `leave`. |

## Installation and rollback

Follow the repository [installation guide](https://github.com/wnz99/llm-bus/blob/main/docs/installation.md).
Managed installation verifies the CLI, hooks and whole skill directory from one
accepted revision. Record the real shared skill parent (normally
`~/.agents/skills/`); later upgrade/reinstall refresh it. Install the
official Beads skill separately; optional caveman separately. Ask project/global
instruction destination before edits, then follow the
[agent instruction install guide](https://github.com/wnz99/llm-bus/blob/main/examples/agent-instructions.md). Preserve settings; never auto-trust hooks
or relax host/remote permissions. Configure each machine and custom host home;
restart individual hosts when needed to load/trust changed hooks, after release.

Before removing recovery tooling, revoke relevant grants/projects on each
machine. Managed `llm-bus uninstall` removes owned launchers, uv registrations,
hooks, the Claude allow rule and the verified skill copy. Mailbox, grants,
native-home bindings and held recovery evidence remain. Remove rule files and
pointer blocks separately only on owner request. Upgrade every installation
sharing a mailbox through the local maintenance hold before activation.
Current schema is 21; older binaries must stay off activated mailboxes.
Schema 17 added unattended run history and schema 18 added decision-to-run
bindings and handoff lineage; migration grants no unattended authority.
The [offline retention cutover](https://github.com/wnz99/llm-bus/blob/main/docs/remote-bus.md#offline-retention-cutover)
owns relay drain, backup, migration, first-deletion approval and replay/high-water
reconciliation. There is no supported in-place downgrade. Restore backups only
offline with separate owner approval, then apply compatible migrations and
reconciliation before release. Never lower `user_version` or delete retained
state to bypass compatibility validation.
