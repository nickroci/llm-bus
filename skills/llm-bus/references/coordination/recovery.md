# Recovery, revocation and replacement

Load with `core.md` when the coordination state is not active, when the hook or
`llm-bus guidance` lists this file, and before an owner-requested `replace`,
`revoke` or `leave`.

## Inactive states

A suspended, `out_of_scope`, revoked or expired state grants no assignment
authority; diagnosis and recovery checks stay allowed, accepting directions
does not. Never auto-enroll or revive a grant. Only a fresh direct owner join
renews ended automatic consent.

Recovery reminders do not resume project work or revive consent. An active
worker with an unreachable coordinator or unverifiable authority preserves its
blocker and yields for user-directed recovery. A reachable coordinator may
present a request for the owner's direct action in the exact owning session or
machine; presenting it does not supply consent.

- `out_of_scope`: working from a folder outside the project keeps consent, but
  directions do not verify there (sends fail with a folder mismatch error, or
  `wrong_scope` under `--cross-folder` and at delivery) and verify again once
  the session is back in the project scope.
- To enroll elsewhere, first run the `leave` command the join error names, from
  that repository or folder.
- An expired or revoked grant suspends directions; ticket summaries and frozen
  context cannot restore it.

## Revocation and replacement

```sh
llm-bus --format agent coordination revoke PROJECT --recipient claude:WORKER_ID
llm-bus --format agent coordination revoke PROJECT
llm-bus --format agent coordination leave PROJECT
llm-bus --format agent coordination replace PROJECT --coordinator codex:NEW_ID --generation FRESH_UUID
```

Recipient revoke removes that enrollment; project revoke suspends the project.
`leave` removes only the current session's enrollment. Records/history remain.
Revoke, leave and replacement also clear the affected workers' ticket/branch
claims, so approvals for them stop verifying; a replaced generation starts
without a delegation and bindings, and the new coordinator delegates again with
`coordination amend` after direct owner approval. Objective amendments belong to
one coordinator generation: after `replace`, the effective objective falls back
to the objective stored at creation, and the new coordinator amends it again if
needed. Owner standing instructions are stored per project and survive
replacement.
Replacement requires a never-used UUIDv4 generation, invalidates local grants,
enrollments and targets, and enrolls the replacement only when it is the current
native main session. Otherwise that session must run `coordinate PROJECT`.
Issue fresh grants, rejoin workers and update coordinator target references.

For an offline receiver, stop/reconcile active work first. Each owner applies
replacement locally with the same new generation and correct qualified address.
Regrant/rejoin on each receiver and exchange fresh target references. Inspect
stale queue/history rather than blindly replaying assignments. No remote command
mutates another owner's grant. An offline until-revoked receiver follows its
last local policy until changed; global immediate revocation is not promised.
Already exposed text or a submitted wake cannot be recalled.

## Recovery

Both hosts fire `SessionStart` with source `compact` after compaction, which
restores full guidance when emitted (owner standing instructions are restored
through the digest pointer, `core.md`). If it was not emitted, run
`llm-bus --format agent guidance` for the same restore. `UserPromptSubmit`
rereads current enrollment every real prompt. Compact reminders
retain authority state and recovery command. Use `llm-bus --format agent guidance` and
`llm-bus --format agent coordination show PROJECT` to recover full current local state.

Before resuming, read the official Beads skill, project-scoped `bd prime`,
memory, assigned tickets and actual worktree/evidence. Normal revoke removes
enrollment, so its reminder disappears; absence of a
reminder grants no authority. Inspect `coordination show` when reconciling it.
The exact stale-wake no-op may omit recovery until the next real prompt.
Hooks neither execute Beads nor contact the network. No dedicated compaction
hook or live host compaction guarantee is implied.

Silence notices also recover through normal coordinator hook/guidance recovery
and `coordinate PROJECT`. These entry points reconcile overdue notices and
missing or expired timer claims. Machine sleep, process loss, missing hooks or
database unavailability can delay delivery; a lost helper has no guaranteed
wall-clock recovery until a normal entry point runs. The read-only
`coordination stalls PROJECT` command never repairs timers or creates notices.
If a host wake fails or is unsupported, resume the coordinator manually and
read its durable inbox. Do not resend a stored notice or treat it as permission
to retry work. Revoked or ended authority stops new detection; history remains.

### Recovery brief

Give the reading to one read-only native subagent (`core.md`, "Delegated
reads"). The brief it returns holds:

- the `bd prime` output summary;
- the memory ticket content;
- assigned tickets;
- worktree evidence;
- owner limits and authorization references, quoted verbatim;
- a `truncated` flag that is true when it could not read everything.

When the brief is truncated or lacks the owner limits, the main agent reads the
memory ticket itself before resuming.
