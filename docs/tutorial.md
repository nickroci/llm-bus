# llm-bus tutorial

This tutorial walks you, the person who runs Claude Code and Codex agents, through every way you can configure and use `llm-bus`. It starts with one machine and two agents, then adds shared-resource locks, coordinated projects, delegated approvals, owner decisions and finally other machines. Each chapter states its goal, gives the exact commands, shows the shape of the output and tells you what to check.

The [README](../README.md) remains the reference for local installation and behaviour, and the [remote guide](remote-bus.md) remains the operator guide for the relay. This tutorial links to them for detail instead of repeating it.

- [1. Concepts](#1-concepts)
- [2. Install, upgrade and verify](#2-install-upgrade-and-verify)
- [3. Messages between local sessions](#3-messages-between-local-sessions)
- [4. Shared-resource locks](#4-shared-resource-locks)
- [5. Coordinate a project](#5-coordinate-a-project)
- [6. Delegate approval gates](#6-delegate-approval-gates)
- [7. Merge gate records and auto-merge](#7-merge-gate-records-and-auto-merge)
- [8. Objective amendments and standing instructions](#8-objective-amendments-and-standing-instructions)
- [9. Owner decisions and timed defaults](#9-owner-decisions-and-timed-defaults)
- [10. Connect other machines](#10-connect-other-machines)
- [11. Collaborate with another person (experimental)](#11-collaborate-with-another-person-experimental)
- [12. Configuration reference](#12-configuration-reference)
- [13. Troubleshooting, safety and known limits](#13-troubleshooting-safety-and-known-limits)

## How to read the examples

Most commands in this tutorial run inside an agent's shell tool, because the bus identifies the caller from the session ID that Claude Code or Codex puts in that shell's environment. A few commands, such as `history`, `status`, `agents`, `remote-status`, `monitor`, `bridge` and `coordinate --setup`, also work from your own terminal. Every `coordination` action needs an agent session, including owner actions such as `setup`, `grant`, `revoke` and `replace`, and `join` runs inside the worker's own session. For an owner action, ask an agent session to run it: the coordinator session on its own machine, or on a receiving machine a session that never joined the project.

Agents should put `--format agent` before the command to get compact output, for example `llm-bus --format agent inbox`. The examples in chapters 5 to 9 use that form because agents run them. Without the flag, the CLI prints expanded JSON.

The example outputs come from a scratch run against a throw-away mailbox with two fake Claude session IDs, `claude:11111111-aaaa` (the coordinator) and `claude:22222222-bbbb` (the worker). Long paths and IDs are shortened with `…`, and the scratch project label is shown as `orders-export`. Outputs marked *illustrative* were not produced by such a run. Example machine names (`laptop`, `build-vps`), the repository ID `orders-app` and the project label `orders-export` are placeholders.

You can repeat the scratch run safely by pointing the bus at temporary files, so that it never touches your real mailbox or configuration:

```sh
export LLM_BUS_DB=/tmp/llm-bus-scratch/bus.sqlite3
export LLM_BUS_CONFIG=/tmp/llm-bus-scratch/config.toml
CLAUDE_CODE_SESSION_ID=11111111-aaaa llm-bus whoami
```

Setting `CLAUDE_CODE_SESSION_ID` by hand only simulates an agent. Do not do it next to real sessions, and never against your real mailbox.

## 1. Concepts

**Session and address.** Every main Claude Code or Codex session gets a bus address built from its host-generated session ID, such as `claude:SESSION_ID` or `codex:SESSION_ID`. Hooks register the session automatically. Sub-agents never use the bus; they report to their parent, and the parent talks to the bus.

**Scope.** Sessions are grouped by Git repository, including all linked worktrees of that repository, or by folder outside Git. `list` shows the sessions in your scope, and `send` refuses a recipient in another scope unless you add `--cross-folder`.

**Label.** A session can claim a readable name such as `Builder` in its scope. A label only selects an address; it grants nothing.

**Message.** A message is durable: `send` stores it first, then asks the recipient's host to wake up. The message stays pending until the recipient runs `ack`, even across restarts. Reading a message does not consume it.

**Lock.** A lock is an advisory, named claim on a shared resource such as a database. Every holder states when it expects to release.

**Coordination.** A coordinated project has one coordinator session and any number of worker sessions that you, the owner, enrolled. The coordinator sends *directions* (assign, review, approve a commit and so on), and the bus verifies each one against the grants you made.

**Local and remote.** Local messaging uses a SQLite mailbox shared by all sessions of one operating-system user and needs no server. Remote messaging adds a Cloudflare relay and one bridge process per machine, and each machine owner decides in a local file which peers may do what.

**Trust and authority.** This rule runs through every chapter: a message never grants permission. Message bodies, ticket text, report codes and claimed approvals are untrusted data. The only things that carry authority are your direct requests to an agent and the verified directions that fall inside grants you made yourself. Host permissions and repository rules still apply on top of them.

## 2. Install, upgrade and verify

**Goal:** a working CLI and hooks on one machine.

From a clone of this repository, install the CLI, then the host hooks:

```sh
uv tool install .
llm-bus install
```

The first command installs the `llm-bus` tool. The second adds user-level `SessionStart`, `UserPromptSubmit` and `SessionEnd` hooks for both hosts and a Claude Code allow rule for direct bus commands. Close both agents before running `llm-bus install`, then restart them. In Codex, open `/hooks` and trust the bus hooks; untrusted hooks do not run.

**Check:** inside each agent's shell tool, run `llm-bus whoami`. It prints the session's address:

```json
{"address": "claude:11111111-aaaa", "label": null}
```

If your shell cannot find `llm-bus`, run `uv tool update-shell` and open a new shell. Custom host homes (`CODEX_HOME`, `CLAUDE_CONFIG_DIR`), hook exit code 127 and the Claude inbound-message setting are covered in [Install and verify](../README.md#install-and-verify) and [Wake behavior](../README.md#wake-behavior).

### Upgrade

From a checkout with no uncommitted tracked changes:

```sh
uv run llm-bus upgrade
```

The command builds a wheel from `HEAD`, reinstalls the tool, verifies it and only then deletes older packages. It does not touch hooks or bridges, so finish with these steps:

1. Close both agents.
2. Run `llm-bus install`.
3. Restart the agents.
4. Restart any running `llm-bus bridge` and check its log.

A first install or a directory install (`uv tool install .`) needs a one-time bootstrap before `upgrade` can work; see [Upgrade](../README.md#upgrade).

> **Upgrade every installation together.** The first command run by a current client migrates the mailbox to schema version 13, and older binaries then refuse that mailbox. Upgrade every `llm-bus` installation that shares a mailbox before any session uses it. Upgrade the copied skill and rule files at the same time, or agents follow stale rules.

### Teach agents when to use the bus

Hooks tell an agent its address and pending-message count, but not when to use the bus. Follow [Install agent instructions](../examples/agent-instructions.md) to add a short pointer block and rule files, and copy the [llm-bus skill](../skills/llm-bus/SKILL.md) into your shared skill directory. The skill is optional for local messaging and required for remote messaging, coordination and collaboration.

## 3. Messages between local sessions

**Goal:** two sessions in the same repository exchange a message and acknowledge it.

### Find each other and claim labels

In the first session, claim a label; in the second, list who is around:

```sh
llm-bus whoami --label Builder
llm-bus list
```

```json
{"address": "claude:11111111-aaaa", "label": "Builder"}
[{"address": "claude:22222222-bbbb", "kind": "claude", "project": "/…/repo", "last_seen": "…", "ended_at": null, "label": null},
 {"address": "claude:11111111-aaaa", "kind": "claude", "project": "/…/repo", "last_seen": "…", "ended_at": null, "label": "Builder"}]
```

Labels have 1 to 64 letters, digits, underscores or hyphens, start with a letter or digit, and match without regard to case. A duplicate claim fails. Run `whoami --label NEW_NAME` to rename. A clean session close, a move to another scope or 24 hours of inactivity releases the label.

`list` hides sessions that ended or have been idle for 24 hours. `llm-bus agents` shows every registered session, including ended ones. `list --all` also shows sessions in other repositories and folders.

### Send, read and acknowledge

From the second session, send to the label (or to the full address):

```sh
llm-bus send Builder --body "Migration is ready for review"
```

The CLI prints a one-line summary on standard error and the stored message on standard output:

```text
llm-bus: message #1 stored; wake unsupported: Claude native inbox is unavailable; pending messages are reported by the next turn hook
{"id": 1, "sender": "claude:22222222-bbbb", "recipient": "claude:11111111-aaaa", "body": "Migration is ready for review", …, "reply_to": null, "wake": {"status": "unsupported", …}}
```

In the recipient session, read and then acknowledge:

```sh
llm-bus inbox
llm-bus ack 1
```

```json
[{"id": 1, "sender": "claude:22222222-bbbb", "recipient": "claude:11111111-aaaa", "body": "Migration is ready for review", …}]
{"acknowledged": 1}
```

**Check:** the `wake.status` field of the send result. It tells you whether the bus could ask the recipient's host to start a turn:

| `wake.status` | Meaning |
| --- | --- |
| `requested` | The host accepted the wake request; handling is not confirmed. |
| `coalesced` | The message shares a wake request already in progress or scheduled. |
| `failed` | The message is stored, but the wake failed. |
| `unknown` | The request timed out; the host may still have received it. |
| `unsupported` | No automatic wake; the recipient's hook reports the message on its next turn. |

In every case the message is stored, so do not resend it. The scratch run reports `unsupported` because fake sessions have no Claude inbox socket; a real Claude Code session with a registered inbox socket can report `requested`. Codex wakes through `codex queue --resume`. [Wake behavior](../README.md#wake-behavior) explains the host requirements.

### Reply in a thread and read history

To link a reply to an earlier message, pass its local ID:

```sh
llm-bus send claude:22222222-bbbb --reply-to 1 --body "Reviewed; two comments in the PR"
```

`history` reads stored messages without acknowledging anything and needs no session, so you can run it from your own terminal:

```sh
llm-bus history
llm-bus history --repository /path/to/repo
llm-bus history --all --before 123 --limit 50
llm-bus history --conversation 1 --peer claude:22222222-bbbb
llm-bus status 1
```

`history` returns at most 100 messages per call, newest first; use `--after ID` to poll for newer ones. `status MESSAGE_ID` prints body-free diagnostics for one message: transport state, wake result, acknowledgement time and synchronization data. See [Browse message history](../README.md#browse-message-history) for all filters and the Herdr viewer.

### Recover guidance after compaction

`llm-bus guidance` prints the full identity, trust and usage guidance that the `SessionStart` hook normally injects, without changing any message state. Both hosts fire `SessionStart` after compaction, so guidance returns without a manual step; run `llm-bus guidance` to recover it by hand. A Codex `fork` gets a new thread ID and a Claude `/clear` a new session ID, so each is a new bus address.

## 4. Shared-resource locks

**Goal:** two sessions that share a database take turns on it.

Locks are advisory and only work if every session uses the same name for the same resource. Start by looking at the catalog of known names:

```sh
llm-bus lock list
llm-bus lock status
```

```json
{"resources": []}
{"locks": []}
```

Take a lock with an estimate of when you can release it. The first use of a new name must also say what the name protects:

```sh
llm-bus lock acquire db:orders --check 45m --describe "orders Postgres database" --reason "running migration 0042"
```

The result is `acquired` for a free resource and `renewed` when you already hold it. When another live session holds it, you are queued, the result is `waiting` and the command exits with status 3, so `lock acquire NAME && command` never runs the command without the lock. To ask the holder now instead of waiting for its check time, add `--notify`. To leave the queue when you no longer need the resource, use `cancel`:

```sh
llm-bus lock acquire db:orders --notify --reason "need to run the export"
llm-bus lock release db:orders
llm-bus lock cancel db:orders
```

*The acquire results above are described, not shown: the scratch run skipped `acquire`, because it starts a detached timer process.*

The flags work as follows:

| Flag | Effect |
| --- | --- |
| `--check 45m` | Your estimate as minutes, hours or days (`45m`, `2h`, `1d`). At that time the bus sends you a `lock check` message; release or acquire again with a new estimate. Required when acquiring a free resource. |
| `--describe TEXT` | What the name protects. Required the first time a name is used. |
| `--new` | Confirms a name that resembles an existing one, such as `orders-db` next to `db:orders`. Without it, the bus refuses and shows the existing name. |
| `--reason TEXT` | Why you need the resource; shown to the holder. |
| `--notify` | Messages and wakes the holder now. Repeating it re-asks at most once every five minutes. |
| `--ttl MINUTES` | Lease length, default 1440 (24 hours), at most 7 days. The lease ends at once when the holder's session ends. |
| `--shared` | Uses a machine-wide name instead of one scoped to this repository or folder. Use it on `release` and `status` too. |

On `release`, or when a lease lapses, the resource passes to the earliest live waiter, which receives a `lock granted` message. `lock status --all` and `lock list --all` cover every repository and folder.

A killed agent fires no close hook, so the bus also records each session's `claude` or `codex` host process. Every lock command checks the processes of holders and waiters. If one has exited, the bus ends that session and passes its locks on. Like a close hook, this also ends the session's labels and its coordination join, so a resumed worker needs a fresh `join`. A Codex thread inside a shared app-server process is not detected this way.

Normally only the holder can release. For a holder that is stuck, the owner can force a release from an interactive terminal that is not running inside Claude Code or Codex:

```sh
llm-bus lock release db:orders --force --reason "holder session was killed"
```

The active coordinator of the lock's project, or of the holder's project, can run the same command from its session. The old holder is told the reason, the lock passes to the next waiter, and the release is recorded with who forced it and why.

**Check:** `lock status` shows holders, check times, leases and queues. Locks do not cross machines. For copyable lock-name tables, see the [lock instruction examples](../examples/resource-locks.md).

## 5. Coordinate a project

**Goal:** one coordinator session directs work in worker sessions, within limits you set.

For an overview first, open the [step-by-step coordination diagram](diagrams/coordination-steps.html) in a browser from a local checkout.

Coordination is opt-in and uses Beads for durable project memory. Before you start, make sure that:

- the repository already has a Beads workspace (a `.beads` folder), and you know the ID of the project memory ticket;
- the [llm-bus skill](../skills/llm-bus/SKILL.md) and the optional [coordination rules file](../examples/instructions/llm-bus-coordination.md) are installed;
- you start every session yourself. Agents never start or enrol other sessions.

### Start the coordinator

In the session that will coordinate, type `/llm-bus coordinate PROJECT` (or ask in plain words). The agent then runs a command like this one:

```sh
llm-bus --format agent coordinate orders-export \
  --objective "Ship the orders export" \
  --beads-workspace /path/to/repo \
  --memory-ticket demo-1 \
  --reference /path/to/repo/AGENTS.md
```

The result describes the new project. Shortened from the scratch run:

```json
{"project_id": "cdf6628c-…", "label": "orders-export", "generation": "2c328be7-…",
 "coordinator": "claude:11111111-aaaa", "objective": "Ship the orders export",
 "beads_workspace": "/…/repo", "memory_ticket": "demo-1",
 "grants": [], "enrollments": [{"address": "claude:11111111-aaaa", "role": "coordinator", …}],
 "targets": [], "guidance": "…"}
```

The project ID and generation are random UUIDs. Running `coordinate` again from the same session resumes the project. A resume with a different `--objective` fails with `Objective differs from the stored objective; use coordination amend PROJECT --objective`, and a different session trying to coordinate the same project fails until you replace the coordinator. `--beads-workspace` must be an existing absolute path containing `.beads`; otherwise the command fails with `Coordination requires an existing Beads workspace`.

### Join workers

In each worker session, type `/llm-bus join PROJECT`. The agent runs:

```sh
llm-bus --format agent join orders-export
```

Your direct join request is your consent for that worker to accept directions from the current coordinator for the current objective. The result lists the new grant, enrollment and target:

```json
{"grants": [{"recipient": "claude:22222222-bbbb", "grant_id": "3faf4315-…",
   "operations_json": "[\"assign\", \"reprioritize\", \"clarify\", \"pause\", \"cancel\", \"review\"]",
   "expires_at": null, "revoked_at": null, …}],
 "enrollments": [… {"address": "claude:22222222-bbbb", "role": "worker", …}],
 "targets": [{"recipient": "claude:22222222-bbbb", "grant_id": "3faf4315-…", …}], …}
```

The coordinator receives one informational message: `Worker claude:22222222-bbbb joined coordination project orders-export. Availability is informational; recheck current authority before assignments.`

This automatic consent lasts through turns and compaction. It ends when the worker session closes, after 24 hours of inactivity, or on `leave`, `revoke` or replacement of the coordinator generation. Moving the session to a folder outside the project does not end it. Until the session works in the project again, directions do not verify (a send is refused with a folder mismatch error, or with `wrong_scope` under `--cross-folder` and at delivery) and hook recovery shows state `out_of_scope`. To enroll in a project elsewhere, first run `leave` from a folder of the original project; the join error names the repository or folder and the exact command. One session can be enrolled in one project.

If you want a grant with an explicit lifetime instead, ask the coordinator session to run `coordination grant` before the worker joins. `grant` is an owner action: run it only on your direct request (see [owner actions](#inspect-stop-and-replace)).

```sh
llm-bus --format agent coordination grant orders-export --recipient claude:22222222-bbbb --until-revoked
llm-bus --format agent coordination grant orders-export --recipient claude:22222222-bbbb --expires-at 2026-12-01T18:00:00+01:00
```

### Send directions

The coordinator sends a direction by adding `--project` and `--direction` to an ordinary send. Both flags are required together:

```sh
llm-bus --format agent send claude:22222222-bbbb --project orders-export --direction assign \
  --body "Task demo-7. Add CSV export. Report evidence."
```

The allowed directions are `assign`, `reprioritize`, `clarify`, `pause`, `cancel` and `review`.

**Check:** the worker's inbox entry carries a `coordination` object. The worker acts on it only when `verified` is `true` and `reason` is `verified`:

```json
{"id": 3, "sender": "claude:11111111-aaaa", "body": "Task demo-7. Add CSV export. Report evidence.",
 "coordination": {"version": 1, "project_id": "cdf6628c-…", "generation": "2c328be7-…",
   "grant_id": "3faf4315-…", "operation": "assign", "verified": true, "reason": "verified"}}
```

A direction that no longer matches a current grant stays in history with `verified: false` and a reason, and it is hidden from pending work. Workers report results with ordinary messages (`send … --reply-to ID`), which carry no authority.

### Track work state

A worker that has acknowledged a direction records its state with `report`. Recording stores the state on the worker's machine; it sends no message and wakes nobody:

```sh
llm-bus --format agent report 3 --status received
llm-bus --format agent report 3 --status proceeding --reason started
llm-bus --format agent report 3 --status not_proceeding --reason dependency_blocked
llm-bus --format agent report 3 --status completed
```

`report` takes only the built-in status and reason codes; free text is refused. Your own words map to codes like this:

| In words | Code |
| --- | --- |
| accepted | `received` |
| started | `proceeding` with reason `started` (or `resumed`) |
| blocked | `not_proceeding` with a reason such as `dependency_blocked` or `needs_approval` |
| done | `completed` |
| failed | `failed` with a reason such as `execution_failed` |

A report is refused for an unacknowledged direction, for a message the caller did not receive, for a change that moves a finished direction back, and for a change back to `received`. Repeating the same report changes nothing. `status ID` then shows the latest state, for example `"work_report": {"message_id": 3, "revision": 2, "status": "proceeding", "reason": "started", "reported_at": "…"}`.

The coordinator lists directions with their state, newest first, at most 100:

```sh
llm-bus --format agent coordination direction-list orders-export --open
```

Each item has `message_id`, `recipient`, `operation`, `sent_at`, `acknowledged_at`, `remote_id`, `work_report` and `state_changed_at`. `state_changed_at` is the report time, or the acknowledgement time when there is no report. It is null for an unacknowledged direction and for an outgoing relay direction whose relay report has a status; a relay direction with no report yet keeps its acknowledgement time. `--open` keeps acknowledged `assign`, `review` and `reprioritize` directions that have no `completed` or `failed` report and are not cancelled. Without the flag, the list also holds unacknowledged, finished and cancelled directions. The coordinator judges each open direction's age and follows up with a `clarify` direction when one looks stalled. `coordination show` adds a `directions` list when any direction is unacknowledged or open. The coordinator sees the directions it sent; any other session sees only directions addressed to itself.

### Inspect, stop and replace

```sh
llm-bus --format agent coordination show orders-export
llm-bus --format agent coordination leave orders-export
llm-bus --format agent coordination revoke orders-export --recipient claude:22222222-bbbb
llm-bus --format agent coordination revoke orders-export
llm-bus --format agent coordination replace orders-export --coordinator codex:NEW_SESSION_ID --generation FRESH_UUID4
```

- `show` prints the project, grants, enrollments, targets, delegation, bindings and, when present, gate records, amendments, instructions and `directions` (unacknowledged or open, see [Track work state](#track-work-state)).
- `leave` removes only the calling session's enrollment.
- `revoke --recipient` removes one worker; `revoke` without it suspends the project.
- `replace` moves the project to a new coordinator and a never-used UUIDv4 generation. It invalidates existing grants, enrollments, targets, delegation and bindings, so workers must join again.

Revoke, leave and replace keep history and also clear the affected workers' ticket and branch claims. They cannot recall text an agent already read or a wake already submitted.

To stop work without revoking, send a `cancel` direction. Once the worker acknowledges it, every earlier `assign`, `review` and `reprioritize` direction under the same grant leaves `--open` and `show`. The worker need not report on them. It messages the coordinator only when it must act on or learn something, such as uncommitted work. Resend any direction that should continue after a cancel.

`grant`, `revoke`, `replace`, `target`, `amend` and `setup` are owner actions: an agent runs them only when you ask it directly. The bus enforces the roles:

- On a local project, only the coordinator may run `grant`, `revoke`, `target` and `amend`. That is the enrolled coordinator, or the session named as coordinator before it enrolls, if it was never a worker. Other sessions get errors such as `Only the project's coordinator may grant`.
- `replace` and `setup` are refused to any session that is or was a worker of the project's current generation: `A worker of this project cannot replace the coordinator`. Leaving or being revoked does not lift this, because the bus keeps every grant record.
- A session that never was a worker can still run `replace`, so you can recover from a lost coordinator from a fresh session. The rule covers the current generation only: after `replace`, workers of the old generation count as fresh sessions until they join again.

The bus cannot tell whether you typed a command or an agent chose to run it, so the agent rules still matter. See [Known limits](#known-limits).

`coordination target PROJECT --recipient ADDRESS --grant-id GRANT_ID` records where the coordinator sends directions to one grant's recipient. `join` records it for you, so you need `target` only for advanced explicit routing, such as a remote worker; see the [coordination reference](../skills/llm-bus/references/coordination.md).

The [canonical coordination reference](../skills/llm-bus/references/coordination.md) covers recovery, Beads memory duties and explicit targets. The [Claude coordinator with three Codex workers example](../examples/claude-coordinator-codex-workers.md) shows a complete session.

## 6. Delegate approval gates

**Goal:** let the coordinator approve commits, pushes and merges for you, on branches you choose.

Workers route questions, approval requests and manual-action requests through the current coordinator by default, even without delegation. The coordinator asks you with the worker's identity, session banner and ticket/branch, records the outcome and follows up. You may directly override this communication default. Delegation lets the coordinator supply some approvals under existing contracts. Two owner choices must agree before an approval counts:

1. **The coordinator side:** which gates the project delegates, and for which target branches.
2. **The worker side:** what each worker accepts when you join it, optionally narrowed.

The gates are `commit`, `push`, `merge` and `merge-admin`. `merge-admin` bypasses only the required-approvals rule; required checks must still pass. A delegation item has the form `GATE:BRANCH`, where `BRANCH` is the integration branch the work lands in, such as `main`. Each gate can name at most 8 branches, and there is no implicit default branch.

> **The floor is never delegable.** Force-push, deploys or production changes, credential or secret changes, paid or live runs, and merging with failing or pending CI always need you directly. The coordinator collects the request and directs you to the exact owning session or machine for that action. `--delegate force-push:main` fails with `Delegation never covers the floor action: force-push`. The bus does not inspect CI, reviews or branches itself; the worker checks that evidence live before it acts.

Routing does not grant consent or host permission. Fresh joins and local authorization still need the appropriate owner's direct action; native permission prompts remain native. The worker stays blocked until its existing authority and verification requirements are met. Delivery, acknowledgements and a claim that an action happened do not replace those checks. If the coordinator is unreachable or authority cannot be verified, the worker preserves the blocker and yields for user-directed recovery.

### Choose the delegation with the setup profile or wizard

Ask the coordinator agent to run the setup profile. It stores nothing and prints a suggestion for you to confirm:

```sh
llm-bus --format agent coordinate orders-export --setup
```

```json
{"delegate": [], "flags": ["owner-decisions", "decision-defaults"],
 "integration_branch": {"name": null, "source": "undetected"},
 "floor": ["force-push", "deploys or production changes", "credential or secret changes", "paid or live runs", "merge with failing or pending CI"],
 "steps": ["Check the repository instructions for an integration branch …", "Ask the owner through the native question UI: …", "…"],
 "command": "llm-bus --format agent coordinate PROJECT --objective OBJECTIVE --beads-workspace PATH --memory-ticket TICKET --delegate GATE:BRANCH ... --owner-decisions --decision-defaults 30"}
```

The scratch repository had no `origin/HEAD`, so the profile detected no integration branch and suggested no gates. In a repository with a remote, `delegate` lists every gate for the detected branch and `flags` also offers `auto-merge`. The agent asks you each question through its native question UI, and then runs only the flags you confirmed.

You can also run the interactive wizard yourself, in your own terminal, from the repository folder:

```sh
llm-bus coordinate orders-export --setup
```

The wizard asks for the objective, Beads workspace, memory ticket and integration branch; then `Delegate commit? [yes]/no:` for each gate, `Delegate auto-merge?` when a merge gate is kept, `Delegate owner decisions?` and the minutes for decision defaults (Enter keeps 30, `no` turns them off). Outside an agent session it lets you pick the coordinator from the registered sessions in that folder. It shows a review screen and stores the setup only when you type `yes`. Ctrl-C or end of input stores nothing.

Without the wizard, pass the choices directly:

```sh
llm-bus --format agent coordinate orders-export --objective "Ship the orders export" \
  --beads-workspace /path/to/repo --memory-ticket demo-1 \
  --delegate commit:main --delegate push:main --delegate merge:main --auto-merge --owner-decisions
```

The result gains a `delegation` object:

```json
"delegation": {"version": 1, "gates": {"commit": ["main"], "merge": ["main"], "push": ["main"]}, "auto_merge": true, "owner_decisions": true}
```

### Accept, or narrow, on the worker side

A plain `join` accepts the full current delegation. To give one worker less, name the items it may accept with `--narrow`, once per item. An item is a `GATE:BRANCH` or one of the flags `auto-merge`, `owner-decisions` or `decision-defaults`, and it must already be delegated by the project. Items you leave out are dropped:

```sh
llm-bus --format agent join orders-export --narrow commit:main --narrow push:main
```

`coordination show` lists each worker's binding by grant ID:

```json
"bindings": {"3faf4315-…": ["commit:main", "push:main"]}
```

You decide the narrowing; a worker never widens its own binding. A later plain rejoin accepts the full current delegation again.

### Claim, approve, act

The worker first claims its ticket and branch. Only one worker holds a ticket or branch at a time:

```sh
llm-bus --format agent coordination claim orders-export --ticket demo-7 --branch feat/orders-export
```

The coordinator then sends an approval, which is a structured direction:

```sh
llm-bus --format agent coordination approve orders-export --recipient claude:22222222-bbbb \
  --gate commit --ticket demo-7 --branch feat/orders-export --base main
```

The flags depend on the gate:

| Gate | Extra flags |
| --- | --- |
| `commit` | None. `--head-sha` and `--pr` are not accepted. |
| `push` | `--head-sha FULL_SHA`, no `--pr`. |
| `merge`, `merge-admin` | Both `--pr NUMBER` and `--head-sha FULL_SHA`. |

`--head-sha` is a full 40 or 64 character hex commit ID. `--expires-in` takes values such as `30m` or `2h`; the default is `2h` and the maximum is `24h`.

**Check:** the worker's inbox entry has `operation: "approve-commit"`, `verified: true` and an `approval` object:

```json
{"id": 4, "body": "Approval commit: ticket demo-7, branch feat/orders-export into main. Verify live evidence before acting.",
 "coordination": {"operation": "approve-commit", "verified": true, "reason": "verified",
   "approval": {"ticket": "demo-7", "branch": "feat/orders-export", "base": "main", "expires_at": "…"}, …}}
```

An approval verifies only when the gate and base are in both the project delegation and the worker's binding, the work branch is neither the base nor a delegated target, it has not expired, and the worker's claim matches the ticket and branch. In the scratch run, approving `merge` for the worker narrowed to `commit` and `push` failed at once with `Direction denied: gate_not_delegated`. Other reasons you may see are `target_denied`, `expired`, `wrong_ticket`, `invalid_direction`, `revoked`, `wrong_scope` and `stale_generation`.

Before acting, the worker checks live evidence itself: the current branch and head SHA for commit and push, and for a merge the PR's base, head and state, every required check passing, and review evidence without unresolved high or medium findings. The exact checks are in [Gate approvals](../skills/llm-bus/references/coordination/gates.md#gate-approvals-local-projects).

### Change the delegation later

Only the enrolled coordinator changes the delegation, and an amend replaces the whole set:

```sh
llm-bus --format agent coordination amend orders-export --delegate commit:main --delegate push:main
llm-bus --format agent coordination amend orders-export --no-delegation
```

Repeat every flag you want to keep: an amend without `--auto-merge`, `--owner-decisions` or `--decision-defaults` removes it. Narrowing applies at once and trims worker bindings. Widening reaches a worker only when you join it again; the result's `rejoin_required` field lists those workers. A resume with `coordinate … --delegate` may only repeat the stored delegation.

After `replace`, the new generation starts with no delegation and no bindings, so the new coordinator runs `amend --delegate …` and every worker joins again.

## 7. Merge gate records and auto-merge

**Goal:** a worker asks for a merge approval as soon as its review passes, without waiting for the coordinator's next turn.

This needs a project with `--auto-merge` and a `merge` or `merge-admin` gate, and a worker whose binding includes `auto-merge`. The flow has three steps.

**1. The worker submits a gate record** after its review loop passes. Its claim must match the ticket and branch:

```sh
llm-bus --format agent coordination gate-record orders-export --ticket demo-7 --branch feat/orders-export \
  --base main --pr 12 --head-sha 0123456789abcdef0123456789abcdef01234567 \
  --review-evidence docs/reviews/demo-7.md --required-check quality
```

```json
{"record_id": "791bc00f…", "status": "submitted", "expires_at": "…", "superseded": [],
 "wakes": [{"to": "claude:11111111-aaaa", "message_id": 5, "wake": {…}}]}
```

`--review-evidence` takes 1 to 8 references, and `--required-check` takes 1 to 32 check names. The record is bound to that head SHA and expires after 24 hours. A new submission for the same PR and branch supersedes the earlier record, so the worker submits again after every push.

**2. The coordinator verifies and accepts or rejects.** It receives `Gate record 791bc00f… submitted for PR 12 head 0123…4567. Verify live, then run coordination gate-accept.` It reads the record's evidence in `coordination show` under `gate_records`, checks the PR, the review evidence and the check names live, and then runs one of:

```sh
llm-bus --format agent coordination gate-accept orders-export --record-id RECORD_ID --head-sha FULL_SHA
llm-bus --format agent coordination gate-accept orders-export --record-id RECORD_ID --head-sha FULL_SHA --admin
llm-bus --format agent coordination gate-reject orders-export --record-id RECORD_ID
```

`--admin` sends `approve-merge-admin` and is for the case where required approvals are the only blocker. Acceptance fails, changing nothing, with `no_record`, `stale_generation`, `not_submitted`, `expired`, `sha_mismatch`, `gate_not_delegated` or `not_coordinator`.

**3. The worker receives a record-bound approval:**

```json
{"id": 6, "body": "Approval merge: gate record 791bc00f…, ticket demo-7, branch feat/orders-export into main, PR 12, head 0123…4567. Wait for required CI on that SHA, verify live, then merge.",
 "coordination": {"operation": "approve-merge", "verified": true, "reason": "verified",
   "approval": {"pr": 12, "head_sha": "0123…4567", "record_id": "791bc00f…", …}, …}}
```

The worker still waits for required CI on that SHA and runs the live merge checks; acceptance does not replace them. If the record is later rejected or superseded, the approval stops verifying with `record_withdrawn`. Dropping auto-merge only blocks new acceptances; run `gate-reject` to withdraw an approval already sent.

## 8. Objective amendments and standing instructions

**Goal:** change the project objective, and give the coordinator or workers standing rules, without restarting the project.

### Amend the objective

```sh
llm-bus --format agent coordination amend orders-export --objective "Ship the orders export with CSV and JSON"
```

Amendments are append-only. `coordination show` keeps `objective` as the original text and adds `amendments` and `effective_objective`, which is the latest amendment:

```json
{"objective": "Ship the orders export", "effective_objective": "Ship the orders export with CSV and JSON",
 "amendments": [{"amendment_id": "a3a45b14…", "text": "Ship the orders export with CSV and JSON", "recorded_by": "claude:11111111-aaaa", …}],
 "rejoin_required": []}
```

`--objective` can be combined with a delegation change in one command; both apply or neither does. `amend` with nothing to change fails with `amend needs --objective or a delegation change`. Amendments belong to one generation: after `replace`, the effective objective falls back to the original text.

### Set standing owner instructions

Standing instructions are your long-lived rules for one audience, `coordinator` or `worker`. Only the enrolled coordinator can set them, and only because you asked it directly in that session; a worker that tries gets `Only the enrolled coordinator may set instructions`.

```sh
llm-bus --format agent coordination instructions orders-export --audience worker --text "Run the full test suite before every push."
llm-bus --format agent coordination instructions orders-export --audience coordinator --file /path/to/coordinator-rules.md
llm-bus --format agent coordination instructions orders-export --audience worker --text "Also update CHANGELOG.md." --amend
```

Without `--amend` the text replaces that audience's instructions; `--amend` appends a dated entry. The text has 1 to 8192 characters and may not contain control characters other than newline and tab, Unicode format characters, `<<<` or `>>>`.

Each session sees only its own role's text. The text appears verbatim in `llm-bus guidance` and in the `guidance` field of `coordinate` and `join` results. Hooks never print it: every hook carries a one-line pointer with a digest, and the session runs `llm-bus guidance` before acting unless its context already holds the text with that digest. `coordination show` lists the entries under `instructions`; workers see only the worker entries.

## 9. Owner decisions and timed defaults

**Goal:** record your answers to questions that block a worker, so the worker can act on them.

Question routing through the coordinator is the default regardless of this optional capability. Recorded answers count only for bound workers under the acceptance rules below. Without that binding, or for remote decisions unsupported by the transport, the coordinator asks you to give required direct authorization in the exact owning session or machine. An ordinary message or `clarify` direction cannot substitute for an accepted decision or a verified gate approval. Routing adds no silence-based approval or timeout.

Enable this with `--owner-decisions` on `coordinate` or `coordination amend`; it needs no gate. The coordinator records a question, and may record your answer only when you gave it directly in the coordinator session. The bus refuses an answer in a turn that a recognized bus wake started (`Decision answers must come from an owner turn, not a bus wake`); this check is partial, see [Known limits](#known-limits). The refusal names the remedy: type the answer as a new prompt in the coordinator session, then have the coordinator record it in that turn. An answer you give inside a wake-started turn does not count.

```sh
llm-bus --format agent coordination decision-ask orders-export --class scope \
  --question "Include archived orders in the export?" --recommended "No, active orders only" \
  --worker claude:22222222-bbbb
llm-bus --format agent coordination decision-answer orders-export --decision-id DECISION_ID --answer "Active orders only"
llm-bus --format agent coordination decision-ask orders-export --class cost --question "Use the paid geocoding API?" --answer "No"
```

The worker checks the decision before acting:

```sh
llm-bus --format agent coordination decision-show orders-export --decision-id DECISION_ID
```

Before your answer it sees `{"accepted": false, "reason": "not_answered"}`. After it, the result contains the answer, its SHA-256, the recording session and `"accepted": true`. The worker acts only on `accepted: true`.

The classes are `scope`, `security`, `cost`, `data`, `irreversible` and `objective`. An `objective` answer also becomes an objective amendment. Text fields take 1 to 4096 characters with the same character limits as instructions.

### Timed defaults

`--decision-defaults MINUTES` (1 to 1440, requires `--owner-decisions`) lets an unanswered `scope` decision that has a `--recommended` option take that option after the given minutes. No other class ever defaults. At the deadline, the bus stores the default and wakes the coordinator and the worker named with `--worker`. A worker narrowed away from `decision-defaults` sees `accepted: false` with reason `default_not_bound` and waits for your answer.

A detached timer applies the default. If the timer did not run, the next `show`, `amend`, `gate-record`, `gate-accept`, `gate-reject`, `decision-show` or `decision-list`, or the coordinator's next prompt, applies it. A defaulted decision is final: if you disagree, record a new decision.

**Check:** list every default the bus took for you:

```sh
llm-bus --format agent coordination decision-list orders-export --defaulted
```

When the coordinator runs `coordination show` and defaults exist, the result also has a `decision_digest` key. The coordinator's recovery guidance repeats a digest line.

## 10. Connect other machines

**Goal:** agents on your laptop and on a VPS exchange messages, under rules each machine owner sets locally.

### What you need

Remote messaging has three parts, each with its own owner:

| Part | Who sets it up | Where it is documented |
| --- | --- | --- |
| Cloudflare relay (Worker, D1, Queues), machine tokens and repository grants | The relay operator | [Deploy the relay](remote-bus.md#deploy-the-relay) and [Issue one static token per machine](remote-bus.md#issue-one-static-token-per-machine) |
| `config.toml` with the local peer policy | Each machine owner | This chapter and [Configure each machine](remote-bus.md#configure-each-machine) |
| One `llm-bus bridge` process per machine | Each machine owner | [Run and use the bridge](remote-bus.md#run-and-use-the-bridge) |

The relay authenticates machines and admits them to shared repository IDs. Admission does not let anyone deliver text to your agents: your local policy decides that, and it denies everything you have not listed.

### Write the local policy

The file lives at `~/.config/llm-bus/config.toml` unless `LLM_BUS_CONFIG` points elsewhere. This example is for a laptop that sends tasks to a VPS, accepts their text into its agents, takes back some reports and lets a coordinator on the VPS approve commits and pushes:

```toml
policy_version = 2

[remote]
url = "https://bus.example.com"
machine_id = "laptop"
credential_file = "/absolute/private/laptop.token"

[[repositories]]
path = "/Users/alice/Projects/orders-app"
repo_id = "orders-app"

[[repositories.peers]]
machine_id = "build-vps"
send_messages = true
receive_messages = "inbox"
send_reports = []
receive_reports = ["received", "completed", "failed"]
accept_delegated_gates = ["commit", "push"]
```

The top-level keys are:

| Key | Meaning |
| --- | --- |
| `policy_version` | Must be `2`. Any other value or a missing value is an error. |
| `[remote] url` | The relay origin. HTTPS is required; plain HTTP is allowed only on loopback. No path, query or credentials. |
| `[remote] machine_id` | This machine's ID as registered at the relay. |
| `[remote] credential_file` | Private token file, owned by you, with no group or other permissions. Relative paths resolve from the configuration folder. |
| `[[repositories]] path` | The local checkout. Map each Git repository once; linked worktrees share the mapping. |
| `[[repositories]] repo_id` | The shared repository ID the operator granted. |

Each `[[repositories.peers]]` entry applies to one exact peer machine in that repository:

| Key | Default | Values and effect |
| --- | --- | --- |
| `machine_id` | (required) | The peer machine. It cannot be this machine. |
| `send_messages` | `false` | `true` lets your agents send normal text to this peer. |
| `receive_messages` | `"deny"` | `"deny"` stores nothing. `"monitor"` stores incoming text in a separate history that only you read with `llm-bus monitor`; it never reaches agents, pending counts or wakes. `"inbox"` delivers text to the native agent inbox and wake path. |
| `send_reports` | `[]` | Report statuses your agents may send back on messages from this peer. |
| `receive_reports` | `[]` | Report statuses you accept on your outgoing messages to this peer. |
| `accept_delegated_gates` | `[]` | Gates (`commit`, `push`, `merge`, `merge-admin`) a coordinator on this peer may approve here. Requires `receive_messages = "inbox"`. |

Report statuses are `received`, `proceeding`, `not_proceeding`, `completed` and `failed`, each with a fixed set of reasons; see [Structured work reports](remote-bus.md#structured-work-reports).

Rules that follow from the code:

- An omitted key denies. An unlisted peer is denied. There are no wildcards and no inferred reverse permissions: `send_messages = true` on your side gives the peer nothing.
- Unknown keys, duplicate peers, duplicate repository mappings and self-machine peers are errors.
- If the file is missing, remote paths are disabled and local messaging keeps working. If `LLM_BUS_CONFIG` points to a missing or invalid file, remote commands fail closed.

**Check:** `llm-bus remote-status` validates the file without contacting the relay. In the scratch run, the policy above returned `[]`; a peer with `receive_messages = "monitor"` and a gate list failed with `accept_delegated_gates needs receive_messages = "inbox"`; and a missing file named by `LLM_BUS_CONFIG` failed with `Invalid or unavailable remote configuration TOML`. Without any configuration, `remote-status` fails with `Remote command requires a remote configuration file`.

### Run the bridge and send

```sh
llm-bus bridge
llm-bus bridge --once
llm-bus bridge --once --refresh-status
```

Run `llm-bus bridge` continuously, in its own terminal or under a process supervisor. `--once` performs one real synchronization pass and prints `uploaded`, `imported`, `acknowledged` and `errors` counts; it is not a dry run. `--refresh-status` forces status and report reads and requires `--once`.

Inside an agent session, remote addresses have the form `MACHINE/REPO/NATIVE_ADDRESS`, or `MACHINE/REPO/LABEL` for a label:

```sh
llm-bus list
llm-bus send build-vps/orders-app/codex:SESSION_ID --body "Migration is ready for review"
llm-bus remote-status
llm-bus report 42 --status completed
llm-bus monitor
```

A remote send returns `queued_local`; the bridge uploads it later. `remote-status` then shows `accepted_remote`, `delivered_local`, `acknowledged` or `expired`. `report` lets a recipient answer with a fixed status code on an imported message, if both policies allow it. `monitor` reads the human-only history from your terminal. All of these are explained in [Run and use the bridge](remote-bus.md#run-and-use-the-bridge) and [Human monitoring](remote-bus.md#human-monitoring).

Policy changes take effect on the next check. Blocking a peer keeps stored history but hides its pending messages and stops future wakes; see [Policy changes](remote-bus.md#policy-changes).

### Coordinate across machines

A coordinator on one machine can direct a worker on another. The receiving owner sets this up locally, because grants never travel between machines:

1. Both machines allow the traffic: the coordinator's machine has `send_messages = true` for the worker's machine, and the worker's machine has `receive_messages = "inbox"` for the coordinator's machine.
2. Ask an agent session on the receiving machine that is not a worker of the project to import the project with the coordinator's project ID, generation and exact qualified address, using local paths. Run `setup` only on the receiving owner's direct request:

   ```sh
   llm-bus --format agent coordination setup orders-export --project-id PROJECT_UUID --generation GENERATION_UUID \
     --coordinator build-vps/orders-app/codex:COORDINATOR_ID --objective "Ship the orders export" \
     --beads-workspace /path/to/local/repo --memory-ticket demo-1
   ```

3. In the worker session on that machine, type `/llm-bus join orders-export`. The agent runs `llm-bus --format agent join orders-export`.

To let that remote coordinator approve gates, four independent checks must all cover the gate:

- the coordinator owner's own project delegation, checked on the coordinator's machine;
- the receiving owner's delegation, added with `coordination setup … --delegate GATE:BRANCH`;
- the receiving machine's `accept_delegated_gates` list for that peer;
- the worker's own join binding.

An approval must also expire within 24 hours of admission. Later, the receiving owner changes its side with `coordination amend PROJECT --delegate GATE:BRANCH` or `--no-delegation`; on a remote-coordinated project `amend` accepts only those two options.

No local session is the coordinator of a remote-coordinated project. On the receiving machine, `setup`, `replace`, `amend`, `grant`, `revoke` and `target` are therefore refused to any session that is or was a worker of the project's current generation, and allowed to every other session. Run them from a session that never joined the project.

Gate records, auto-merge, owner decisions and standing instructions stay on the coordinator's machine. See [Optional project directions](remote-bus.md#optional-project-directions) and [Remote gate approvals](remote-bus.md#remote-gate-approvals).

### Upgrade order

Upgrade in this order, or new features stay queued with codes such as `unsupported_coordination` or `unsupported_approvals`:

1. Stop every bridge and back up the mailboxes, configuration and D1.
2. The operator applies all relay migrations (through `0007_coordination_approvals.sql`), then deploys the matching Worker. Deploying a Worker before its migration can break requests.
3. Each machine owner runs `uv run llm-bus upgrade`, `llm-bus install` and restarts the agents.
4. Each owner runs `llm-bus bridge --once`, checks `errors` and `remote-status`, then restarts the supervised bridge.

Never strip `--project` or `--direction` and resend a queued item as ordinary text to get around an old relay. The full procedure, including rollback, is in [Migration and rollback](remote-bus.md#migration-and-rollback).

## 11. Collaborate with another person (experimental)

`llm-bus collab` connects your agents with another person's agents for one repository, point to point, without the Cloudflare relay. Each person proves a GitHub identity with an SSH signing key. Install the optional extra with `uv tool install --force '.[collab]'`, then:

```sh
llm-bus collab setup --github-user YOUR_GITHUB_USER
llm-bus collab daemon
llm-bus collab invite
llm-bus collab join INVITE
llm-bus collab send COLLABORATOR_GITHUB_USER
llm-bus collab status
```

For each collaborator, you choose on your private owner page what their messages may cause on your machine: `monitor`, `isolated`, `readonly`, `readwrite` or `full`. Below `full`, unsolicited text from them never reaches your main agent automatically. If you enable agent answers for a peer, the first reply to each question your agent sent is delivered to it, labeled untrusted. The setup steps, modes and threat model are in [Collaborate with another person](../README.md#collaborate-with-another-person-experimental), the [collaboration plan](collab-plan.md) and the [collab skill reference](../skills/llm-bus/references/collab.md).

## 12. Configuration reference

### Environment variables

| Variable | Default | Effect | Chapter |
| --- | --- | --- | --- |
| `LLM_BUS_DB` | `~/.local/share/llm-bus/bus.sqlite3` (Windows: `LOCALAPPDATA/llm-bus/bus.sqlite3`) | Mailbox path. Once remote state exists, a mailbox is bound to one relay URL and machine ID; use another path to change that identity. | [2](#2-install-upgrade-and-verify) |
| `LLM_BUS_CONFIG` | `~/.config/llm-bus/config.toml` | Remote policy file. If set, the file must exist and be valid for remote commands. | [10](#10-connect-other-machines) |
| `CODEX_HOME` | Codex default | Codex configuration folder. Set it before `llm-bus install` and before starting Codex; run `install` for each Codex home you use. | [2](#2-install-upgrade-and-verify) |
| `CLAUDE_CONFIG_DIR` | Claude Code default | Claude Code configuration folder. Set it before `llm-bus install` and before starting Claude Code. | [2](#2-install-upgrade-and-verify) |
| `CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`, `CODEX_SESSION_ID` | Set by the host | Identify the calling session. `CODEX_THREAD_ID` is per thread and is the bus identity; `CODEX_SESSION_ID` is the root session, shared by sub-agents. Never set them by hand next to real sessions. | [1](#1-concepts) |
| `CLAUDE_CODE_MESSAGING_SOCKET` | Set by Claude Code v2.1.224+ | Claude's inbox socket used for wakes. | [3](#3-messages-between-local-sessions) |
| `LLM_BUS_COLLAB_HOME` | `~/.config/llm-bus/collab` | Separate collaboration identity, daemon and owner page. | [11](#11-collaborate-with-another-person-experimental) |
| `LLM_BUS_COLLAB_CLAUDE`, `LLM_BUS_COLLAB_MODEL` | `claude` on `PATH`, Claude's default model | Claude binary and model used by collaboration responders. | [11](#11-collaborate-with-another-person-experimental) |

### Files

| Path | Content |
| --- | --- |
| `~/.local/share/llm-bus/bus.sqlite3` | The mailbox: messages, sessions, locks and coordination records. |
| `~/.local/share/llm-bus/native-hosts.json` | Codex homes recorded by `llm-bus install`. |
| `~/.local/share/llm-bus/packages/`, `~/.local/state/llm-bus/releases/` | Wheels managed by `llm-bus upgrade`. |
| `~/.config/llm-bus/config.toml` | Remote policy (chapter 10). |
| `~/.codex/hooks.json`, `~/.claude/settings.json` | Hooks written by `llm-bus install`. |

### Commands at a glance

| Task | Command | Chapter |
| --- | --- | --- |
| Identity, label, roster | `whoami [--label NAME]`, `list [--all]`, `agents`, `guidance` | [3](#3-messages-between-local-sessions) |
| Messages | `send TO [--body TEXT] [--reply-to ID] [--cross-folder]`, `inbox [--limit N]`, `ack ID…` | [3](#3-messages-between-local-sessions) |
| History and diagnostics | `history [--folder P \| --repository P \| --all] [--before ID \| --after ID] [--limit N] [--conversation ID] [--peer ADDR]`, `status ID` | [3](#3-messages-between-local-sessions) |
| Locks | `lock list [--all]`, `lock status [--all] [--shared] [NAME]`, `lock acquire NAME --check D [--describe T] [--reason T] [--new] [--ttl M] [--notify] [--shared]`, `lock release NAME [--shared] [--force --reason T]`, `lock cancel NAME [--shared]` | [4](#4-shared-resource-locks) |
| Start or join a project | `coordinate PROJECT [--setup] [--objective T] [--beads-workspace P] [--memory-ticket ID] [--reference P] [--delegate G:B] [--auto-merge] [--owner-decisions] [--decision-defaults M]`, `join PROJECT [--narrow ITEM]` | [5](#5-coordinate-a-project), [6](#6-delegate-approval-gates) |
| Directions | `send TO --project PROJECT --direction assign\|reprioritize\|clarify\|pause\|cancel\|review`, `report ID --status S [--reason R]` (worker, local direction), `coordination direction-list PROJECT [--open]` | [5](#5-coordinate-a-project) |
| Project lifecycle | `coordination show\|leave PROJECT`, `coordination grant PROJECT --recipient A (--until-revoked \| --expires-at T)`, `coordination revoke PROJECT [--recipient A]`, `coordination replace PROJECT --coordinator A --generation UUID`, `coordination target PROJECT --recipient A --grant-id ID` | [5](#5-coordinate-a-project) |
| Gates | `coordination claim`, `coordination approve`, `coordination amend` | [6](#6-delegate-approval-gates) |
| Gate records | `coordination gate-record`, `coordination gate-accept [--admin]`, `coordination gate-reject` | [7](#7-merge-gate-records-and-auto-merge) |
| Objective and instructions | `coordination amend PROJECT --objective T`, `coordination instructions PROJECT --audience coordinator\|worker (--file P \| --text T) [--amend]` | [8](#8-objective-amendments-and-standing-instructions) |
| Decisions | `coordination decision-ask`, `decision-answer`, `decision-show`, `decision-list [--defaulted]` | [9](#9-owner-decisions-and-timed-defaults) |
| Remote | `bridge [--once] [--refresh-status]`, `remote-status`, `report ID --status S [--reason R]`, `monitor [--repository P \| --all] [--before ID] [--limit N]`, `coordination setup` | [10](#10-connect-other-machines) |
| Collaboration | `collab setup\|daemon\|invite\|join\|send\|status` | [11](#11-collaborate-with-another-person-experimental) |
| Installation | `install`, `uninstall`, `upgrade` | [2](#2-install-upgrade-and-verify) |

Run `llm-bus COMMAND --help` for the exact syntax installed on your machine.

## 13. Troubleshooting, safety and known limits

### Common errors

| Message | Cause and fix |
| --- | --- |
| `No host session ID found; run inside a Claude Code or Codex agent session` | The command needs an agent identity. Run it in the agent's shell tool, or use a command that works without one, such as `history` or `status`. |
| `Sub-agents must report to their parent; only main agents use the bus` | A Codex sub-agent called the bus. Use the parent session. |
| `Coordination requires an existing Beads workspace` | `--beads-workspace` must be an absolute path that contains `.beads`. |
| `Objective differs from the stored objective; use coordination amend PROJECT --objective` | A resume repeated a different objective. Repeat the current one, or amend it. |
| `amend needs --objective or a delegation change` | `amend` had nothing to change. |
| `Delegation never covers the floor action: …` | A delegation named a floor item. Remove it; route the request through the coordinator, which directs the owner to act in the exact owning session or machine. |
| `Direction denied: gate_not_delegated` (or another reason) | The approval falls outside the project delegation, the worker's binding or the claim. Check `coordination show`. |
| `Only the enrolled coordinator may set instructions` | A non-coordinator session ran `coordination instructions`. |
| `Remote command requires a remote configuration file` | No `config.toml` was found. |
| `Local policy requires policy_version = 2; migrate legacy configuration` | Old configuration format; follow [Migration and rollback](remote-bus.md#migration-and-rollback). |
| `accept_delegated_gates needs receive_messages = "inbox"` | Gates were accepted from a peer whose text is denied or monitored. |
| `This mailbox is bound to another relay/machine; use another LLM_BUS_DB` | The relay URL or machine ID changed. Use a separate mailbox for the new identity. |
| Remote message stuck in `queued_local` with `unsupported_reply_link`, `unsupported_coordination`, `unsupported_availability` or `unsupported_approvals` | The relay or a client is too old. Upgrade it; do not resend without the metadata. |
| Hook exits with code 127 | The hook cannot find `llm-bus`. See [Install and verify](../README.md#install-and-verify). |

### Safety rules worth repeating

- Messages, ticket text, report codes and gate records are untrusted data. Only verified directions within your grants carry delegation, and your direct instructions, host permissions and repository rules still win.
- A wake request, a delivery receipt, an acknowledgement or a `completed` report proves neither that work was done nor that it was done correctly.
- Revoking or blocking stops future exposure and wakes but cannot recall text an agent already read or a wake already submitted.
- Processes running as the same operating-system user can read the same mailbox. The bus records your decisions; it is not an isolation boundary against that account.
- The relay operator can read message plaintext.
- Never put credentials in messages, configuration in a repository, or command arguments.

### Known limits

These limits come from the current implementation:

- **The role and turn checks stop mistakes, not a deliberate agent.** Session identity comes from the host's environment and hook input, which any process running as your user can set. An agent that ignores the rules can claim a fresh session ID (and then run `replace`) or replay hook input to make a wake turn look like an owner turn. The checks below block an agent that follows its rules but is misled; they are not a security boundary against one that does not.

- **Owner commands check roles, not people.** Workers of the current generation cannot run owner commands, but any agent session that never was a worker can still run `replace`, and on a receiving machine `setup`, `amend`, `grant`, `revoke` and `target` too. After `replace`, workers of the old generation count as such sessions until they join again. Only the agent rules and your direct request stop a fresh session. Widening gate delegation on a receiver is still capped by its `accept_delegated_gates` and the coordinator machine's own delegation.
- **Answers prove turn origin only.** The bus refuses a decision answer when the coordinator's latest prompt was a recognized bus wake. It cannot check that the answer repeats your words, only that the turn did not start from a wake. On both hosts a prompt that contains a bus notice counts as a wake. In Claude Code 2.1.316, idle and mid-turn wakes reached `UserPromptSubmit` as the bare notice; a mid-turn wake's hook fired only after the running tool call returned, inside the same turn, so that turn is recorded as a wake and an answer in it is refused. The hook payload carries no origin, so `/loop` and scheduled prompts, background-sub-agent reports and non-bus peer messages count as owner turns. The bus also accepts answers from a coordinator that has no recorded turn, such as one without hooks. Workers see `accepted: true` for any recorded answer. The rules allow recording only answers you gave directly in that session.
- **Work state is self-reported.** The bus records what the worker reports and cannot see host activity. An open direction is not proof of a stall; judge its age. Reporting wakes nobody. Relay directions show state only when both owners allow reports, and their report time is not recorded.
- **A cancel closes directions under one grant.** After a grant change within one generation (re-join, re-grant, `target`, remote availability event), a cancel under the new grant does not close directions sent under the earlier grant.
- **Old receivers.** The capability probe runs between client and relay. A receiver running an older client denies gate approvals on its own.
- **Decision classes are trusted.** The coordinator chooses the class. The bus cannot check that a `scope` question avoids security, cost, data or irreversible matters; the rules tell the coordinator to classify conservatively.
- **The decision digest has no acknowledgement.** The digest line repeats on every coordinator prompt until the coordinator is replaced.
- **Defaults are final.** A decision that took its default cannot be answered afterwards; record a new one.
- **No notice for some parties.** A coordinator session that never registered, or a party on another machine, gets the default applied but no notice. Remote addresses cannot be named with `--worker`.
- **Turn numbers are approximate.** They count every prompt that is not a recognized bus wake.
- **Instructions survive replacement.** Standing instructions are stored per project and survive `replace`. There is no command to clear them yet.
- **Hosts may truncate long guidance.** Long hook output can be cut by the host; `llm-bus guidance` prints the full text.
- **Gate records do not compare the recipient with the submitter.** Today only `gate-accept` creates record-bound approvals, and it always addresses the submitter.
- **Locks are advisory and local.** A session that never calls `acquire` is not stopped, and locks do not cross machines.

To remove the bus, revoke coordination grants first, then follow [Uninstall](../README.md#uninstall).
