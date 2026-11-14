# Remote coordinator, local worker

Load with `core.md` when the project's coordinator address is remote (a worker),
or when the repository config lists a peer (a coordinator).

Gate records, auto-merge, owner decisions, standing instructions and objective
amendments are local-project data: they stay on the coordinator machine, and
remote addresses get no decision notices and cannot be a `--worker`. Remote gate
approvals exist (below). The roles the bus enforces on the receiver are in
`core.md`.

Workers still route questions, approvals and manual-action requests through the
current coordinator. Local decision records do not become remotely acceptable
through this route. For unsupported decisions or required local consent, the
coordinator asks the appropriate machine owner to act in the exact owning
session or machine. Preserve the blocker until the worker verifies its existing
requirements; never expand either endpoint's permissions to deliver a request.

Each owner independently configures relay repository admission, matching shared
repository ID, sender text permission, receiver `inbox` permission and running
bridges; see [../remote.md](../remote.md). Remote coordination also requires version 1
coordination transport support on both clients and relay. Sender permission has
no reciprocal effect. `--cross-folder` cannot bypass remote restrictions.

Receiver owner imports the coordinator's project UUID, generation UUID and exact
authenticated qualified address. Local paths belong to the receiver:

```sh
llm-bus --format agent coordination setup PROJECT --project-id PROJECT_UUID --generation GENERATION_UUID --coordinator MACHINE/REPO/codex:COORDINATOR_ID --objective 'Owner-approved objective' --beads-workspace /absolute/local/project --memory-ticket MEMORY_ID --reference /absolute/local/project/AGENTS.md
llm-bus --format agent join PROJECT
```

`setup` cannot overwrite an existing binding. Matching upgraded clients and relay
exchange structured availability automatically, installing the exact target only
for the current binding. Relay migration `0006_coordination_availability.sql` must
precede the matching Worker deployment. Unsupported relays leave availability
queued with `unsupported_availability`; never strip metadata to bypass it.
No deployment or trust change follows from join, and a join never establishes
trust in an unknown remote coordinator: remote coordinators also need the
receiving machine owner's approval of the project binding. Advanced explicit grants remain
available. For an older compatible direction transport, exchange the non-secret returned
recipient grant UUID out of band or as ordinary data. Coordinator stores it with
`coordination target PROJECT --recipient MACHINE/REPO/claude:WORKER_ID --grant-id GRANT_UUID`,
then sends with the same paired direction flags. Claimed IDs cannot authorize
the receiver. Local receive policy and current local grant must both allow
delivery; monitor/report channels never become agent assignment channels.

## Remote gate approvals

A remote coordinator's `coordination approve` reaches the worker only when every
side allows it; no side can grant another's permission:

- Coordinator owner: its own project delegation covers the gate, the base
  target and expiry, and the work branch is not a target. Checked at `approve`
  and again at each upload, so `amend` narrowing, `replace` or expiry holds the
  queued approval (`queued_local`; it is never resent as ordinary text).
- Receiver owner: imports the project with `setup ... --delegate GATE:BRANCH`
  (gate specs only), and sets `accept_delegated_gates` for that coordinator's
  machine in the local peer policy (omitted means deny). The worker's own `join`
  binding (optionally `--narrow`) must also cover the gate. The approval must
  expire within 24 hours of admission.
- Relay: version 1 `GET /v2/coordination/approvals/capabilities`; migration
  `0007_coordination_approvals.sql` precedes the Worker deployment. Otherwise
  the approval stays queued with `unsupported_approvals`.

After a receiver-local `replace`, the receiver owner runs
`coordination amend PROJECT --delegate GATE:BRANCH` (or `--no-delegation`;
narrowing applies at once), then each worker owner rejoins. On a
remote-coordinated project `amend` accepts only those two options; the
coordinator owner re-declares its own delegation with `amend` on its machine.
