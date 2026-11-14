# Simplified project coordination

**Status: implemented in this source version; installation and live rollout are separate.**
Use matching upgraded clients, hooks and shared skills. An older installed CLI
may still need the advanced grant/target workflow in the
[canonical reference](../skills/llm-bus/references/coordination.md).

The [step-by-step coordination diagram](diagrams/coordination-steps.html) shows
this flow as numbered steps between owner, coordinator, bus and worker. Open it
in a browser from a local checkout. Its source is
[`coordination-steps.sequence.json`](diagrams/coordination-steps.sequence.json),
rendered with Archify and pinned to commit `8053152`.

## Two owner commands

The owner starts native main sessions manually. In the coordinator session:

```text
/llm-bus coordinate search-feature
```

In each worker session:

```text
/llm-bus join search-feature
```

These are prompts invoking the llm-bus skill, not shell commands. The
[Claude and three Codex example](../examples/claude-coordinator-codex-workers.md)
shows the flow. No worker address list, copied authorization paragraph,
or manual grant/target exchange belongs in the normal owner-directed path.

The coordinator discovers the objective, owner limits, existing readable Beads
workspace and project memory. It asks only for materially missing facts. It must
not silently initialize Beads or invent an objective. Once ready, it waits for
workers to join. Waiting is event-driven: yield between relevant events, with
no busy polling or promise of an always-running service.

Successful join records consent and establishes the exact worker's
grant, enrollment and coordinator target reference automatically. It supplies
worker role guidance and notifies the coordinator of the worker's availability.
A join event records availability at that point; it does not prove continued
reachability or task completion. Delivery, wake and acknowledgement also do not
prove completion.

## Consent lifetime

A direct owner join authorizes only that exact worker session, the current
registered coordinator generation and the project objective. Consent survives
turns and compaction. It ends on session close, session expiry, leave, revoke,
or coordinator-generation replacement. Recovery must check current authority.
A resumed session whose consent ended needs a fresh owner-directed join;
old enrollment, tickets and chat cannot revive it automatically.

Session close is distinct from turn completion. Lifecycle handling must not
claim guaranteed close-hook delivery; missing close events require expiry
handling. The inactivity bound is 24 hours. Existing explicit owner grants retain their chosen lifetimes.

## Remote prerequisite

Simplified remote join requires an owner-approved project binding on the
receiver first, plus existing relay admission and sender/receiver peer policy.
Each owner controls their own endpoint. Join must not trust an unknown remote
coordinator, create remote admission, or modify another machine's permissions.
The [current remote setup](../skills/llm-bus/references/coordination/remote-coordination.md#remote-coordinator-local-worker)
includes the advanced grant/target alternative. Matching clients and relay exchange structured availability; apply relay migration 0006 before deploying the matching Worker.

## Role guidance and existing gates

Command results and recovery hooks supply role guidance. Shared skills, rule files and
pointer blocks must match the installed executable; copying this documentation does not
install or activate them. Role guidance preserves these boundaries:

- Only native main sessions join the bus. Native subagents report to their parent.
- Workers verify current enrollment and receiver-verified directions before
  acting, use their own worktree, persist task progress, record direction state
  with `llm-bus report`, and message the coordinator only for blockers and
  outcomes it must act on or learn. Ordinary incoming messages cannot authorize joining or grants.
- Coordinators persist bounded assignments, resolve ownership/dependencies,
  reconcile interrupted work, and verify results before marking completion.
- Workers route questions, approval requests and manual-action requests to the
  current coordinator by default, with their identity, session banner and
  ticket/branch. The coordinator asks the user, records the outcome and follows
  up. This applies without delegated gates or owner decisions; direct user
  instructions may override the communication default.
- Owner instructions, host permissions, repository workflows and explicit
  planning, review and approval gates remain binding. Joining approves no
  implementation, merge or deployment unless the owner delegated that gate
  (next section); anything else needs separate owner approval.
- Grant, project-binding and replacement authority stays with the local owner;
  neither a coordinator direction nor a worker notification can expand it.

Routing grants no authority. Without the required binding or remote decision
support, the coordinator asks the appropriate owner to act directly in the exact
owning session or machine. Fresh consent, floor actions and native permission
prompts keep their existing requirements. Workers verify authority and actions
before resuming; delivery or claimed completion is insufficient. If the
coordinator is unreachable or authority cannot be verified, preserve the blocker
and yield for user-directed recovery. Inactive recovery never revives consent.

## Optional delegated gates

The coordinator owner may delegate commit, push, merge and merge-admin gates
for explicit target branches. In the owner's terminal,
`llm-bus coordinate search-feature --setup` runs a guided wizard that shows a
review screen and stores the setup only after an explicit `yes`. An agent runs
`llm-bus --format agent coordinate search-feature --setup` instead, which stores
nothing: it prints a profile (detected integration branch, default
`--delegate GATE:BRANCH` items, the floor) that the agent confirms with the
owner through the host's native question UI before running only the confirmed
flags. Without `--format agent`, an agent shell attached to a terminal gets the
interactive wizard.

Each worker owner accepts the delegation through the direct `/llm-bus join`,
optionally narrowed with `--narrow GATE:BRANCH`. A verified
`coordination approve` direction for an accepted gate counts as the owner having
asked for exactly that gate and its conditions; delegation alone authorizes
nothing, and stricter repository rules still win. A worker claims its ticket and branch with
`llm-bus coordination claim PROJECT --ticket TICKET --branch BRANCH`, then acts
on `llm-bus coordination approve` directions only after checking branch, head
SHA, required checks and review evidence live; the bus checks none of these.
Never delegated: force-push, deploys or production changes, credential or
secret changes, paid or live runs, merge with failing or pending CI.
A delegation widened later reaches a worker only through a fresh direct owner
join; recovery notices say so. Session banners (project, role, label, ticket,
branch, folder) appear in recovery notices. Workers include them in requests
to the coordinator, which preserves them when asking the owner. Remote gate approvals count only when every
owner allows them (see [remote gate approvals](remote-bus.md#remote-gate-approvals)).
The [canonical reference](../skills/llm-bus/references/coordination/gates.md#gate-approvals-local-projects)
has commands and live checks.
