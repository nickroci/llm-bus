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

Follow the repository [installation guide](https://github.com/wnz99/llm-bus#install-and-verify).
CLI installation, host hooks, shared skill copying and standing instructions are
separate actions. Discover the shared skill directory (normally
`~/.agents/skills/llm-bus/`) and copy the whole skill directory. Install the
official Beads skill separately; optional caveman separately. Ask project/global
instruction destination before edits, then follow the
[agent instruction install guide](https://github.com/wnz99/llm-bus/blob/main/examples/agent-instructions.md). Preserve settings; never auto-trust hooks
or relax host/remote permissions. Install hooks per machine and custom host home,
restart hosts, and let the owner review/trust native hooks.

Before removing recovery tooling, revoke relevant grants/projects on each
machine. `llm-bus uninstall` removes bus hooks, Claude allow rule and uv tool;
mailbox, grants and native-home bindings remain. Remove copied skills, rule files
and pointer blocks separately only on owner request. Stop bridges and back up before client rollback:
schema 16 mailboxes reject clients that support only earlier schemas, and old
transports cannot carry directions. This version upgrades the mailbox to
schema version 16 on database access, adding local silence settings, clocks
and timer claims without launching helpers during migration. No relay upgrade
is required for this feature. Older binaries then refuse the mailbox, so
upgrade every llm-bus installation that shares the mailbox (`uv run llm-bus
upgrade` from a checkout) before any session uses it.
There is no supported in-place downgrade: an older binary requires restoring
a pre-upgrade backup with explicit owner approval. Never lower `user_version`
or delete retained state to bypass schema validation.
