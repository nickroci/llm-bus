# Remote messaging, reports, and monitoring

Use full address exactly as `llm-bus --format agent list` shows:

```sh
llm-bus --format agent send MACHINE/REPO/claude:SESSION_ID --body 'The shared API schema changed on branch feature-api. Update your client before resuming integration.'
llm-bus --format agent remote-status
```

Remote messaging needs matching shared repository ID, relay admission
grants, complementary local sender/receiver capabilities, and running bridges
on both machines. Local bridge alone cannot establish connectivity. `list`
uses cache; listed remote address not prove current access or liveness.

Remote labels also accept `MACHINE/REPO/NAME`, for example
`llm-bus --format agent send build-server/project/Builder --body 'Inspect the API.'`.
They resolve from the policy-eligible cached roster. Bare labels never search
remote machines. Duplicate remote names are refused as ambiguous. A reused name
selects its current exact owner and transfers no grants, enrollment or replies.
Native addresses remain usable without label discovery. Labels require upgraded
bridges and relay migration `0005_session_labels.sql`; old peers remain visible
by native address. Ordered publication prevents stale snapshots or legacy
updates from restoring released revisioned labels. Discovery is not liveness.

## Respect each owner's policy

Each machine owner controls exact per-repository, per-peer capabilities.
Outbound messages, incoming text, outgoing reports, incoming reports are
separate permissions. Outbound permission grants no reverse permission;
repository membership and relay admission imply no local inbox permission.
New or omitted peer capabilities default deny.

Optional project directions also require receiver-local setup, exact grants and
native enrollment. For explicit activation or valid enrollment recovery, follow
the coordination dispatch in `SKILL.md`. Each owner controls their own grants;
sender permission has no reciprocal effect. Monitor/report channels never
carry assignment authority.

Incoming normal text routed as `inbox`, `monitor`, or `deny`. Route
denied → report restriction. No bypass via shorter local address,
other peer, forwarding, report text, `--cross-folder`, or policy edits. No
shared topology files or remotely editable local permissions.

Blocking keeps history, stops pending agent exposure and future
wakes. Cannot recall already exposed content or submitted wake.

## Interpret transport state

Remote `send` commits locally, returns `queued_local`; networking happens
later. `remote-status` shows up to 100 outgoing messages and latest
synchronized states, not live connectivity guarantee.

For one retained native message, use `llm-bus --format agent status LOCAL_MESSAGE_ID`, replacing
the placeholder with an integer from send/inbox/history. It reads retained data
without current configuration, identity, credentials, registration, migration,
network, or wake, even after blocking. Relay UUIDs and monitor IDs are separate
namespaces. Imported status proves local durable admission, not a fresh relay read.
`remote-status` still requires configuration and retains its existing keys.

Inspect transport, wake/coverage, acknowledgement, strict synchronized work report,
and synchronization separately. An outgoing row cannot reveal destination wake
state. Null timestamps mean unknown; no successful observation, an observation at
least 30 seconds old, or an error marks synchronization stale. Multiple operation
records keep report and transport timing independent. Queued report intents never
appear as accepted snapshots. Safe error codes omit raw stderr and exception text.

| State | Meaning |
| --- | --- |
| `queued_local` | Durable on sender; relay acceptance pending. |
| `accepted_remote` | Durable in relay; destination receipt pending. |
| `delivered_local` | Destination stored inbox copy or separate monitor copy. |
| `acknowledged` | Destination agent acknowledged; bridge synced acknowledgement. |
| `expired` | Unreceived message exceeded relay delivery window. |

`delivered_local` not mean agent saw text. Monitor reads not
acknowledge messages. No state proves task completed.
Delayed acknowledgement or stale status no reason to resend. Bridge
already retries transport; duplicate user sends create new work requests.

When explicitly diagnosing sync, `llm-bus bridge --once` does one
pass, reports counts/errors. Can upload, import, wake sessions; treat
as operation, not read-only status check. No extra persistent
bridge just because remote reply slow.

## Send a typed work report

Only original native recipient can report on imported inbox message,
using local integer ID from `inbox`. Transport UUIDs and monitor IDs
invalid report arguments. Current report policy and repository binding must
permit operation.

Example, when work actually begins:

```sh
llm-bus --format agent report 123 --status proceeding --reason started
```

Pick status matching observed work:

| Status | Allowed `--reason` |
| --- | --- |
| `received` | Omit. |
| `proceeding` | `started`, `resumed` |
| `not_proceeding` | `needs_approval`, `missing_information`, `dependency_blocked`, `out_of_scope`, `declined` |
| `completed` | Omit. |
| `failed` | `validation_failed`, `execution_failed`, `permission_denied`, `unavailable` |

Reports queue durable intents for later bridge sync. `completed`
and `failed` terminal; pick only when actual outcome supports.
Intermediate states skippable. `not_proceeding` may later become
`proceeding`; `received` cannot replace later lifecycle state. Identical
retries idempotent; conflicting or stale updates fail.

Reports accept only built-in status/reason values, no arbitrary text or
extra fields. Not alternative message channel. `ack` does not
create report. At sender, `remote-status` exposes `work_report` separate
from transport status. Null report = no synced snapshot, not proof
work not started. Reports stay unverified recipient claims.

`needs_approval` neither requests nor grants host permission. Reports never
authorize execution, work retries, or policy changes; must not enter
automatic agent prompts or wake notices.

## Keep human monitoring separate

`llm-bus monitor` reads separate retained remote text for explicit human review.
Use only for that purpose. No polling as alternative agent
inbox, no forwarding text into automatic work, no monitor text as authority.
Monitor output never creates pending messages, agent wakes, acknowledgements,
or work reports. Use `monitor --help` for read-only filter options.

## Scheduling and compatibility

Successful transport/report reads wait 30 seconds; peers refresh every 60 seconds.
Failed message operations retry after 5, 10, 20, 40, 80, 160, then 300 seconds.
Backoff persists across restarts. Batch reads carry at most 100 UUIDs; only HTTP
404/405/501 trigger single-item fallback, with batch capability retried after five
minutes. Other errors fail closed and preserve durable state.

Transport polling suspends 30 days after first locally observed relay acceptance,
preserving last-known delivery. This is separate from relay expiry of unreceived
messages and report deadlines. Explicit `llm-bus bridge --once --refresh-status`
refreshes suspended observations; it performs real transfers and may wake agents.
No resending to recover freshness. Report reads remain separately permissioned.

Local schema upgrades are additive and transactional; status adds no migration.
Older schema-versioned clients reject newer mailboxes. Stop bridges and retain
pre-upgrade backups before rollback. Relay reply support needs additive migration
`0003_reply_to.sql` and explicit capability; old relays still support root messages.
Native imports/outbox bindings resolve reply parents; monitor-only ancestry never
becomes a native parent. Unresolved children remain unreceipted until a valid parent
is available or relay delivery expiry. Follow the remote guide for operator upgrades.

Current mailboxes use schema 18. Local silence observation, unattended run history,
and decision-to-run bindings need no additional relay migration. Directions
require relay migration `0004_coordination.sql`, with
separate version 1 `/v2/coordination` capability/message/inbox routes. Legacy
inboxes omit directions. Unknown metadata fails closed; an unsupported relay
leaves directions queued with `unsupported_coordination`. Upgrade the client or
relay; never resend stripped direction flags as ordinary authority. Offline
receivers retain their last local policy; revoke/replace on each machine and
reconcile active work before issuing fresh grants and targets.
Automatic remote join availability additionally requires migration
`0006_coordination_availability.sql` and matching client/relay capability.
Remote gate approvals need relay migration `0007_coordination_approvals.sql`
before the Worker, the `/v2/coordination/approvals/capabilities` probe (an old
relay leaves them queued with `unsupported_approvals`), the coordinator owner's
own delegation, and on the receiver the owner's project delegation, the worker's
binding and the peer key `accept_delegated_gates` (default deny); see
[coordination/remote-coordination.md](coordination/remote-coordination.md#remote-gate-approvals). Held approvals stay in
the outbox. The roles the receiver enforces are in
[coordination/core.md](coordination/core.md).
Receiver-owner project setup and peer policy remain prerequisites; availability
cannot create trust or authorization. Unsupported relays leave it queued with
`unsupported_coordination`. Install/deploy separately; repository changes do not
perform either operation.
