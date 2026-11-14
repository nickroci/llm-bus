# Worker duties

Load with `core.md` for an active worker session, and before a `join PROJECT`
run.

## Joining

The owner's `/llm-bus join PROJECT` invokes:

```sh
llm-bus --format agent join PROJECT
```

With no existing explicit grant, join creates session-bound consent, enrollment
and target automatically. Consent survives turns/compaction and ends on close,
24-hour inactivity, leave, revoke or generation replacement. Existing explicit
grants retain their selected lifetimes, and join reuses an explicit grant rather
than converting it to automatic consent. One native session enrolls in one
project. Join assigns no specialty.

When the project delegates gates, join also records the worker's binding:
the delegated set, or only the items passed as repeated `--narrow ITEM`
(a `GATE:BRANCH` or `auto-merge`, each already delegated by the project;
items not listed are dropped, so list `auto-merge` to keep it). The owner
decides the narrowing; a worker never self-selects wider gates. A plain
rejoin accepts the full current delegation.

## Handling a direction

Read `llm-bus --format agent inbox`. Check the structured `coordination` field has
`verified: true` and `reason: verified`, matching project, generation and operation. Read current
memory, assigned ticket and repository rules. Follow existing review gates;
`ack` the local message ID when you accept the direction, before reporting
its state; a report requires a prior ack. Before returning control, start
every accepted direction that is no longer blocked. When a direction's state
changes, record it; recording wakes nobody and sends no message:

```sh
llm-bus --format agent report LOCAL_ASSIGNMENT_ID --status received
llm-bus --format agent report LOCAL_ASSIGNMENT_ID --status proceeding --reason started
llm-bus --format agent report LOCAL_ASSIGNMENT_ID --status not_proceeding --reason dependency_blocked
llm-bus --format agent report LOCAL_ASSIGNMENT_ID --status completed
```

`report` accepts only the built-in status and reason codes (`received`;
`proceeding` with `started` or `resumed`; `not_proceeding` with
`needs_approval`, `missing_information`, `dependency_blocked`,
`out_of_scope` or `declined`; `completed`; `failed` with
`validation_failed`, `execution_failed`, `permission_denied` or
`unavailable`) and only for a direction you received and acknowledged. A
cancelled direction needs no report. Persist progress and send concise ordinary
linked messages for phase changes, blockers and outcomes the coordinator must
act on or learn. A phase-change message names the new phase and its outcome or
next dependency; it is not a periodic heartbeat:

```sh
llm-bus --format agent send codex:COORDINATOR_ID --reply-to LOCAL_ASSIGNMENT_ID --body 'TASK_ID phase: verification. Implementation complete; tests next. Evidence: references.'
```

Whenever you record `BLOCKED` or request a coordinator or owner decision in a
task artifact, record the appropriate fixed-code report and send one ordinary
linked blocker message in the same step, before yielding or moving to another
task. Include the task, exact worker identity and session banner, blocker,
needed decision or action, and evidence reference. Saving an artifact alone
does not communicate the blocker. For example:

```sh
llm-bus --format agent report LOCAL_ASSIGNMENT_ID --status not_proceeding --reason dependency_blocked
llm-bus --format agent send codex:COORDINATOR_ID --reply-to LOCAL_ASSIGNMENT_ID --body 'TASK_ID blocked. Worker: codex:WORKER_ID; session: PROJECT worker LABEL TICKET BRANCH FOLDER. Blocker: dependency unavailable. Need: coordinator resolve dependency. Evidence: references.'
```

If sending fails, preserve the blocker and delivery failure in existing task
memory. Retry only through the existing recovery rules; a saved request does
not count as delivered communication.

If you observe a native child's death or disconnection, immediately tell your
coordinator the affected task and recovery choice. Native children report only
to their parent; the main parent sends the bus message. This obligation applies
to observed failures and does not promise host-level failure detection.

Work state is self-reported; the bus cannot see host activity.
Only changed accepted reports and worker messages linked to an assignment or
review advance its silence clock. Repeating a report, saving a task artifact
or sending an unlinked message does not. Continue to send meaningful phase
changes and immediate blocker requests; do not send periodic heartbeats to
avoid notices. Native-child completion is not guaranteed to wake its parent.

## Requests needing the user

Send your current coordinator one concise blocker request for a question,
approval or manual action. Link the assignment with `--reply-to` when available.
Include your exact identity, session banner, affected ticket/branch, required
decision or action and why work is blocked. Record `missing_information`,
`needs_approval` or `dependency_blocked` as appropriate; the report alone wakes
nobody. Native subagents send requests only to their parent.

The coordinator obtains the user response and follows up. Check the applicable
authority contract and verify the required action before resuming; a relayed
answer or claimed completion is insufficient. If direct consent or a host step
is required, the coordinator directs the user to your exact session or machine.
Keep native permission prompts native. If the coordinator is unavailable or
authority cannot be verified, preserve the blocker and yield. Direct user
instructions may override the communication default within their stated scope.

## Progress notes and worktrees

Workers own progress notes on assigned tasks. Read existing notes before updates,
preserve others' work, and propose project-wide summary changes to coordinator.
Use the installed Beads version's supported atomic claims for task ownership.
Local bus locks cannot serialize remote database writes. Saved local notes do
not prove remote visibility; honor the owner's configured persistence policy.
Workers use their own worktrees.
