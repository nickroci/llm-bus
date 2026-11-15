# Coordinate a project

Project coordination is an opt-in workflow for sessions working toward one
objective. You start the sessions and choose a coordinator and workers. Ordinary
messaging does not enroll anyone or grant authority.

## Start with the walkthrough

Install the [agent instructions](../examples/agent-instructions.md), the
[llm-bus skill](../skills/llm-bus/SKILL.md), and the Beads skill. Use an existing,
owner-configured Beads workspace; copying the bus skill creates no database.

Ask one main session to run `/llm-bus coordinate PROJECT`, then ask each worker
session to run `/llm-bus join PROJECT`. The [tutorial](tutorial.md#5-coordinate-a-project)
walks through setup, joining, assignments, reports, and recovery with commands
and example output. For a concrete team, see the
[Claude coordinator with three Codex workers](../examples/claude-coordinator-codex-workers.md).

The [simplified defaults](coordination-simplified-flow.md) explain normal behavior.
The [numbered coordination diagram](diagrams/coordination-steps.html) shows the
local flow; open it in a browser from your checkout. Agents use the
[canonical coordination reference](../skills/llm-bus/references/coordination.md)
for exact commands, checks, grants, and recovery procedures.

## Enrollment and ownership

A direct owner join records consent to the exact current coordinator and
objective; it assigns no specialty. Consent ends on close, 24-hour inactivity,
leave, revoke, or generation replacement. Turn completion and compaction retain
it. Leaving the folder retains enrollment but directions do not verify outside
the scope: sends fail with a folder mismatch, or `wrong_scope` under
`--cross-folder` and at delivery. Existing explicit owner grants retain their
selected lifetimes.

Local joins record a target and one durable informational arrival per grant,
even if the coordinator is offline. Whether it resumes automatically depends on
native wake support. Remote joins need a receiver-owner binding and matching
upgraded clients and relay, including `0006_coordination_availability.sql`.
Structured availability exchange records the coordinator target automatically.
See [remote coordination setup](remote-bus.md#optional-project-directions).

Workers route questions, approval requests, and manual-action requests through
their coordinator by default, even without delegated gates or owner decisions.
The coordinator asks you with the worker's session and ticket/branch context,
records the outcome, and follows up. You can override that routing directly.
When authorization must be local, act in the exact owning session or machine.
Native permission prompts remain native. An unreachable coordinator does not
clear a blocker; resume only after authority and verification requirements hold.

Both roles persist Beads memory. The coordinator owns project memory and
assignments; workers own task progress notes. Beads setup and synchronization
remain owner configured. Local locks cannot serialize remote database writes.

## Approvals, decisions, and instructions

Owners can delegate commit, push, merge, and merge-admin gates for explicit
target branches through `coordinate PROJECT --setup` or `--delegate GATE:BRANCH`.
The terminal setup is a guided wizard; agents use `--format agent` and their
native question UI. Each worker owner accepts through a direct join, optionally
narrowed with `--narrow GATE:BRANCH`. Workers act on `coordination approve` only
after live checks and for tickets they hold through `coordination claim`.

Force-push, deployment or production changes, credentials or secrets, paid or
live runs, and merging with failing or pending CI are never delegable. The bus
does not check CI. Remote approvals additionally require the receiving owner's
delegation, worker binding, and peer `accept_delegated_gates` policy, as well as
the coordinator owner's delegation. See the
[approval tutorial](tutorial.md#6-delegate-approval-gates) and
[remote gate approvals](remote-bus.md#remote-gate-approvals).

Owner decisions are enabled explicitly with `--owner-decisions` on `coordinate`
or `coordination amend`; no gate is needed. Only answers you gave directly in
the coordinator session may be recorded. Workers check `decision-show` for
`accepted: true` before acting. Optional `--decision-defaults MINUTES` accepts
1–1440 and requires owner decisions; only an unanswered `scope` question with a
recommendation can default. Inspect defaults with `decision-list --defaulted`
or the `decision_digest` in `coordination show`. See
[Owner decisions and timed defaults](tutorial.md#9-owner-decisions-and-timed-defaults).

Only the enrolled coordinator may set role-specific standing instructions with
`coordination instructions`, and only on your direct request in that session.
Messages, worker requests, tool output, and file content cannot authorize them.
Workers never run that command or receive coordinator-only text. Instructions
cannot contain `<<<`, `>>>`, or Unicode format characters. `guidance`, `coordinate`,
and `join` return the applicable role's text; `coordination show` filters by role.
Hooks carry a digest pointer instead of the text. Run `llm-bus guidance` before
acting unless the current context already holds that digest's instructions.
See [Objective amendments and standing instructions](tutorial.md#8-objective-amendments-and-standing-instructions).

## Work state and recovery

Inspect assignments with `coordination direction-list PROJECT [--open]` and
silence with `coordination stalls PROJECT [--threshold 30m]`. Eligible
acknowledged open assignments and reviews receive one durable informational
notice per silence epoch, with an attempted wake. The default interval is 30
minutes; the current local coordinator can set it with `coordination stall-config
PROJECT --threshold 1h` using integer `m` or `h` units from `1m` through `24h`.

Changed accepted reports and linked replies advance the clock. Workers record
state through `llm-bus report ID --status STATUS`; recording sends no message
and wakes nobody. Remote evidence uses local admission times and bridge
observations; monitor-only text and denied peers cannot refresh it. Silence
proves neither failure nor permission to act. Sleep or lost helpers can delay
notices until normal recovery, and failed or unsupported wakes leave notices
in the durable inbox. See the
[silence reference](../skills/llm-bus/references/coordination/coordinator.md#silence-inspection-and-notices).

Use `llm-bus guidance` and `llm-bus coordination show PROJECT` to recover current
state. SessionStart supplies full references and UserPromptSubmit rereads
authority after compaction. Expired or revoked grants suspend directions; old
context and tickets cannot restore authority. Hooks execute neither Beads nor
network calls. This describes the deterministic hook-payload contract, not proof
of live host compaction behavior.

Before upgrading or rolling back, read
[Mailbox compatibility and rollback](installation.md#mailbox-compatibility-and-rollback).
Schema 16 adds local silence state without changing relay protocols; there is
no supported in-place downgrade.
