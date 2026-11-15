# Installation and maintenance

The [README quick start](../README.md#get-started) covers a first local message.
This guide covers host setup, upgrades, permissions, and removal.

## Install and verify

Use macOS or Linux, Python 3.12, and [uv](https://docs.astral.sh/uv/). Install
separately on each machine, as the operating-system user who runs the agents.
Close both agents, then run these commands from a clone of this repository:

```sh
uv tool install .
llm-bus install
```

For optional [collaboration](collaboration.md), install the extra instead with
`uv tool install --force '.[collab]'`. If your shell cannot find the CLI, run
`uv tool update-shell` and open a new shell. `uv tool dir --bin` shows its
executable directory without assuming a home directory or package-manager prefix.

The first command installs the CLI; the second adds user-level `SessionStart`,
`UserPromptSubmit`, and `SessionEnd` hooks for both hosts, plus a Claude Code allow
rule for direct bus commands. Neither copies skills, enables coordination,
initializes Beads, or edits standing agent instructions.

Avoid simultaneous edits to host settings. The installer preserves unrelated
settings but cannot coordinate with another writer. It rejects symlinked
settings files; use your dotfile manager to edit their targets.

| Host | Default hook file | Custom configuration directory |
| --- | --- | --- |
| Codex | `~/.codex/hooks.json` | Set `CODEX_HOME` before installation and before starting Codex. |
| Claude Code | `~/.claude/settings.json` | Set `CLAUDE_CONFIG_DIR` before installation and before starting Claude Code. |

The installer discovers `llm-bus` on its process's `PATH` and writes an absolute,
shell-quoted executable path. These paths belong to that machine; do not copy
generated hook files between machines or commit them as shared configuration.
Reinstall after an upgrade, executable move, or configuration-directory change.
This also migrates older bare `llm-bus hook ...` commands without duplicating bus
hooks. Remove older project-level copies to avoid duplicate notifications.

Restart the agents. In Codex, open `/hooks` and review and trust the bus hooks;
untrusted hooks do not run. Hooks are enabled by default; `hooks = false` under
`[features]` disables them. Project-layer hooks also require a trusted project.
Reinstallation preserves other tools' handlers, but moves bus handlers out of
groups with a `matcher` or other extra keys and removes duplicate bus-only groups.
These changes, and uninstalling, can shift group positions. Re-trust any hook
that `/hooks` marks modified. See the [Codex](https://learn.chatgpt.com/docs/hooks)
and [Claude Code](https://code.claude.com/docs/en/hooks) hook guides.

Inside each main agent's shell tool, run `llm-bus whoami`. It returns an address
such as `codex:SESSION_ID` or `claude:SESSION_ID`; use the actual address when
sending. Claude registers at launch; a new Codex session may register only on
its first turn.

### Custom Codex homes

Installation records each selected Codex home in
`~/.local/share/llm-bus/native-hosts.json`. All callers, including Claude and
bridges, use these machine-owned bindings to identify Codex children regardless
of the caller's `CODEX_HOME`. Run `install` for every Codex home you use. Adding
a home retains previous bindings; uninstalling retains them too. Before first
installation, lookup uses the calling process's Codex home. Complete custom-home
setup before enabling mixed-host messaging.

### Executable lookup and hook errors

GUI apps, services, SSH sessions, and terminals can inherit different environments.
Installed hooks use absolute paths, but agent shell commands still need the tool
directory on `PATH`, and Codex wakes need `codex` on the sender's `PATH`.
Configure and restart the process that launches the agent or service; changing
`.zshrc` or `.bashrc` alone may not affect it.

For hook exit code 127, inspect the active hook file. `command -v llm-bus`
should identify the intended installation. Reinstall from that environment,
then review changed Codex hooks in `/hooks`.

To check executable lookup without registering a fake session or sending a
message, append `--help` to the installed hook command, preserve its executable
quoting, and temporarily set `PATH=/usr/bin:/bin`. Exit code 0 proves only that
the command starts. Verify lifecycle hooks, identity, and wake permissions in a
real new session. Empty JSON is not a valid hook payload.

For Claude inbound settings and other wake requirements, see
[Wake behavior](local-messaging.md#wake-behavior).

## Upgrade

From a checkout with no uncommitted tracked changes:

```sh
uv run llm-bus upgrade
```

The command builds a wheel from `HEAD`, installs it with the currently installed
extras and the checkout's pinned Python version, and verifies the installation.
After successful verification it deletes all older folders under
`~/.local/share/llm-bus/packages/` and everything in
`~/.local/state/llm-bus/releases/`; it keeps no old versions. Run it from a
checkout outside those folders. It does not touch `backups/`, `deploy-backups/`,
or other bus state. On installation or verification failure, it deletes nothing,
reinstalls the previous wheel, and exits with status 1.

Add `--skills-dir DIR` to update the installed skill from the same `HEAD`, for
example `uv run llm-bus upgrade --skills-dir ~/.agents/skills`. The directory
must already exist. After CLI verification and before pruning, this replaces
`DIR/llm-bus`; local skill edits are lost and untracked checkout files are not
copied. A symlink or file at `DIR/llm-bus` is refused: pass the directory holding
the real copy. If skill replacement fails, the new CLI remains installed and
nothing is pruned. Without the option the installed skill stays unchanged.
Rule files are never copied; update them yourself.

The command does not reinstall hooks or manage bridges. Close both agents,
run `llm-bus install`, then restart them. Stop any running bridge before upgrading
and start it afterwards, or restart it afterwards and check its log, using your
machine's supervisor.

### First upgrade from a directory install

Rollback requires a previous wheel, so a first install or `uv tool install .`
needs this one-time bootstrap. Omit `[collab]` if you do not use that extra:

```sh
uv build --wheel --out-dir ~/.local/share/llm-bus/packages/bootstrap
uv tool install --force --python 3.12 "$(ls ~/.local/share/llm-bus/packages/bootstrap/llm_bus-*.whl)[collab]"
```

The next successful upgrade deletes `bootstrap/`.

### Mailbox compatibility and rollback

Current clients upgrade the mailbox to schema 16. Binaries supporting earlier
schemas then refuse it, so upgrade every installation sharing that mailbox
before any session uses it. Upgrade executable, hooks, copied skill, and rule
files together; mixed versions can disagree. Migration does not start timers.

There is no supported in-place schema downgrade. Older code requires an
owner-approved restoration of a compatible pre-upgrade mailbox backup. Stop
bridges and retain backups before rollback; never resend directions as ordinary
messages to bypass compatibility checks. See
[coordination installation and rollback](../skills/llm-bus/references/coordination.md#installation-and-rollback)
and [relay migration and rollback](remote-bus.md#migration-and-rollback).

## Help agents discover the bus

Hooks provide identity and pending-message counts. Standing instructions tell
agents when to use the bus. Follow [Install agent instructions](../examples/agent-instructions.md)
to choose project or global scope, install a short always-loaded pointer block,
and place rule files where agents can load them on demand. Preserve existing
instructions and replace older bus sections instead of appending duplicates.
An agent doing this setup must ask you to choose the scope before editing.

Copy the entire [llm-bus skill](../skills/llm-bus/SKILL.md) directory into the
host's shared skills directory, normally `~/.agents/skills/llm-bus/`, or use
`upgrade --skills-dir` from a checkout. The skill is optional for local messaging
and required for remote messaging, collaboration, and coordination. Copying it
or the rules installs no hooks, Beads database, or trust changes. Coordination
also needs the Beads skill and an existing Beads workspace; the instruction
guide lists those prerequisites and optional caveman setup.

Only main agents use the bus; sub-agents report through their parent. Start a
new session in the chosen scope and confirm `llm-bus whoami` succeeds. Configure
each machine separately and never copy another machine's executable paths or
session IDs. See [Codex instruction loading](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
and [Claude Code memory](https://code.claude.com/docs/en/memory#agents-md).

SessionStart and missing or outdated guidance state emit full guidance. Normal
prompt hooks give a compact reminder with the folder, pending count (capped at
`100+`), inbox/ack hints, and peer restrictions. Full remote details appear only
for a configured repository. Both hosts fire SessionStart after compaction
(Codex can also fire it mid-turn). Run `llm-bus guidance` for manual recovery;
it does not change messages or guidance-version state.

## Storage and permissions

Sessions running as one operating-system user share a SQLite mailbox. Its
default path is `~/.local/share/llm-bus/bus.sqlite3` on macOS and Linux. Windows
uses `LOCALAPPDATA/llm-bus/bus.sqlite3`, falling back to `AppData/Local` under the
user profile. Set `LLM_BUS_DB` to use another path. The installed CLI works from
any folder.

Schema upgrades are additive and transactional; current mailboxes skip migration
work. Inbox reads scan bounded pages from the oldest pending message, hide
remote messages denied by current policy, and retain them in history. Transport
updates preserve delivery and acknowledgement progress across out-of-order bridge
responses; delivery takes precedence over unreceived expiry.

Claude's installed allow rule covers direct bus commands. Pipelines and other
commands may still prompt, and managed or explicit deny rules can override it.
See [Claude Code permissions](https://code.claude.com/docs/en/permissions).

For Codex, an allow rule in `~/.codex/rules/*.rules` can use
`prefix_rule(pattern=["llm-bus"], decision="allow")`. Without that rule or a
`writable_roots` entry for the mailbox directory in `[sandbox_workspace_write]`
in `config.toml`, the sandbox blocks agent-run writes under `~/.local/share`.
`codex --ask-for-approval never` suppresses prompts for the whole session, not
just bus commands, and does not remove sandbox limits. Use it only if that
broader permission is acceptable.

Message text and transport do not grant execution permission. Optional
directions remain subject to owner grants and host permissions. Hooks report
pending counts but do not read or acknowledge messages for agents.

Coordination records, grants, enrollments, targets, and direction metadata stay
in the machine-local mailbox. Processes sharing one OS user can read that state;
it is not an isolation boundary against that account. Revocation stops future
guarded exposure and wakes, retains history, and cannot recall exposed text or
an already submitted wake.

## Uninstall

Revoke active coordination grants and projects on each machine before removing
recovery tooling. Then run `llm-bus uninstall`.

This removes user-level bus hooks, the Claude allow rule, and the uv tool.
The mailbox, pending messages, coordination records, and native-home bindings
remain. Copied skills, rule files, and pointer blocks need separate removal
at the owner's request. For downgrades, follow the rollback guidance above.
