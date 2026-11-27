# Installation and maintenance

The [README quick start](../README.md#get-started) covers a first local message.
This guide covers host setup, upgrades, permissions, and removal.

## Install and verify

Use macOS or Linux, Python 3.12, and [uv](https://docs.astral.sh/uv/). Install
separately on each machine, as the operating-system user who runs the agents.
Use a clean accepted checkout (`git status --short` must be empty), outside the
managed package and maintenance directories. For a first installation, keep
agents and other mailbox users stopped and install a retained wheel:

```sh
uv build --wheel --out-dir "$HOME/.local/share/llm-bus/packages/bootstrap"
uv tool install --python 3.12 "$HOME/.local/share/llm-bus/packages/bootstrap/llm_bus-0.1.0-py3-none-any.whl"
mkdir -p "$HOME/.agents/skills"
llm-bus install --skills-dir "$HOME/.agents/skills"
```

For optional [collaboration](collaboration.md), append `[collab]` inside the
quoted wheel argument to `uv tool install`. If your shell cannot find the CLI, run
`uv tool update-shell` and open a new shell. `uv tool dir --bin` shows its
executable directory without assuming a home directory or package-manager prefix.

The first `install` invocation prepares stable launchers and exits with
`Stable launcher installed; complete maintenance begin --inventory FILE, then retry`.
This is an incomplete setup, not a completed hold. Follow the
[owner inventory and hold procedure](#owner-inventory-and-hold), then repeat
`llm-bus install --skills-dir "$HOME/.agents/skills"` from the checkout.
The command installs a verified generation, user-level `SessionStart`,
`UserPromptSubmit`, and `SessionEnd` hooks for both hosts, a Claude Code allow
rule, and the skill from the same accepted revision. It remains held until
[compatibility verification and resume](#compatibility-verification-and-resume),
including the proven-absent mailbox initialization procedure below. A mailbox
that already exists follows the existing-mailbox compatibility and cutover rules.
It does not enable coordination,
initialize Beads, or edit standing agent instructions.

Avoid simultaneous edits to host settings. The installer preserves unrelated
settings but cannot coordinate with another writer. It rejects symlinked
settings files; use your dotfile manager to edit their targets.

| Host | Default hook file | Custom configuration directory |
| --- | --- | --- |
| Codex | `~/.codex/hooks.json` | Set `CODEX_HOME` before installation and before starting Codex. |
| Claude Code | `~/.claude/settings.json` | Set `CLAUDE_CONFIG_DIR` before installation and before starting Claude Code. |
| Gemini CLI | `~/.gemini/settings.json` | Set `GEMINI_CLI_HOME` before installation and before starting Gemini CLI. |
| Antigravity (`agy`) | `~/.gemini/config/hooks.json`, plus an allow rule in `~/.gemini/antigravity-cli/settings.json` | None. |

Gemini CLI hooks are installed only when Gemini's `.gemini` folder already
exists; the installer never creates it. If you start using Gemini CLI after
installing the bus, run `reinstall`. Gemini hooks run SessionStart, BeforeAgent
(Gemini's prompt-submission event) and SessionEnd.

Antigravity hooks are installed only when Antigravity has created
`~/.gemini/config`; the allow rule only when it has created
`~/.gemini/antigravity-cli`. The bus owns the `llm-bus` entry of the shared
`hooks.json` and runs it on SessionStart and PreInvocation. Antigravity runs
PreInvocation before every model call; the hook acts only on the first call of
each user turn (`invocationNum` 0) and returns `{}` for the rest. Antigravity
has no session-end event, so an Antigravity session stays listed until its
`agy` process exits or the inactivity cutoff ends it. Resuming a conversation
runs no SessionStart; its first prompt registers it. If you start using
Antigravity after installing the bus, run `reinstall` so it adds the hooks and
allow rule.

Two Antigravity behaviors are unverified. The hooks file sits in
`~/.gemini/config`, outside the CLI's own folder, so the Antigravity app or IDE
may also read it, run the bus hooks and register its sessions as `agy:` peers. Whether Antigravity subagents run hooks under their own
conversation IDs, and so register as separate peers instead of staying off the
bus, is also untested. Check `llm-bus list` after using either.

The installer discovers `llm-bus` on its process's `PATH` and writes an absolute,
shell-quoted executable path. These paths belong to that machine; do not copy
generated hook files between machines or commit them as shared configuration.
Managed upgrades refresh these integrations. Use `reinstall` from the accepted
checkout to repair them or after a configuration-directory change.
This also migrates older bare `llm-bus hook ...` commands without duplicating bus
hooks. Remove older project-level copies to avoid duplicate notifications.

Start the agents after release. In Codex, open `/hooks` and review and trust the bus hooks;
untrusted hooks do not run. Hooks are enabled by default; `hooks = false` under
`[features]` disables them. Project-layer hooks also require a trusted project.
Reinstallation preserves other tools' handlers, but moves bus handlers out of
groups with a `matcher` or other extra keys and removes duplicate bus-only groups.
These changes, and uninstalling, can shift group positions. Re-trust any hook
that `/hooks` marks modified. See the [Codex](https://learn.chatgpt.com/docs/hooks)
and [Claude Code](https://code.claude.com/docs/en/hooks) hook guides.

Inside each main agent's shell tool, run `llm-bus whoami`. It returns an address
such as `codex:SESSION_ID`, `claude:SESSION_ID`, `gemini:SESSION_ID` or
`agy:CONVERSATION_ID`; use the
actual address when sending. Claude and Gemini register at launch; a new Codex
session may register only on its first turn.

Gemini CLI gives its shell tool no session ID. Its SessionStart hook records the
session against the Gemini process, and a bus command run from Gemini's shell
uses the session registered for its nearest Gemini ancestor process. A command
run before that hook has registered the session fails rather than guessing.

Antigravity puts its conversation ID in its shell tools' environment as
`ANTIGRAVITY_CONVERSATION_ID`, the same ID its hooks receive. It wins over a
Claude or Codex session ID that Antigravity inherited from the shell that
started it, unless the nearest host ancestor process is another host.

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

Run operator commands in your terminal from a clean accepted checkout. Managed
entrypoints intercept installation and maintenance commands before application
imports or mailbox access; omit ordinary agent `--format` flags for these
commands. Older installations first need the bootstrap below.

```sh
llm-bus maintenance begin --inventory /absolute/private/access.json
llm-bus upgrade
llm-bus maintenance status
```

The explicit begin keeps the installation held after upgrade, allowing an
extended local or cross-machine cutover. Complete verification and resume below.
From an already active managed installation, `llm-bus upgrade` can establish the
hold using its recorded owner inventory and resume automatically only if every
release check succeeds without new schema evidence. Refresh that inventory with
`begin --inventory FILE` whenever callers, mailboxes or restart sources change.
Check the returned `phase` and `next_step`; command success alone does not mean
dispatch resumed.

Upgrade builds from `HEAD`, preserving installed extras and Python 3.12. It can
reuse a verified matching generation; `llm-bus reinstall` always installs a fresh
generation, including at the same revision. Both refresh hooks and the recorded
real skill destination from that revision. `--skills-dir DIR` explicitly changes
the skill parent; it must exist. A symlink or file at `DIR/llm-bus` is refused.
Local skill edits are replaced. Rule files are never copied.

Generations install directly into immutable final paths under
`~/.local/share/llm-bus/maintenance/generations/`, with normal uv receipts.
Verification covers wheel provenance, payload, extras, interpreter, CLI/module
entrypoints, hooks, skill and mailbox compatibility before publication. It
retains previous artifacts and integration snapshots; there is no automatic
pruning or automatic old-wheel rollback. Failed verification retains the hold.
Never delete journal/launcher files to bypass a failure.

### First upgrade from a directory install

An older installed package cannot gate itself safely. Use the accepted
checkout's standard-library bootstrap with an explicitly selected Python 3.12
interpreter outside managed generations. Do not invoke the old CLI to establish
the hold:

```sh
/absolute/path/to/python3.12 tools/maintenance_upgrade.py begin \
  --receipt /absolute/uv/tools/llm-bus/uv-receipt.toml \
  --skills-dir /absolute/real/skill-parent \
  --alias /absolute/service/path/llm-bus \
  --host-home /absolute/custom/host-home
```

Discover the original tool directory with `uv tool dir`; its receipt is
`llm-bus/uv-receipt.toml` below that directory. Repeat `--alias` for additional
owner-declared service/client paths and `--host-home` for extra host homes;
omit these options when none exist. The bootstrap inventories receipt paths,
PATH matches and effective/remembered hook paths. It preserves and gates those
paths, including symlink aliases, without replacing an unowned symlink target.
Unknown copies, hardlinks, ambiguous or unwritable paths block completion.
Explicitly register supported paths or stop their callers; do not assume a
copied script routes through the gate. Cached hook commands remain gated.

Bootstrap reports `preparing`, which is not proof of stopped mailbox access.
Use `status` or `recover` with this same script and interpreter if bootstrap
is interrupted. Recovery requires the recorded bootstrap runtime bytes; use
the original accepted checkout when a different checkout is rejected.

Generation installation requires the previous receipt to name an existing
wheel. If the old receipt names a directory or its wheel is missing, preserve
the prior source/artifact and stop all its callers/restarts first. Before
bootstrap, build and install a retained wheel of that prior accepted revision
with `uv build --wheel --out-dir DIR` and
`uv tool install --force --python 3.12 /absolute/prior.whl` (append the existing
extras). This ungated legacy conversion requires stopped access. Do not replace
already managed public launchers with `uv tool install --force`.

### Owner inventory and hold

Create an owner-only regular JSON file (mode `0600`); the example is a shape,
not an assertion that your machine has no callers. Include the default mailbox
and every `LLM_BUS_DB` override, even from other services. Paths must be absolute,
unique and not symlinks. Stop or retarget raw imports, `python -m llm_bus`,
direct interpreters, SQLite tools, viewers, bridges, collaboration daemons and
timers, including dormant callers and every restart source. New installed module
entrypoints gate before application imports; old interpreters and raw database
access do not gain that protection.

```json
{
  "version": 1,
  "complete": true,
  "raw_clients_stopped": true,
  "hosts_gated": true,
  "mailboxes": ["/absolute/home/.local/share/llm-bus/bus.sqlite3"],
  "processes": [],
  "restart_sources": [
    {"kind": "assisted", "identity": "owner-bridge", "stopped": true, "restart_disabled": true}
  ]
}
```

Set confirmations true only after checking them. Empty arrays are valid only
when no corresponding callers/sources exist. For a process the controller must
stop, supply exactly `pid`, `started` and `command_sha256`: the PID, `ps -ww -u
UID -o pid=,ppid=,lstart=,command=` start-time fields joined with spaces, and the
SHA-256 of its exact command field without a newline. The controller rechecks
that identity before signalling; never choose processes by substring alone.

Native restart records have exactly `kind` and `identity`: `launchd` with a
literal user launchd label, or `systemd-user` with an exact `.service` unit.
The controller records and suspends supported prior states, then verifies
restoration. Unsupported supervisors, system services, cron jobs and other
callers need the `assisted` record above after the owner stops the caller and
disables every restart source. Identities accept letters, digits, underscores,
periods, `@`, colons and hyphens, cannot start with `-`, and contain no spaces.
No arbitrary shell commands are accepted. Retrying an inventory may add coverage
but cannot discard recorded mailboxes or restart sources.

Run `llm-bus maintenance begin --inventory FILE`. Only `phase: held` establishes
the local barrier. The runtime under `~/.local/share/llm-bus/maintenance/` and
journal under `~/.local/state/llm-bus/maintenance/` are owner-only and have no
automatic expiry. Admitted children are drained; every known SQLite file and
its WAL, shared-memory and journal companions must pass handle inspection.
Missing inspection, unknown access or an uncontrolled restart blocks release.
An empty handle list alone does not prove dormant raw clients cannot reopen.

Agent hosts can stay open only when their captured CLI/hook paths are gated and
raw clients/restarts are controlled. Close or restart individual hosts that
cannot meet that condition, or that need to load/trust changed hooks. Rewriting
settings does not prove a running host reloaded them.

macOS generation checks used uv 0.13.3/Python 3.12.8; Linux generation checks used
uv 0.13.6/Python 3.12.3 with test-only handle fixtures. Native Linux `lsof -nP -F p
-- PATH` on the tested container hosts warned that output might be incomplete
(namespace, tracefs, overlay or FUSE mounts). Production rejects such warnings;
those hosts have no proven native stopped-access barrier. Do not suppress
warnings, substitute `lsof -t`, or treat fixture passes as production clearance.
The repository's uv 0.13.1 quality-tool pin is separate from these layout checks.

During hold, ordinary commands print exactly:

```text
llm-bus is temporarily offline for maintenance. Retry in 1 minute, then every minute while this message appears. Stop retrying once the command responds normally.
```

They exit 75; hooks print the same line and exit 0. Neither sleeps or retries
internally. Agents retry once per minute only while this exact message appears,
then stop retrying when normal responses return. Other errors require diagnosis,
not this retry loop. Maintenance status, reports and monitor data grant no agent
execution authority.

### Compatibility verification and resume

`maintenance status` returns `operation`, `phase`, optional `step` and restart
actions, plus sanitized `active`/`candidate` identities: `id`, `revision`,
`wheel_sha256`, `schema`, `executable`, `receipt`, `tools`, and `bin`. These are
operator diagnostics, not a mailbox or automatic agent prompt.

For each exact recorded mailbox, invoke only the fixed candidate checks while
held:

```sh
llm-bus maintenance preview --mailbox /absolute/path/to/bus.sqlite3
llm-bus maintenance migrate --mailbox /absolute/path/to/bus.sqlite3
llm-bus maintenance verify --mailbox /absolute/path/to/bus.sqlite3
```

Preview is read-only and requires an existing database. For a fresh missing
mailbox, run `migrate`, then `verify`, then `maintenance resume`; omit the initial
preview. Migration constructs the accepted candidate's `Store` without normal
writable activity or purge.
Verification checks integrity, foreign keys and schema. These commands neither
authorize deletion nor replace the [offline retention cutover](remote-bus.md#offline-retention-cutover).
Follow that runbook before migrating existing mailboxes and before their activation.

For unchanged compatible schemas, run `llm-bus maintenance resume`. The same
command releases a proven-absent mailbox initialized by the accepted candidate
in the current operation, without an owner-supplied evidence file. The mailbox
and its `-wal`, `-shm` and `-journal` paths must all have been absent when the held
baseline was captured; dangling symlinks count as present. An existing empty or
schema-0 file does not qualify. Use the exact mailbox path recorded in inventory.

Fresh initialization records intent, reserves the file exclusively, and binds
the completed output to the operation, candidate wheel, exact schema, owned file
identity and digest. It checks integrity and foreign keys and syncs the file and
its immediate parent before recording completion. Release rechecks that proof;
do not create or edit initialization receipts yourself. If existing mailboxes
accompany a fresh mailbox, every changed mailbox must have valid fresh proof.
If the initial client's schema differs from the candidate, every recorded
mailbox must have that proof. A real migration in the set uses the strict
cutover path below.

Before fresh initialization, the controller records `migrate-parent-intent`,
creates any missing parent directories, and syncs every ancestor from the filesystem root to
the immediate parent before initializing the mailbox. This requires directory
access and working directory fsync throughout that chain. An access or filesystem
error retains the hold and leaves the mailbox uninitialized. After resolving
that error, retry `migrate`; it repeats all ancestor syncs even if directories
are already visible. An unfinished initializer receipt or unknown output is a
different failure: it remains held and cannot be accepted merely by retrying or
observing a file. Inspect status and use the recovery procedure below; automatic
recovery from an unverified initialization crash is not promised.

Other schema activation requires `llm-bus maintenance resume --evidence FILE`.
The owner-only mode-0600 JSON file
must contain exactly the following fields, with actual identities from status
and verified mailbox schemas, and literal true booleans:

```json
{
  "operation": "CURRENT-OPERATION-ID",
  "wheel_sha256": "CANDIDATE-WHEEL-SHA256",
  "mailboxes": {"/absolute/path/to/bus.sqlite3": 21},
  "compatible_floor": "ACCEPTED-FULL-GIT-REVISION",
  "local_schema_verified": true,
  "relay_drained": true,
  "first_deletion_approved": true,
  "retention_preview_verified": true,
  "replay_high_water_verified": true
}
```

Include every recorded mailbox. The initial floor is the candidate revision;
later releases retain that floor and must prove ancestry. Unknown compatibility
keeps the hold. Existing-mailbox cutover evidence has no not-applicable form,
including on a machine without a relay. If those statements cannot truthfully
describe completed work and approval, keep the hold and report the unsupported
cutover. The proven-absent initialization path creates no cutover evidence and
asserts no relay drain or deletion approval. Do not fabricate either assertion.
A successful installation, help probe, relay response or bus message cannot
supply these approvals.

Publication reopens dispatch before restart restoration. Check status even when
`phase` is `active`: `step: restart-restore-pending` and
`restart_restore_pending` name outstanding work. Native sources restore recorded
prior states; changed or unsupported state requires owner reconciliation and
another `maintenance resume`. Before a new hold, finish pending restoration.
For assisted sources, manually restore them, save the exact
`restart_restore_evidence` object emitted by status to an owner-only JSON file,
then run `maintenance resume --evidence FILE`. Its separate shape is:

```json
{"operation": "CURRENT-OPERATION-ID", "assisted_restored": ["owner-bridge"]}
```

Use all pending assisted names, sorted without duplicates, with no extra fields.
This acknowledges owner work; it executes no supplied commands. It is distinct
from prepublication cutover evidence.

### Recovery and uv discovery

After an interrupted controller or failed integration, inspect status and repair
from the clean accepted checkout with `llm-bus reinstall`. To recheck a retained
compatible generation, use `llm-bus maintenance recover [--generation ID]`, then
verify and explicitly resume. Without an ID it uses the recorded candidate.
Recovery remains held and never probes an old binary against the real mailbox.
An older artifact without a verified skill copy requires reinstall instead.
The current accepted checkout can replace an unavailable recorded controller
checkout for recovery/resume/uninstall. Bootstrap interruptions use the standalone
script described above. Do not restore backups through this interface.

Default `uv tool list` no longer identifies the selected generation. Copy the
exact `tools` and `bin` paths from `maintenance status` into both overrides:

```sh
UV_TOOL_DIR=/exact/generation/tools UV_TOOL_BIN_DIR=/exact/generation/bin uv tool list
UV_TOOL_DIR=/exact/generation/tools UV_TOOL_BIN_DIR=/exact/generation/bin uv tool dir
UV_TOOL_DIR=/exact/generation/tools UV_TOOL_BIN_DIR=/exact/generation/bin uv tool dir --bin
```

Direct generation removal uses those same overrides with `uv tool uninstall
llm-bus`. It is not the supported whole-installation uninstall: removing the
active receipt makes dispatch fail closed and selects no fallback. Use explicit
reinstall from the accepted checkout to repair it from retained wheel/extras
evidence. Do not manually prune generations, snapshots or bootstrap wheels;
they can still be recovery evidence.

### Mailbox compatibility and rollback

Current clients upgrade the mailbox to schema 21. Binaries supporting earlier
schemas then refuse it, so upgrade every installation sharing that mailbox
before any session uses it. Upgrade executable, hooks, copied skill, and rule
files together; mixed versions can disagree. Schema 17 added local unattended
run history; schema 18 adds decision-to-run bindings and decision handoff lineage.
Migration does not enable unattended authority or start timers. Schema 19 adds
normalized retention ages, replay identifiers, and durable ID allocation.
Schema 20 preserves availability ordering and deduplication after message
deletion. Schema 21 adds the nullable saved relay retention deadline without
rewriting a previously advertised report deadline. Migrations do not purge data.
The next writable operation can begin bounded deletion. Before activating an
existing mailbox, follow the [offline retention cutover](remote-bus.md#offline-retention-cutover),
including local-only hosts and processes with an already-open mailbox.

There is no supported in-place schema downgrade. Keep older code off an activated
mailbox, including during rollback. Restore backups offline only with owner
authorization, reconcile replay identifiers and ID high-water marks, and apply
compatible migrations and current retention before resuming access. Stop bridges
and retain backups before recovery; never resend directions as ordinary messages
to bypass compatibility checks. See
[coordination installation and rollback](../skills/llm-bus/references/coordination.md#installation-and-rollback)
and [relay migration and rollback](remote-bus.md#migration-and-rollback).

## Help agents discover the bus

Hooks provide identity and pending-message counts. Standing instructions tell
agents when to use the bus. Follow [Install agent instructions](../examples/agent-instructions.md)
to choose project or global scope, install a short always-loaded pointer block,
and place rule files where agents can load them on demand. Preserve existing
instructions and replace older bus sections instead of appending duplicates.
An agent doing this setup must ask you to choose the scope before editing.

Managed installation copies the entire [llm-bus skill](../skills/llm-bus/SKILL.md)
directory to the selected real shared skills parent, normally `~/.agents/skills/`.
Upgrade and reinstall refresh that recorded destination; use `--skills-dir` to
override it. The skill is optional for using local messaging
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

Gemini CLI asks before running each shell command unless a rule allows it. The
installer adds none, because Gemini deprecates `tools.allowed`. To allow bus
commands, add a Policy Engine rule such as this to `~/.gemini/policies/llm-bus.toml`:

```toml
[[rule]]
toolName = "run_shell_command"
commandPrefix = "llm-bus"
decision = "allow"
priority = 100
```

See [Gemini CLI policy engine](https://geminicli.com/docs/core/policy-engine/).

Antigravity runs agent commands in a terminal sandbox that cannot see the
mailbox, so bus commands fail there. The installer adds the allow rule
`command(llm-bus)` to `permissions.allow` in `~/.gemini/antigravity-cli/settings.json`.
A `command` allow rule runs matching commands, by token prefix, without
prompting and outside the sandbox. Uninstall removes it. Deny and ask rules,
and an organization's admin policy, take precedence. The rule matches a command
line that starts with `llm-bus`, so a pipe or heredoc into `llm-bus send` still
asks for approval, and headless `agy -p` runs deny it. Agents can pass the
message with `--body` instead. See
[Antigravity permissions](https://antigravity.google/docs/permissions).

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

For a managed installation this establishes the hold, then removes owned public
launchers/aliases, generation uv registrations, bus hooks, the Claude allow rule
and the verified installed skill copy. Owner-modified aliases or skill bytes
block removal rather than being overwritten. The mailbox, policy, history,
coordination records and native-home bindings remain, along with recovery
artifacts and a held private runtime/journal tombstone. Rule files and pointer
blocks need separate removal at the owner's request. Interrupted removal resumes
through `maintenance recover` using the retained private launcher at
`~/.local/share/llm-bus/maintenance/launcher` if public aliases are gone.

After completed uninstall, a new setup uses the first-install wheel commands
above and a fresh explicit inventory. Do not delete the tombstone: bootstrap
archives the retired operation and verifies the old-to-new runtime transition.
Existing mailbox compatibility and cutover approvals still apply. For
downgrades, follow the rollback guidance above.
