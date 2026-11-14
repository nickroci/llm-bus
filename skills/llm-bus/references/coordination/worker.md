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
cancelled direction needs no report. Persist progress, then send an
ordinary linked result only for a blocker or an outcome the coordinator must
act on or learn, never for presence or routine progress:

```sh
llm-bus --format agent send codex:COORDINATOR_ID --reply-to LOCAL_ASSIGNMENT_ID --body 'TASK_ID result. Evidence: references. Remaining blockers: none.'
```

Work state is self-reported; the bus cannot see host activity.

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
