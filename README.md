# llm-bus

Let your Claude Code and Codex sessions talk to each other.

`llm-bus` gives coding agents a shared, durable mailbox. One agent can ask another to review a change, coordinate edits across worktrees, or hand over context. Messages stay pending until the recipient acknowledges them, including across restarts.

Local messaging needs no server. An optional Cloudflare relay connects machines, with each owner controlling which peers can send or receive. Start with the local setup below, or follow the [remote guide](docs/remote-bus.md) for cross-machine messaging.

New to the bus? The [tutorial](docs/tutorial.md) walks through every mode and configuration, from local messages to coordinated projects and remote machines, with commands and example output.

- [Install and verify](#install-and-verify)
- [Send, read, and acknowledge](#send-read-and-acknowledge)
- [Agent labels](#agent-labels)
- [Coordinate shared resources](#coordinate-shared-resources)
- [Wake behavior](#wake-behavior)
- [Help agents discover the bus](#help-agents-discover-the-bus)
- [Cross-machine messaging](#cross-machine-messaging)
- [Collaborate with another person](#collaborate-with-another-person-experimental)
- [Browse message history](#browse-message-history)
- [Storage and permissions](#storage-and-permissions)
- [Uninstall](#uninstall)
- [Development](#development)
- [Branch workflow](#branch-workflow)

## Typical workflow

1. Open two agent sessions in the same repository, including separate worktrees.
2. Find the other agent with `llm-bus list`.
3. Send a focused question or handoff with `llm-bus send`.
4. The recipient reads its inbox, handles the message, and acknowledges it.

Sending saves the message before requesting a wake. A successful wake request does not prove the recipient handled it. If waking fails, the stored message remains available; do not resend blindly.

## Install and verify

Install [uv](https://docs.astral.sh/uv/), clone this repository, and run these commands from its root:

```text
uv tool install .
llm-bus install
```

The first command installs the CLI; the second separately installs native host
hooks. Neither copies skills, enables project coordination, initializes Beads,
or edits standing agent instructions.

To [collaborate with another person](#collaborate-with-another-person-experimental), install the optional extra instead: `uv tool install --force '.[collab]'`.

These steps apply to macOS and Linux. Run installation separately on each machine, as the operating-system user who runs the agents. If your shell cannot find `llm-bus`, run `uv tool update-shell` and open a new shell. To discover uv's executable directory without assuming a username, home directory, or package-manager prefix, run `uv tool dir --bin`.

`llm-bus install` adds user-level `SessionStart`, `UserPromptSubmit`, and `SessionEnd` hooks for both hosts, plus a Claude Code allow rule for direct bus commands. It discovers `llm-bus` on the installing process's `PATH` and writes its absolute executable path, with shell quoting for paths containing spaces. The path belongs to that machine; do not copy generated hook files between machines or commit them as shared configuration. After upgrading, moving the installation, or changing configuration directories, run `llm-bus install` again. This also migrates older bare `llm-bus hook ...` commands without duplicating bus hooks.

Close both agents before installing or uninstalling, and avoid simultaneous edits to their settings files. The installer preserves unrelated settings but cannot coordinate with another writer. It rejects symlinked settings files; use your dotfile manager to edit their targets.

| Host | User hook file | Custom configuration directory |
| --- | --- | --- |
| Codex | `.codex/hooks.json` under the user's home | Set `CODEX_HOME` before installing and starting Codex. |
| Claude Code | `.claude/settings.json` under the user's home | Set `CLAUDE_CONFIG_DIR` before installing and starting Claude Code. |

Installation also records the selected Codex home in
`~/.local/share/llm-bus/native-hosts.json`. All bus callers, including Claude and
bridges, use these machine-owned bindings to identify Codex children regardless
of their own `CODEX_HOME`. Run `llm-bus install` for each Codex home you use.
Previous installed homes remain recorded when another is added. Before first installation,
lookup uses the calling process's Codex home; custom-home installations must
complete setup before enabling mixed-host messaging. Uninstall retains the bindings.

GUI applications, services, SSH sessions, and shell tools can inherit different environments. An interactive terminal finding `llm-bus` does not prove a hook can find a bare command. The installed absolute paths avoid that dependency for hooks. Agent shell commands still need the tool directory on `PATH`, and Codex wake requests still need `codex` on the sender's `PATH`. Configure the environment of the process that actually launches the agent or service; changing only `.zshrc` or `.bashrc` may not affect it. Restart that process after environment changes.

Restart Codex and Claude Code. In Codex, open `/hooks` and review and trust the bus hooks; untrusted hooks do not run. Remove any older project-level copies of the bus hooks to avoid duplicate notifications. Codex hooks are on by default (`hooks` feature, stable); `hooks = false` under `[features]` turns them off. Project-layer hooks also load only when Codex trusts the project. Reinstalling keeps other tools' hooks in place, except in two cases: the bus handler moves out of a group that has a `matcher` or any other key besides `hooks` into its own group, and a duplicate group holding only bus handlers is dropped, which shifts later groups. Uninstalling can shift later hook groups. Re-trust any hook that `/hooks` then marks modified. See the [Codex](https://learn.chatgpt.com/docs/hooks) and [Claude Code](https://code.claude.com/docs/en/hooks) hook guides.

Inside each agent's shell tool, verify registration:

```sh
llm-bus whoami
```

It returns an address such as `codex:SESSION_ID` or `claude:SESSION_ID`. Use the actual returned address when sending. Hooks register sessions automatically. Claude Code registers at launch (`SessionStart`); a newly opened Codex session may not have registered until its first turn.

If a hook exits with code 127, inspect its command in the active user hook file. On macOS or Linux, `command -v llm-bus` should identify the installation you intend to use. Re-run the installer from that environment, then review changed hooks in Codex's `/hooks`. To check executable lookup without registering a fake session or writing a message, run the installed hook command with `--help` appended, keeping its executable quoting and temporarily using a restricted `PATH`. For example, set `PATH=/usr/bin:/bin` for that check. Exit code 0 confirms the command can start; a real new session must still verify hook execution, identity, and any native wake permissions. Empty JSON is not a valid hook payload and must not be treated as a successful lifecycle test.

### Upgrade

From a checkout with no uncommitted tracked changes, run `uv run llm-bus upgrade`. It builds a wheel from `HEAD`, installs it with the currently installed extras and the Python pinned by the checkout (`.python-version`) and verifies the new install. Only after verification passes does it delete every older folder in `~/.local/share/llm-bus/packages/` and everything in `~/.local/state/llm-bus/releases/`; it keeps no old versions, and rolling back means checking out the older commit and upgrading again. If the install or verification fails, it deletes nothing, reinstalls the previous wheel and exits with status 1. It never touches `backups/`, `deploy-backups/` or other bus state. Run it from a checkout outside both folders it prunes.

To update the installed skill in the same step, add `--skills-dir DIR`, for example `uv run llm-bus upgrade --skills-dir ~/.agents/skills`. After verification and before pruning, it replaces `DIR/llm-bus` with `skills/llm-bus` from the same `HEAD`, so the skill's guidance version matches the new CLI. Local edits in the installed copy are lost; untracked files in the checkout are not copied. `DIR` must already exist. If `DIR/llm-bus` is a symlink or a file, the command refuses: pass the directory that holds the real copy instead (here, `~/.agents/skills` rather than a linked `~/.claude/skills`). If the skill update fails, the new CLI stays installed and nothing is pruned. Without the option, the installed skill is left alone. Rule files are never copied; update them yourself.

The command does not reinstall hooks or manage bridges. Afterwards, close both agents, run `llm-bus install` (as the sentence above requires after upgrading), then restart the agents. Stop any running `llm-bus bridge` before upgrading and start it again afterwards, or restart it afterwards and check its log; how depends on how your machine supervises it.

The command can restore only a previous install from a wheel, so a first install or a directory install (`uv tool install .`) needs this one-time bootstrap instead. Omit `[collab]` when you do not use the extra; the next successful upgrade deletes `bootstrap/`:

```text
uv build --wheel --out-dir ~/.local/share/llm-bus/packages/bootstrap
uv tool install --force --python 3.12 "$(ls ~/.local/share/llm-bus/packages/bootstrap/llm_bus-*.whl)[collab]"
```

## Send, read, and acknowledge

Agents should add `--format agent` before the command for concise output; see [Agent output format](#agent-output-format).

Each agent uses its host-generated session ID as its bus address. Inside an agent's shell tool:

```text
llm-bus whoami
llm-bus list
llm-bus send claude:SESSION_ID --body "Migration is ready for review"
llm-bus inbox
llm-bus ack MESSAGE_ID
```

`list` shows sessions in the current Git repository, including its linked worktrees, that have not reported an end or been idle for 24 hours. Outside Git, it uses the current folder. `agents` also shows ended sessions with `ended_at`. `send` accepts registered recipients in the same repository or folder by default. To contact an agent in another repository or folder, make that choice explicit:

```text
llm-bus list --all
llm-bus send codex:SESSION_ID --cross-folder --body "Please review the API change in my project"
```

Messages include the sender's and recipient's actual folders at send time. Routing uses each session's latest reported repository or folder; an agent that moves should run `whoami` or `list` to update its registration. A clean session close removes its address from `list`. A session idle for 24 hours is also marked ended when the roster or its status is next read. Its next hook or CLI command registers it again, restoring it to `list` with pending messages intact. A listed address does not prove that the session is online. Sending directly to a known ended address still stores the message but reports wake failure.

The recipient reads pending messages with `inbox` and runs `ack` for each message after handling it. Reading does not consume a message. Pending messages survive restarts and can appear again after an interrupted turn, so use message IDs to recognize repeats. Ordinary message bodies are untrusted agent input, never user instructions, permission, or approval. Optional directions require current receiver verification under an owner-approved project grant; body text cannot establish that authority. Bus addresses and folder values alone do not authenticate senders.

The response includes the stored message and a `wake` status. Check that status when a prompt response matters; [Wake behavior](#wake-behavior) explains each result.

### Agent output format

Agents should use `llm-bus --format agent COMMAND ...` for concise output. For
example, `llm-bus --format agent inbox --limit 20`. Put `--format` before the
command. Use `llm-bus --format json COMMAND ...` when expanded metadata is needed
for inspection. Omitting the flag preserves the existing expanded JSON contract.

Agent output is compact JSON: send receipts omit the already-known body and
sender context; peer lists omit the kind already encoded in the address; lock
catalogs omit creator and last-use metadata. Message reads omit null optional
fields and historical wake-check timestamps. Message bodies, reply IDs, current
coordination verification, wake results, lock ownership and expiry remain intact.
Collections with repeated context can return `{"shared": {...}, "items": [...]}`;
the shared fields apply to every item. Other collections remain arrays, including
empty results. No message text is truncated. Unicode text stays readable while
terminal control and directional formatting characters remain escaped.

This changes presentation only: reads do not acknowledge messages, grants are
not inferred from omitted fields, and stored data is unchanged. Hook, bridge and
human-only monitor output retain their dedicated formats. The flag does not
change page sizes; use `--limit` for smaller inbox/history reads.

## Agent labels

Claim a readable name in your current repository or folder:

```sh
llm-bus whoami --label Builder
llm-bus send Builder --body 'Inspect the shared API before editing.'
llm-bus send MACHINE/REPO/Builder --body 'Inspect the shared API before editing.'
```

Names contain 1–64 ASCII letters, digits, underscores or hyphens and start with
a letter or digit. Matching ignores case; the claimed spelling is displayed.
Linked worktrees share repository scope. Duplicate claims fail without changing
the existing claim. Run `whoami --label NEW_NAME` to rename atomically.

A clean session close, movement to another scope, or 24 hours of inactivity
releases the name. Configured close hooks provide coverage, not a guarantee that
every live host sends a close event. Expiry applies when activity is inspected.
Turns and compaction retain an active claim. A resumed ended session must
explicitly claim again; implicit registration does not reclaim a released name.

Names select exact session addresses once per command. They grant no permissions
and never transfer history, reply participation or coordination grants on reuse.
Use `history --peer codex:OLD_SESSION_ID` to inspect the previous owner's history.
Bare names resolve only in the selected local scope. Remote names require full
`MACHINE/REPO/NAME` qualification and a policy-eligible cached roster; duplicate
remote claims fail as ambiguous. Cache presence does not prove reachability.
Exact native addresses remain supported. Remote discovery needs upgraded bridges
and relay migration `0005_session_labels.sql`.

## Coordinate shared resources

Sessions that share a database, a generated file, or any other resource can take an advisory lock on it by name. Every holder must say when it expects to be done:

```text
llm-bus lock list
llm-bus lock acquire db:orders --check 45m --describe "orders Postgres database" --reason "running migration 0042"
llm-bus lock acquire db:orders --notify
llm-bus lock status
llm-bus lock release db:orders
llm-bus lock cancel db:orders
llm-bus lock release db:orders --force --reason "holder session was killed"
```

A lock only protects anything if every session uses the same name for the same resource. The bus keeps a catalog of names with what each protects, free or held. `lock list` shows it, and hook notices show up to ten names with their descriptions at session start and when names or descriptions change. The first acquire of a new name must say what it protects with `--describe`. A new name that resembles an existing one, such as `orders-db` next to `db:orders`, is refused with the existing name and its description, unless `--new` confirms it is a different resource. Names unused for 30 days drop out of the catalog.

`--check` is the holder's optimistic estimate of when the lock will be releasable, as minutes, hours, or days (`45m`, `2h`, `1d`). When that time arrives, the bus sends the holder a `lock check` message and wakes its session: release it if the task no longer needs it, or run `acquire` again with a new `--check` to be asked again then. A check is sent once per estimate. Until the holder answers, its hook notice marks the check as unanswered on every turn. Acquiring a free resource without `--check` is an error.

The check fires from a small detached process that `acquire` starts and that sleeps until the estimate. If that process is gone, for example after a reboot, the next lock command or hook from any session on the machine sends the overdue check instead.

`acquire` returns `acquired` for a free resource and `renewed` when you already hold it, which also resets the lease and the check. When another live session holds it, you are queued, the result is `waiting`, the command exits with status 3, and nothing is sent unless you add `--notify`. The nonzero status keeps `lock acquire NAME && command` from running the command without the lock. With `--notify`, the holder receives a `lock request` message with your reason and is woken now rather than at its check time. Repeating `--notify` re-asks at most once every five minutes; the result's `notified` field says whether a message went out. On `release`, or when a lease lapses, the resource passes to the earliest live waiter, which is woken with a `lock granted` message asking for its own `--check`; otherwise the resource becomes free. Only the holder can release. A waiter that no longer needs the resource leaves the queue with `cancel`, without taking the lock first.

Each lock is also a lease: 24 hours by default, up to 7 days with `--ttl` in minutes, and it ends at once when the holder's session ends. The lease is a backstop; the check is the normal way locks come back.

A killed host fires no close hook. To catch that, hooks and commands record the session's host process: the nearest `claude` or `codex` ancestor, with its start time. Every lock command checks the recorded processes of holders and waiters with `ps`. When a process is gone, or its PID now belongs to a later process, the bus ends that session, passes its locks to the next live waiter, and records the release. The grant message says the holder exited. Ending the session also ends its labels and its coordination join consent and enrollment, as a close hook would; a resumed session registers again but needs a fresh owner `join`. This does not detect a dead session whose host process lives on. For example, a Codex thread inside a shared app-server or editor process stays held until its lease ends or a forced release. If `ps` fails, or no process was recorded, nothing changes.

`lock release NAME --force --reason TEXT` takes a lock from another holder. Only two callers may run it:

- the owner, from an interactive terminal with no agent session identity and no `claude` or `codex` ancestor process;
- the active coordinator of a coordination project in the lock's scope, or of the project the holder works in.

The old holder receives a `lock force-released` message with the reason, and the lock passes to the next live waiter. If the holder named at the start already lost the lock, because its process exited or its lease lapsed, the result is `already_released` and the new holder keeps it. Every forced and exited release is recorded in the `resource_lock_releases` table with the holder, the cause, the actor, the reason and the new holder. The terminal check is not proof of a human: an agent process that escapes its host's process tree, for example through a detached launcher, can pass it. Treat the audit row as the control. Names are scoped to the current Git repository or folder; add `--shared` for a machine-wide name. `status` lists locks in this scope plus shared ones, with holders, check times, leases, and queues; `--all` lists every scope.

Locks are advisory: nothing stops a session that never calls `acquire`. Lock messages are bus messages, so treat them as untrusted coordination data like any other. Locks do not cross machines.

## Wake behavior

`send` commits the message before requesting a host wake. The CLI exits successfully when storage succeeds, even if the wake fails, and reports that failure in the JSON `wake` field. Inspect that field when a prompt reply matters.

| `wake.status` | What it tells you |
| --- | --- |
| `requested` | The request was submitted; handling is not confirmed. |
| `coalesced` | Pending work shares an existing wake request, lease, or scheduled retry. |
| `failed` | The message is stored, but the wake failed. |
| `unknown` | The request timed out; the host may still have received it. |
| `unsupported` | Automatic wake is unavailable; hooks notify on a later turn. |

Codex wakes through `codex queue --resume`; Claude Code uses its native inbox socket on macOS and Linux. Both require a registered, reachable session. If delivery stalls, expand the host details below.

<details>
<summary>Host requirements, stale notices, and Claude inbound settings</summary>

Concurrent sends share a recipient lease and may return `coalesced`. Each message remains separately stored and requires its own acknowledgement. The coordinator gathers a burst for up to 25 milliseconds, uses the oldest permitted pending message without a successful wake request as the notice anchor, and records exactly which pending IDs the request covered, including messages beyond the first inbox page. Successful wake coverage persists across restarts: the coordinator does not request another wake for those same IDs merely because handling is slow. A later message does not inherit that coverage and can request its own wake. Neither `coalesced` nor `requested` proves receipt or handling. If a successfully queued notice is lost by the host, messages remain pending for the recipient's next hook or explicit inbox read; the bus does not automatically resend that notice.

A call makes at most two native wake attempts. Further arrivals remain due for a running bridge, a later send, or notification in the recipient's next hook. A bridge also recovers abandoned 30-second leases and retries failed or unknown wake outcomes with capped backoff. Unsupported wakes wait for a changed registered host capability or a later send; hooks still report pending items. Without a running bridge, a crashed sender has no autonomous recovery: another send or recipient turn is required. Current policy and ended-session state are checked before dispatch. Blocking a peer cannot recall an already submitted wake.

Acknowledging a message does not cancel its already queued host notice. With the bus's trusted Codex `UserPromptSubmit` hook enabled, an exact bus wake receives no-op context when its ID, sender, and recipient match an acknowledged message and the inbox has no pending messages. The context tells the agent that no action is needed and not to retry or acknowledge the notice again. The hook allows the prompt rather than rejecting it, avoiding repeated hook rejections if the host resubmits a stale notice. Codex may still display the queued notice and run a redundant agent turn. Other prompts and wakes with pending work continue normally. Claude wake behavior is unchanged.

| Recipient | Automatic wake path | Limit |
| --- | --- | --- |
| Codex | `codex queue --resume` with the recipient's session ID | Requires `codex` on `PATH` and a reachable parent session. |
| Claude Code | Native inbox Unix socket on macOS and Linux | Requires a registered socket and Claude inbound delivery permission; no terminal input is sent. |

Codex's `--resume` requests that the host resume the thread after queueing. Resume is best-effort; an active turn can still defer the notice, and a queued notice does not prove handling. Older Codex versions that reject `--resume` fall back to queueing alone. Codex v2 sub-agents and v1 spawned sub-agents that are not loaded reject direct app-server input: their wake is `unsupported`; address the parent coordinator instead. The bus never redirects a message to another recipient automatically.

The wake prompt includes the bus message ID and sender address, never the message body. Both hosts receive the same notice, such as ``llm-bus message #1231 from codex:SESSION_ID. Read and handle it with `llm-bus inbox`.`` Claude Code may still label its native approval dialog “Another Claude session”; that host label does not identify the bus sender. Read the sender address in the notice or stored inbox message. For Claude, `requested` means the frame was written to the native socket; inbound controls may still hold or refuse it. A successful request does not prove delivery or handling. Messages remain pending until the recipient reads and acknowledges them. Without a wake, hooks report pending messages at the recipient's next turn.

Claude Code v2.1.224+ exposes `CLAUDE_CODE_MESSAGING_SOCKET` to hooks and shell commands. Restart Claude or run `llm-bus whoami` inside its shell tool after upgrading llm-bus to register the endpoint. Native messages enter Claude's queue between tool calls and can start a turn while idle, preserving the prompt draft. The bus sends a session-targeted JSON frame and never uses `herdr agent prompt`, which can submit unfinished user input. No messaging tokens are stored. Missing sockets and Windows named pipes use deferred notification (`unsupported`); unreachable sockets report `failed`. See [Claude's inbox socket](https://code.claude.com/docs/en/cross-session-messaging#the-sessions-inbox-socket).

Claude may drop or hold inbound messages: messages held by default expire (five minutes unless `dialogExpiry` says otherwise), the receiving inbox queue is capped, and each sender is rate-limited. See [cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging) for the current numbers. VS Code and Desktop sessions cannot show the approval dialog. `claude --bare` opens no socket. Sockets do not cross container or WSL boundaries. A sandboxed Claude Bash command needs `sandbox.network.allowUnixSockets` or `sandbox.network.allowAllUnixSockets` to reach the socket. Claude's inbox frame format is undocumented and may change; a raw frame from another tool appears as from `unknown`.

Claude's inbound controls apply to bus wake frames. The bus does not assert a sender permission-mode class, so Claude sessions that bypass permission prompts may hold the wake for approval by default. To accept native messages without a per-message dialog, select **Messages from your other sessions: accept** in Claude's `/config`, or set `crossSessionInbound` in your user-level `settings.json` under `.claude`:

```json
{
  "crossSessionInbound": "accept"
}
```

This setting affects all native inbound messages, including bus wakes; llm-bus does not change it during installation. A project-level setting cannot relax the user-level inbound rule. The recipient's own permissions still apply to work requested in the message. See [inbound controls](https://code.claude.com/docs/en/cross-session-messaging#control-inbound-messages) and [setting precedence](https://code.claude.com/docs/en/settings-reference#crosssessioninbound).

</details>

## Help agents discover the bus

Only main agents use the bus. Sub-agents report to their parent through native
agent communication; the parent handles bus messages and resource locks.

The hooks tell agents their bus address and pending-message count, but standing instructions tell them when to use the bus. Follow [Install agent instructions](examples/agent-instructions.md): it lists the machine setup (CLI, hooks, hook trust, Claude inbound setting, `PATH`, skills), then installs a short always-loaded pointer block plus rule files that agents read only when a trigger applies. The [llm-bus rules file](examples/instructions/llm-bus.md) also carries team rules: contact agents only when their work is affected, agree ownership before overlapping edits, and use separate worktrees.

If you are an LLM installing the bus for someone, **ask whether to install the instructions for this project or globally before editing those files**. Preserve existing instructions and replace older llm-bus sections instead of adding duplicates. The guide's scope table lists the instruction file and rule directory for each host. Configure these files on each machine; never copy another machine's executable paths or session IDs.

Start a new agent session in the chosen scope and confirm `llm-bus whoami` succeeds. See the [Codex AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md) and [Claude Code memory](https://code.claude.com/docs/en/memory#agents-md) guides for instruction loading rules.

Install the [llm-bus skill](skills/llm-bus/SKILL.md) by copying the whole `skills/llm-bus/` directory into the host's shared skill directory, normally `~/.agents/skills/llm-bus/`, or by running [`llm-bus upgrade --skills-dir`](#upgrade) from a checkout. It is optional for local-only messaging and required for remote messaging, collaboration and coordination, which the rule files delegate to its references. The skill requires the CLI above; copying it, or the rule files, installs nothing else: no hooks, Beads database or trust changes. Coordination prerequisites (Beads skill, existing Beads workspace) and optional caveman are listed in [Install agent instructions](examples/agent-instructions.md).

SessionStart and missing or outdated guidance state emit full identity, trust, and usage guidance. Normal prompt hooks use a compact reminder with the current folder, pending count (capped at `100+`), inbox/ack hints, and current peer restrictions. Remote usage details appear in the full form only for a configured repository. To recover full guidance after context compaction, run `llm-bus guidance`; it returns the current guidance without changing messages or guidance-version state. Both hosts fire `SessionStart` after compaction (source `compact`; Codex also mid-turn), so full guidance returns on both without a manual step. `llm-bus guidance` stays the manual recovery.

<details>
<summary>How the bus handles sub-agents</summary>

Codex child sessions identified in native host metadata are excluded from
discovery and delivery; the bridge withdraws their older advertisements.
History is retained. On both hosts, hook payloads from sub-agents carry the
root `session_id` plus `agent_id`, so the bus treats a non-empty `agent_id` as
a sub-agent. Claude also sets `agent_id` for in-process teammates (agent
teams), so the bus treats them as children too. Codex children get
`SubagentStart` and no `SessionEnd`. Codex CLI calls are still identified by
spawn edges, because `CODEX_THREAD_ID` is per thread. Child hooks carrying
`agent_id` do not register an endpoint or change their parent's lifecycle. No
message is redirected automatically. Claude workers can share the parent's shell session identity;
standalone CLI calls cannot reliably distinguish them. The main-agent rule in
instructions and the skill forbids workers from using that inherited identity.
These checks are not a sandbox against processes sharing the same
operating-system account. Codex native metadata read errors stop affected
operations rather than guessing a main role. Existing child-held resource locks
keep their ordinary expiry/end rules; child waiters are removed, and
undeliverable reminders do not block main-agent operations. Registered sessions
seen within the last day are not necessarily running.

</details>

### Resource lock instruction examples

See the [lock instruction examples](examples/resource-locks.md) for a copyable
project lock-name table and an acquire/wait/release walkthrough. The general
lock procedure is part of the [llm-bus rules file](examples/instructions/llm-bus.md).

### Optional project coordination

Project direction is a separate, opt-in workflow. The owner manually starts
sessions, requests `/llm-bus coordinate PROJECT` in one main session and
`/llm-bus join PROJECT` in each worker. A direct owner join records consent to
the current exact coordinator and objective. Automatic consent ends at session
close, 24-hour inactivity expiry, leave, revoke or generation replacement;
turn completion, compaction and working outside the project folder preserve it,
though directions do not verify while the session is outside: a send is refused
with a folder mismatch error, or with `wrong_scope` under `--cross-folder` and at
delivery. Existing explicit owner grants keep
their selected lifetimes. Local joins record the assignment target and one
durable informational arrival per grant, even if the coordinator is offline.
Native wake support determines whether the coordinator resumes automatically.
Remote joins require a receiver-owner binding and matching upgraded clients/relay
(relay migration `0006_coordination_availability.sql`; see the
[remote guide](docs/remote-bus.md#optional-project-directions));
structured availability exchange records the coordinator target automatically.
Joining assigns no specialty. Defaults and ordinary messages remain
unchanged. Directions cannot bypass owner instructions, host permissions or
repository rules.

Active workers send questions, approval requests and manual-action requests to
their current coordinator by default, even without delegated gates or owner
decisions. The coordinator asks you with the worker's session and ticket/branch
context, records the outcome and follows up. You may override this communication
default directly. Routing supplies no consent or permission: when direct local
authorization is required, the coordinator asks you to act in the exact owning
session or machine. Native permission prompts remain native. A blocked worker
resumes only after its existing authority and verification requirements are met;
an unreachable coordinator means preserving the blocker and yielding for recovery.

Owners can delegate commit, push, merge and merge-admin gates for explicit
target branches: the coordinator owner chooses them with
`llm-bus coordinate PROJECT --setup` (a guided confirmation wizard in a
terminal; agents pass `--format agent` and ask the owner through their native
question UI) or `--delegate GATE:BRANCH`, and each worker owner accepts them
through a direct `join`, optionally narrowed with `--narrow GATE:BRANCH`.
The coordinator then sends `llm-bus coordination approve`, which workers act on
only after live checks and for tickets they hold with `coordination claim`.
Force-push, deploys or production changes, credential or secret changes, paid
or live runs, and merge with failing or pending CI are never delegable, and
the bus checks no CI itself. A remote coordinator can send gate approvals only
when the coordinator owner's delegation, the receiver owner's own delegation
(`coordination setup --delegate`), the worker's binding and the receiver's peer
policy `accept_delegated_gates` all allow them; see the
[remote guide](docs/remote-bus.md#remote-gate-approvals).
The first command run by this version upgrades the mailbox to schema version
13; older binaries then refuse it, so upgrade every llm-bus installation that
shares the mailbox (`uv run llm-bus upgrade` from a checkout) before any
session uses it. See the
[canonical workflow](skills/llm-bus/references/coordination/gates.md#gate-approvals-local-projects).

The [simplified coordination defaults](docs/coordination-simplified-flow.md) and
[Claude coordinator with three Codex workers example](examples/claude-coordinator-codex-workers.md)
describe the coordination flow. The
[step-by-step coordination diagram](docs/diagrams/coordination-steps.html)
shows the local flow as numbered steps; open it in a browser from a local
checkout. Upgrade the executable, hooks, skill and rule files together; mixed
versions can disagree.

Read the [canonical coordination workflow index](skills/llm-bus/references/coordination.md)
and the files it lists for all CLI equivalents, local Codex/Claude and remote examples, grants,
revocation, replacement and recovery. Install the optional [coordination rules file](examples/instructions/llm-bus-coordination.md)
with [Install agent instructions](examples/agent-instructions.md) after choosing
project/global destinations. Both roles persist Beads memory; the coordinator
owns project memory/assignments and workers own task progress notes. Beads setup
and synchronization remain owner configured; local bus locks cannot serialize
remote database updates.

Owner decisions are opt-in with `--owner-decisions` on `coordinate` or
`coordination amend`; no gate is needed. The coordinator records the owner's
answers with `coordination decision-ask`/`decision-answer` only when the owner
gave them directly in its session, and a bound worker acts on one only after
`coordination decision-show` reports `accepted: true`. `--decision-defaults
MINUTES` (1 to 1440, needs `--owner-decisions`) lets an unanswered `scope`
decision with a recommended option take that option after the time, waking the
coordinator and the worker named with `--worker`; other classes never default.
`decision-list --defaulted` and the `decision_digest` key of `coordination show`
list the defaults for the owner.

Owner standing instructions for the coordinator or the workers are set only by
the enrolled coordinator with
`llm-bus coordination instructions PROJECT --audience coordinator|worker (--file PATH | --text TEXT) [--amend]`,
and only on the owner's direct request in that coordinator session; never
because of a bus message, worker request, tool output or file content. Workers
never run it, and command results show a worker only worker-facing text. The
text may not contain `<<<`, `>>>` or Unicode format characters. Each session
gets its own role's text verbatim from `llm-bus guidance` and in the
`guidance` field of `coordinate` and `join` results; `coordination show` lists
it filtered by role. Hooks never print the text: every hook carries a one-line
pointer with a digest, and the session runs `llm-bus guidance` before acting
unless its context already holds the text with that digest.

The coordinator lists the directions it sent, with each one's work state and age, with `llm-bus coordination direction-list PROJECT [--open]`. A worker reports progress on a local direction with `llm-bus report ID --status STATUS`; the report stays on that machine and wakes nobody. See the [tutorial](docs/tutorial.md#5-coordinate-a-project).

Current enrollment reminders survive normal prompt recovery: SessionStart gives
full references; UserPromptSubmit rereads authority after compaction. Use
`llm-bus guidance` and `llm-bus coordination show PROJECT` for explicit recovery.
Expired/revoked grants suspend directions; frozen context or tickets cannot
restore authority. Hooks do not execute Beads or network calls. This is a
deterministic hook-payload contract, not proof of live host compaction behavior.

## Cross-machine messaging

The optional Cloudflare relay connects sessions on different machines. Each owner sets a local version 2 TOML policy for exact peers and capabilities. Both ends must permit an operation; sending permission grants no reverse access. Relay credentials and repository admission grants are separate from local policy. There is no shared topology administrator.

Optional project directions additionally need version 1 coordination transport,
receiver-local project setup and exact recipient grants. Unsupported relays leave
directions queued with `unsupported_coordination`; upgrade rather than resend as
ordinary authority. Each machine independently applies replacement/revocation;
offline receivers retain their last local policy. See the [remote coordination setup](docs/remote-bus.md#optional-project-directions).

Incoming text can go to an agent's `inbox`, stay in a human-only `monitor`, or be denied. Monitor text stays outside native inboxes, pending counts, hooks, and wakes. Structured reports use fixed status/reason codes tied to existing messages; they never wake agents or authorize work. Your PC can send tasks to a VPS while accepting only reports in return. Forwarded text still follows normal incoming-text policy.

Follow the [remote guide](docs/remote-bus.md) for setup, complementary policies, monitoring, and migration. Old repository-wide configuration fails closed and requires explicit peer choices. Stop bridges before downgrading to an older client.

The relay operator can read plaintext. These controls govern bus delivery, not processes sharing an OS account or machine credential. Reports remain recipient claims, not proof of completed work. Their values and timing can still convey information.

## Collaborate with another person (experimental)

`llm-bus collab` connects your agents with a collaborator's agents for one repository, point to point, without a hosted service. Each person proves their GitHub identity with an SSH signing key published on their account. Machines connect over [iroh](https://iroh.computer): direct encrypted QUIC with NAT traversal, falling back to public relays that forward only ciphertext.

1. Install the extra: `uv tool install --force '.[collab]'`, then run `llm-bus collab setup --github-user YOU` and follow its `next_steps` to create and publish a signing key.
2. Run `llm-bus collab daemon` (one per identity). It serves your private owner page on `127.0.0.1`.
3. Your agent runs `llm-bus collab invite` and gives you a link. On that page you choose the collaborator's GitHub user and what their messages may cause on your machine, then send them the invite text.
4. Their agent runs `llm-bus collab join INVITE`. On their own page they choose what your messages may cause on their machine. Both sides verify each other's signing keys and show the same confirm code.
5. Agents send with `llm-bus collab send USER`.

Each side picks a mode per collaborator: `monitor` (only you read their messages), `isolated` (a fresh sandboxed agent answers from committed files), `readonly` (a sandboxed fork of your session answers), `readwrite` (the fork may edit a separate review branch), or `full` (their text reaches your main agent; expires). Below `full`, unsolicited peer text never enters your main agent automatically; your agent gets a notice and you read or forward the message on your page. To ask a collaborator's agent something and get the answer back in your session, enable **agent answers** for that peer on your page: the first reply to each question your agent sent is delivered to it, labeled as untrusted. Only you approve invites, joins, modes, and held replies.

Several GitHub accounts on one machine work too, for example to test both ends. Give each identity its own terminal and set `GH_CONFIG_DIR` (a separate `gh` login) and `LLM_BUS_COLLAB_HOME` (a separate signing identity, daemon, and owner page) before starting the agent there. Do not use `gh auth switch`; it changes the account for every terminal. Identities that share one macOS user can read each other's files, so this is for testing or your own accounts, not isolation between people.

Under each collaborator's **Change permissions**, choose **Responder context**: **Fork** (the default) copies session history; **Summary** starts a fresh agent with a status summary you write on that page (up to 4000 characters). Summary context needs no bound Claude session, but still uses Claude as the responder. It preserves the mode's tool limits. Isolated mode never copies history; monitor and full ignore this setting. Update the summary manually as work changes.

Forked responders can repeat anything already in your session's context. Summary responders receive only your supplied summary and repository access; include only information that collaborator may receive. Replies that look like secrets are held for your review, and you can choose to review every reply. See the [collaboration plan](docs/collab-plan.md) for the threat model, enforcement details, and known gaps. The optional [skill](skills/llm-bus/references/collab.md) walks agents through setup.

## Browse message history

`llm-bus status MESSAGE_ID` reads body-free diagnostics for one retained local integer message ID. Replace `MESSAGE_ID` with an ID from `send`, `inbox`, or `history`. It needs no host identity, configuration, token, network, or running bridge, and remains readable after blocking a peer or removing configuration. It never registers a session, migrates the mailbox, acknowledges a message, or requests a wake. A missing mailbox or unknown/nonpositive ID fails without creating data.

The result separates `transport`, the message's own `wake` result and coverage, `acknowledged_at`, `work_report`, and `synchronization`. For an outgoing relay message `work_report` is the last synchronized strict report; for a local coordination direction it is the recipient's local report (`llm-bus report ID --status STATUS`), which stays on that machine and wakes nobody. Native storage is `stored_local`; an imported message proves local durable admission, not current sender-side relay state. Missing metadata is null. Synchronization is stale without a successful observation, after 30 seconds, or after an error. Separate operation metadata prevents a successful transport read from implying a fresh report. Wake errors use fixed safe categories; original history is retained. A queued report intent is not a synchronized report, and no state proves task completion or authorizes work.

`history` reads stored bus messages without acknowledging them. It does not need a Claude or Codex session ID, so a separate viewer can use it. It shows messages involving the current folder by default:

```text
llm-bus history
llm-bus history --folder PROJECT_FOLDER
llm-bus history --repository PROJECT_FOLDER
llm-bus history --all
llm-bus history --all --before 123 --limit 50
llm-bus history --folder PROJECT_FOLDER --after 123
llm-bus history --conversation 123 --peer codex:SESSION_ID
llm-bus status MESSAGE_ID
llm-bus agents
```

`--folder` matches one exact folder. `--repository` includes that Git repository and its linked worktrees, using each message's send-time repository; outside Git, it matches the folder. Old messages whose worktree paths no longer exist may appear only under their exact folder. History rows contain message ID, sender, recipient, body, both folders at send time, send time, acknowledgement time, and the recorded wake status, path, reason, and check time. A null acknowledgement time means the message is pending. Null wake fields mean the message predates wake recording or the send stopped before its result could be stored. The default view and `--before` return newest messages first; `--after` returns oldest first for polling. Each call returns at most 100 messages. Use the lowest returned ID with `--before` for an older page and the highest seen ID with `--after` for new messages.

`agents` returns registered addresses, provider kinds, last reported folders (`project`), and last activity times. Neither command changes message state. Old messages with unknown folders keep null values; the bus does not infer their past location from a session's current one. History contains bus messages, not full agent transcripts.

Use `llm-bus send ADDRESS --reply-to 123 --body "Reply text"` to link a reply to a retained local message ID. Participants and their stored repository scopes must match; cross-folder replies still require `--cross-folder`. Unknown historical scopes cannot establish a link. Inbox and history JSON include nullable `reply_to`; old messages remain roots. `history --conversation 123` selects the root and descendants of that message, intersected with the usual scope, cursor, and limit filters. `--peer ADDRESS` matches either participant exactly and composes with conversation filtering. Both options remain read-only.

### Herdr viewer

Herdr 0.9.1+ on macOS or Linux can show history in a terminal tab. Install `llm-bus` as above and ensure both `python3` (3.10+) and `llm-bus` are on Herdr's `PATH`. From a local checkout:

```text
herdr plugin link /absolute/path/to/llm-bus/plugins/herdr-history
herdr plugin pane open --plugin llm-bus.history --entrypoint history
```

To install from GitHub instead of a checkout, run `herdr plugin install wnz99/llm-bus/plugins/herdr-history`.

The viewer starts with the active Herdr workspace repository, using the focused pane folder if the workspace has no folder. Without either, it starts in global view. Press `f` for repository, `g` for all folders, `j`/`k` or arrow keys to scroll, `n`/`p` for older/newer pages, `r` to refresh, and `q` to close. It shows bus messages newest first, with send-time folders, wake results, and pending or acknowledged state. Global view can expose messages from every local folder. The viewer never sends or acknowledges messages; recipients still handle pending messages through `llm-bus inbox` and `ack`.

## Storage and permissions

Sessions running as the same operating-system user share a SQLite mailbox. By default it lives at `~/.local/share/llm-bus/bus.sqlite3` on Linux and macOS, or under `LOCALAPPDATA/llm-bus/bus.sqlite3` on Windows. If `LOCALAPPDATA` is unavailable, Windows uses `AppData/Local` under the user profile. Set `LLM_BUS_DB` to use another path. The installed CLI works from any folder on the computer.

Mailbox upgrades are additive and transactional. A current mailbox skips schema migration work; an older CLI rejects a mailbox with a newer schema version. Inbox reads scan bounded pages from the oldest pending message each time, skip remote messages denied by current machine policy, and retain them in history. Transport updates preserve delivery and acknowledgement progress when bridge responses arrive out of order; delivery takes precedence over unreceived expiry.

After host setup, the bus itself asks for no per-message approval. Claude's installed allow rule covers direct `llm-bus` commands; shell pipelines and other commands may still prompt, and managed or explicit deny rules can override it. See [Claude Code permissions](https://code.claude.com/docs/en/permissions).

Codex allows `llm-bus` commands through a rule in a `~/.codex/rules/*.rules` file: `prefix_rule(pattern=["llm-bus"], decision="allow")`. Without that rule, or a `writable_roots` entry (in the `[sandbox_workspace_write]` table of `config.toml`) for the mailbox folder, the Codex sandbox blocks agent-run `llm-bus` writes under `~/.local/share`. `codex --ask-for-approval never` suppresses command approval prompts for the entire session, not just bus commands, and does not remove sandbox limits. Use it only if that broader permission is acceptable. Transport and ordinary message text grant neither host execution permission. Optional directions stay within explicit owner grants and host permissions. The hooks report pending counts but do not run `inbox` or `ack` for the agent.

Coordination records are machine-local in the mailbox: project IDs/generations,
exact grants, enrollments, target references and retained direction metadata.
Processes sharing one OS user are procedurally trusted; these records are not
an isolation boundary against that account. Revocation stops subsequent guarded
exposure/wakes, retains history, and cannot recall exposed text or submitted wakes.

## Uninstall

Run `llm-bus uninstall` to remove the user-level bus hooks and Claude allow rule and uninstall the `uv` tool. The SQLite mailbox and any pending messages remain on disk.

Revoke active coordination grants/projects on each machine before removing
recovery tooling. Uninstall retains coordination records and native-home
bindings. Copied skills, rule files and pointer blocks require separate
owner-requested removal. Stop bridges and retain backups before rollback; schema 13 mailboxes
reject older clients. Never downgrade directions into ordinary messages.

## Development

Use Python 3.12. CI pins uv 0.13.1; to use the same version without replacing your installed uv, prefix commands with `uv tool run --from uv==0.13.1 uv` instead of `uv`.

```text
sh tools/quality/setup.sh
sh tools/quality/check.sh
```

Application modules live in the `src/llm_bus/` package; tests live in `tests/`. `poe check` runs Ruff formatting and read-only lint, strict BasedPyright, Pylint, deptry, pytest, and the package check. Formatting, lint, types, and Pylint cover application code, tests, the Herdr viewer, and maintained Python tools. Pylint allows fixture literals and exact empty-list assertions in tests; Ruff owns test formatting. Existing viewer contract and platform API exceptions remain local to their declarations.

Run `uv run --frozen poe package-check` for the packaging check alone. It builds an sdist and a wheel from that sdist, installs the wheel into a fresh temporary environment outside the checkout, imports every application module under Python's isolated mode, and checks the installed `llm-bus --help` and `python -I -m llm_bus --help` entry points. Core modules are imported before installing the optional collaboration extra; its runtime modules are imported afterward. The package has no runtime dependencies, so installation uses `--no-deps` to catch accidental reliance on development packages. The temporary environment is removed afterward. This smoke check does not install agent hooks or access the user's mailbox.

Use `uv run --frozen poe format` to apply formatting and `uv run --frozen poe fix` for explicit lint fixes. Checks are read-only. Setup installs repository Git hooks, preserving existing hook owners: pre-commit checks formatting in a staged snapshot; pre-push requires a clean checkout and checks the pushed HEAD with `poe check-static`. CI runs the full check, including tests and package isolation. `poe check-static` also checks uv/Python versions, repository hygiene, and shell scripts.

Setup scripts and hooks adapt the [standard Python profile](https://github.com/wnz99/repo-starters/tree/9c5f517d2ef98af476a4234865376a10ac50e79b/profiles/python). Existing Python 3.12 support, application package layout, quality policy in `pyproject.toml`, and the isolated package consumer remain authoritative.

`.editorconfig` and `.gitattributes` define shared text defaults. Local `.env` files are ignored; secret-free `.env.example` and `.env.*.example` templates may be tracked. Never put credentials in those templates.

## Branch workflow

`main` is the only permanent branch. Create feature and fix branches from `main` and open pull requests back into `main`. Merge with a merge commit and delete temporary branches after their pull requests merge. See [AGENTS.md](AGENTS.md) for contributor rules.

The `Repository / quality` and `Relay / quality` checks run on Linux for pull requests into `main`. Check their results before merging, because quality results are advisory: GitHub permits merging while checks are pending or after they fail. A merge into `main` does not deploy anything.
