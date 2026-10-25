# Communicate across machines

Remote messaging is opt-in. Each machine runs one bridge, which connects to a Cloudflare Worker over HTTPS. The Worker uses D1 for repository permissions and durable mailboxes, and Queues for delivery jobs. Local messaging continues to work without remote configuration or a running bridge.

Communication requires the same shared repository ID on both machines. A repository may have different filesystem paths on each machine. Git worktrees share their repository's mapping. Folder names and Git remote URLs never grant access.

Each person owns their machines' local TOML policy. Exact per-peer capabilities decide whether normal text may be sent and whether incoming text goes to a native inbox, a separate human monitor, or neither. Structured work reports are a separate fixed-vocabulary channel for existing messages. There is no shared topology file or central topology administrator: independently configured endpoints determine the routes. Relay authentication and repository admission grants do not authorize delivery into another owner's agents or changes to their policy.

This guide covers the current implementation. Existing installations should read [migration and rollback](#migration-and-rollback) before starting updated bridges.

## Deploy the relay

The relay operator needs Cloudflare credentials. People connecting their machines do not need Cloudflare accounts.

From `cloudflare/`, install dependencies and create resources:

```sh
bun install --frozen-lockfile
bunx wrangler d1 create llm-bus
bunx wrangler queues create llm-bus-delivery
bunx wrangler queues create llm-bus-dead-letter
```

Replace the placeholder `database_id` in `cloudflare/wrangler.jsonc` with the D1 ID returned by Cloudflare. Configure a custom domain in the same file, for example:

```json
"routes": [{"pattern": "bus.example.com", "custom_domain": true}]
```

The example domain must belong to the operator's Cloudflare account. `workers_dev` is disabled in the supplied configuration. Back up existing D1 data before upgrades. Apply all migrations before deploying the matching Worker:

```sh
bunx wrangler d1 migrations apply llm-bus --remote
bunx wrangler deploy
```

`0001_bus.sql` owns machine admission and messages. The additive `0002_reports.sql` migration adds work-report storage; `0003_reply_to.sql` adds nullable reply UUIDs and their index. These migrations do not configure peer trust or change machine grants. See [upgrade ordering](#migration-and-rollback) for existing relays.

One Worker handles the HTTP API, Queue consumer, and scheduled dispatch repair. Queue jobs contain message IDs; message bodies remain in D1. The supplied cron repairs missed Queue publication every minute. The dead-letter queue retains failed jobs for operator inspection; pending D1 dispatch rows also remain eligible for repair.

## Issue one static token per machine

Choose a unique machine ID such as `whitenoise`. Machine IDs, shared repository IDs, and native session IDs use letters, digits, underscores, or hyphens, with a maximum of 128 characters. Generate an independent token for every machine. Never reuse a token across people or machines.

From the repository root, this command creates a private token file and prints only its SHA-256 hash. Use an absolute destination outside repositories. The exclusive file creation prevents overwriting an existing token.

```sh
uv run python - /absolute/private/whitenoise.token <<'PY'
import hashlib
import os
import secrets
import sys
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
token = secrets.token_urlsafe(32)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="ascii") as stream:
    stream.write(token + "\n")
print(hashlib.sha256(token.encode("ascii")).hexdigest())
PY
```

Securely give the token file to the machine owner. Do not put its contents in a command argument, chat transcript, repository, Cloudflare configuration, or log. On POSIX machines the bridge requires a regular file owned by the current user with no group or other permissions; symlinks are rejected. On Windows, restrict the file's ACL to the machine owner; POSIX mode checks do not apply.

From `cloudflare/`, register the machine and its repository grant in D1, substituting the printed hash:

```sh
bunx wrangler d1 execute llm-bus --remote --command "INSERT INTO machines(id,token_hash) VALUES ('whitenoise','PRINTED_SHA256_HASH');"
bunx wrangler d1 execute llm-bus --remote --command "INSERT INTO repo_grants(machine_id,repo_id) VALUES ('whitenoise','xyz');"
```

Repeat with a different token for `herdr-dev-1`, granting it `xyz` too. Shared repository IDs are operator-assigned; choosing the same name in a local file cannot create a server grant. There is no public enrollment or permission-administration endpoint.

## Configure each machine

Update/install the Python CLI from this checkout using the [normal installation procedure](../README.md#install-and-verify). Create `~/.config/llm-bus/config.toml`. These complementary examples connect a personal PC named `whitenoise` to a VPS named `herdr-dev-1`. Names carry no special privileges. Provision both machine tokens and the `xyz` grants first, as above.

On the PC, permit outbound tasks and built-in reports back, while denying all normal incoming text:

```toml
policy_version = 2

[remote]
url = "https://bus.example.com"
machine_id = "whitenoise"
credential_file = "/absolute/private/whitenoise.token"

[[repositories]]
path = "/Users/alice/Projects/xyz"
repo_id = "xyz"

[[repositories.peers]]
machine_id = "herdr-dev-1"
send_messages = true
receive_messages = "deny"
send_reports = []
receive_reports = ["received", "proceeding", "not_proceeding", "completed", "failed"]
```

On the VPS, independently permit PC tasks into its agents and reports back to the PC. This example also accepts normal text from a separately admitted teammate VPS; the PC has no matching teammate permission:

```toml
policy_version = 2

[remote]
url = "https://bus.example.com"
machine_id = "herdr-dev-1"
credential_file = "/absolute/private/herdr-dev-1.token"

[[repositories]]
path = "/home/alice/xyz"
repo_id = "xyz"

[[repositories.peers]]
machine_id = "whitenoise"
send_messages = false
receive_messages = "inbox"
send_reports = ["received", "proceeding", "not_proceeding", "completed", "failed"]
receive_reports = []

[[repositories.peers]]
machine_id = "teammate-vps"
send_messages = false
receive_messages = "inbox"
send_reports = []
receive_reports = []
```

The teammate needs its own token, relay grant, and local `send_messages = true` permission for `herdr-dev-1` to send tasks. Its policy cannot override PC reception. A VPS forwarding teammate text to the PC is still a normal message from that VPS, which the PC denies. Putting arbitrary text in report fields is rejected. A valid but false report remains untrusted data.

Capabilities apply only to the exact `machine_id` under that repository:

| Key | Default | Effect |
| --- | --- | --- |
| `send_messages` | `false` | Allow normal text to this peer. Boolean only. |
| `receive_messages` | `"deny"` | `deny` stores no incoming copy; `monitor` stores separate human history; `inbox` admits text to native agents and the existing wake path. |
| `send_reports` | `[]` | Allow listed built-in statuses for imported messages from this peer. |
| `receive_reports` | `[]` | Accept listed built-in statuses for this machine's outgoing messages to this peer. |

Unlisted peers are denied. There are no wildcards, inferred reciprocal permissions, custom report labels, or configurable reason sets. Selecting a status permits its fixed reasons listed [below](#structured-work-reports). Use `receive_messages = "monitor"` in a chosen peer entry to opt into human-only storage; it grants no normal reply or report permission.

`policy_version = 2` is mandatory. Unknown keys, mixed legacy/new settings, duplicate peers, duplicate repository IDs or scope mappings, self-machine peers, and malformed identities are errors. Relative paths resolve from the configuration directory. Map a Git repository once, including all linked worktrees. Unmapped repositories cannot communicate remotely. Multiple `[[repositories]]` entries keep their peer decisions independent. `--cross-folder` cannot override any local capability, same-repository restriction, or server grant.

Set `LLM_BUS_CONFIG` to select another file. Once remote state exists, a SQLite mailbox is bound to its relay URL and machine ID; use another `LLM_BUS_DB` when changing that identity. Rotating the token does not change the binding. HTTPS is required except for loopback HTTP during local testing. Redirects are never followed with credentials.

## Run and use the bridge

Run one bridge per machine in a separate terminal or under your preferred process supervisor:

```sh
llm-bus bridge
```

The bridge sleeps between one and 30 seconds according to durable due work. It rereads configuration and credentials between passes, reuses its transport connection identity, publishes session activity, transfers messages, and synchronizes acknowledgements and work reports. Successful transport/report reads wait 30 seconds; peer-cache refreshes wait 60 seconds. Failed message operations retry after 5, 10, 20, 40, 80, 160, then 300 seconds, capped at 300 seconds; successful operations reset that backoff. Retry timing survives restart. A heartbeat does not extend agent activity: native session hooks and commands own that activity. Multiple bridge processes deduplicate local imports; only one claims a wake attempt, but one process per mailbox avoids unnecessary polling.

For manual synchronization:

```sh
llm-bus bridge --once
```

The command needs no Claude/Codex session environment. It prints `uploaded`, `imported`, `acknowledged`, and `errors` counts. `uploaded` counts successful outgoing transfer/status synchronizations, not only new uploads. `imported` counts new native inbox admissions still permitted at the pending check; it does not prove wake success. Replays and monitor rows do not increment it. `acknowledged` counts synchronized agent acknowledgements, not reports or monitor receipts. An incomplete one-shot pass exits nonzero; durable pending state remains for retry. One denied outgoing message does not prevent other outgoing messages or incoming delivery. Continuous mode retries failed operations on later passes. Local filesystem or malformed-configuration errors stop the process, requiring correction and restart.

`--once` processes currently due operations. To refresh transport and report reads immediately, including transport records suspended at the observation horizon, run `llm-bus bridge --once --refresh-status`. This option requires `--once`; it never resends already accepted messages. Other due writes can still run during this pass.

Compatible relays accept status and report reads in batches of up to 100 UUIDs. Old single-item routes remain supported. Only HTTP 404, 405, or 501 from a batch route triggers single-item fallback; the bridge retries batch capability after five minutes. Authentication, rate-limit, server, and malformed-response errors retain state and use retry scheduling.

Within an agent session:

```sh
llm-bus list
llm-bus send herdr-dev-1/xyz/claude:SESSION_ID --body "Migration is ready for review"
llm-bus remote-status
llm-bus inbox
llm-bus ack 123
```

To reply, use `llm-bus send MACHINE/REPO/NATIVE_ADDRESS --reply-to 123 --body "Reply text"`, where `123` is the parent's local ID on this machine. The parent must be retained remote traffic for the same participants, repository, and saved relay connection. Native-only parents cannot become remote links. The wire carries the original parent UUID; each receiver resolves its own local integer ID. Replies never bypass either endpoint's current policy. `history --conversation 123 --peer ADDRESS` combines the reply tree and an exact sender/recipient filter with the selected history scope.

The bridge checks authenticated `GET /v1/capabilities` before uploading linked messages. Missing reply support or HTTP 404, 405, or 501 keeps the message `queued_local` with `unsupported_reply_link` and capped retry delays up to five minutes. Malformed capability fields and other HTTP errors fail closed. Roots continue using the old protocol. A child arriving before its retained parent is not receipted; later scans retry from sequence zero and can resolve it after the parent arrives. Other valid deliveries in that scan continue.

Reply-parent resolution uses retained native imports or outgoing messages on the same saved connection. A monitor-only parent cannot establish a native reply link and is never promoted by replay. A child with no resolvable parent remains unreceipted at the relay until a valid parent becomes available or the relay delivery window expires; monitor-only ancestry alone cannot resolve it.

`list` combines native peers with cached remote peers whose exact local peer policy permits `send_messages`, for the current repository. `list --all` extends that selection to all mapped repositories; the cache is not proof of connectivity or current server access. The server rechecks authorization before any remote delivery. Absolute checkout paths and local host socket endpoints are never sent to the relay.

A remote address includes machine ID, repo ID, and native agent address. Moving a native session to another repository changes its remote address. Incoming text requires an existing native recipient registration in the original repository mapping, plus current local peer reception permission. Monitor delivery uses that same registration; it creates no synthetic monitoring session. Pending messages for moved sessions remain in the relay until the session returns to that repository or they expire.

Remote `send` commits a message and UUID outbox record locally, then returns `queued_local`. It does not wait for networking. The bridge uploads later. The original repository context stays attached to the outbox entry even if the sender session ends or moves before upload. Sending to a known ended session still stores the message remotely; a wake may fail and the recipient can read it on a later turn. An unknown remote address remains queued until registration permits delivery.

`remote-status` shows the latest 100 outgoing messages with local integer IDs, transport UUIDs, recipients, and the latest synchronized state:

| State | Meaning |
| --- | --- |
| `queued_local` | Durable on the sender machine; relay acceptance is pending. |
| `accepted_remote` | Durable in the relay mailbox; destination receipt is pending. |
| `delivered_local` | Destination committed either its native inbox copy or separate monitor copy. |
| `acknowledged` | Destination agent acknowledged its native local message and the bridge synchronized it. Human monitor reads do not acknowledge. |
| `expired` | An unreceived message reached the relay's 30-day delivery window. |

Outgoing records retain `id`, `remote_id`, `recipient`, `status`, and `work_report`, and add the same body-free projection as `status ID`. `synchronization` separates transport timing from report timing; `upload_synchronization` and `report_synchronization` retain their operation-specific metadata. Cached peers include separate synchronization freshness. Missing timestamps are null and stale; a cached value is never proof of current relay state.

For one retained message, run `llm-bus status LOCAL_MESSAGE_ID` (replace the placeholder with a local integer from `send`, `inbox`, or `history`). This command needs no native session, current configuration, token, bridge, or relay access. It remains readable after blocking a peer or removing configuration and does not create or migrate a mailbox. `remote-status` still requires current remote configuration and shows only the latest 100 outgoing messages. Neither command polls the relay.

The single-message result contains `id`, `remote_id`, `sender`, `recipient`, `reply_to`, `transport`, `wake`, `acknowledged_at`, `work_report`, and `synchronization`. `transport` contains `status`, `source`, and `observed_at`: native messages use `stored_local` with source `local`; imported messages show `delivered_local` as local durable admission. An outgoing relay snapshot does not reveal the destination machine's wake result. Unknown observation time stays null.

`wake` contains this message's recorded `status`, `via`, safe `reason`, and `checked_at`, plus its explicit `coverage_status` and the recipient coordinator's `retry_at` while the message is pending. Another message's coverage never supplies this message's wake outcome. A scheduled retry does not guarantee a running process will attempt it. Legacy adapter diagnostics are projected as fixed categories plus known adapter names; newly stored reasons use these safe categories. Original history is retained without rewriting.

`synchronization` contains `last_attempt_at`, `last_success_at`, `next_attempt_at`, `error_code`, `polling_suspended_at`, and `stale`. It is stale without a successful observation, when that observation is at least 30 seconds old, or after an error. When operations have retained metadata, `operations` lists each fixed kind separately, including upload, receipt, acknowledgement, report read, or report submission. Report submissions include `expected_revision`; each attempted revision retains its own success, error, and retry timing. The summary describes transport synchronization, so inspect submission operations even when a receipt succeeded. A successful transport read cannot establish report freshness. `work_report` is only the strict synchronized sender-side snapshot; queued recipient submissions are not displayed as accepted reports. These fields are diagnostic data, never instructions or approval.

Transport polling suspends 30 days after the first valid relay acceptance observed locally. Suspension preserves the last known transport state, including delivery; it does not assign `expired`. Queued uploads have no local expiry or fabricated acceptance date. Report polling remains governed separately by the relay's saved report deadline and terminal report snapshots.

A Queue acknowledgement, a destination receipt, a host wake, and an agent acknowledgement are separate events. Host wake is best effort and uses the existing local adapter. Reads never consume messages. Native inbox imports share the local recipient coordinator: one lease gathers pending IDs, rechecks current policy, and requests one fixed notice without body, monitor, or report content. Successful requested coverage persists across restarts and prevents automatic repeat wakes for those same IDs. Later arrivals remain distinct durable messages and can cause a follow-up request; each call permits at most two native attempts. A host losing an already queued notice does not trigger an automatic resend; pending messages remain visible at the recipient's next hook or inbox read.

Every bridge pass scans native pending recipients, including already receipted imports, without requiring relay replay. A crashed leader's 30-second lease expires; failed or unknown wakes retry after 5, 10, 20, 40, 80, 160, then 300 seconds, capped at 300. Unsupported wakes wait for a changed registered capability or later send instead of repeated automatic attempts. The next recipient hook always reports permitted pending work. Without a running bridge, recovery after a crash requires a later send or host turn; no background service is added. `coalesced` reports a shared lease, successful request, or scheduled retry, never delivery or handling. See [local wake behavior](../README.md#wake-behavior).

The bridge retries uploads, admissions, receipts, agent acknowledgements, and queued report submissions. UUIDs prevent duplicate mailbox rows. A repeated UUID with different content is rejected. Mailbox cursors restart at zero each synchronization pass, so out-of-order Queue delivery cannot skip earlier messages. Queues may reorder work; no total conversation ordering or exactly-once agent handling is promised.

## Structured work reports

The original native recipient can explicitly report on an imported inbox message. Use its local integer message ID from `inbox`; neither transport UUIDs nor monitor cursor IDs are report arguments. The command requires the current native recipient identity, matching repository registration, and current `send_reports` permission:

```sh
llm-bus report 123 --status received
llm-bus report 123 --status proceeding --reason started
llm-bus report 123 --status not_proceeding --reason needs_approval
llm-bus report 123 --status completed
llm-bus report 123 --status failed --reason validation_failed
```

The last two commands illustrate alternative terminal outcomes, not a sequence to run together. `received` and `completed` require no `--reason`. Every other status requires exactly one of its fixed reasons:

| Status | Allowed reason values |
| --- | --- |
| `received` | None (`null` on the wire) |
| `proceeding` | `started`, `resumed` |
| `not_proceeding` | `needs_approval`, `missing_information`, `dependency_blocked`, `out_of_scope`, `declined` |
| `completed` | None (`null` on the wire) |
| `failed` | `validation_failed`, `execution_failed`, `permission_denied`, `unavailable` |

Success prints a durable queued intent containing `message_id` (the original UUID), `expected_revision`, `status`, and `reason`. It does not mean relay acceptance. The bridge submits it later; offline state and lost responses preserve the exact intent for retry. The relay derives identities from the original message and permits only its original recipient machine, after durable receipt and with current grants. The sender independently checks its current `receive_reports` policy and strict fields before storing a snapshot.

`remote-status` keeps transport state separate from `work_report`. For example, an acknowledged task can later show:

```json
{
  "id": 42,
  "remote_id": "c5664d2a-3ab9-4bb2-948e-933f3d059a8f",
  "recipient": "herdr-dev-1/xyz/codex:SESSION_ID",
  "status": "acknowledged",
  "work_report": {
    "message_id": "c5664d2a-3ab9-4bb2-948e-933f3d059a8f",
    "revision": 2,
    "status": "completed",
    "reason": null
  }
}
```

The command returns a JSON array of up to 100 such outgoing rows. `work_report: null` means no snapshot has been synchronized. A snapshot with `revision: 0`, `status: null`, and `reason: null` means the relay had no explicit report yet. Neither condition proves a report is available; check relay compatibility, policy, and retention. Reports may continue after transport acknowledgement and stop polling after a terminal report or the relay-owned report deadline.

Successful message acceptance and transport-status responses include `report_expires_at`, an explicit UTC deadline 30 days after relay acceptance. Identical upload retries preserve it. Offline enqueue time does not start that clock. Invalid supplied deadlines are protocol errors; an existing deadline cannot silently change. A legacy relay omitting this metadata still supports ordinary transport, but reports cannot be polled without a valid deadline. Already acknowledged originals without a stored deadline are not automatically revisited for one.

Reports use ordered revisions starting from zero; successful changes increment atomically. Identical retries of the last accepted intent are idempotent. Stale/concurrent changes conflict, and delayed older snapshots cannot overwrite newer ones. Intermediate states may be skipped. `not_proceeding` may later become `proceeding`; `received` cannot replace a later lifecycle state. `completed` and `failed` are terminal and permit only identical retries. Polling returns the latest snapshot, not a report event log.

`received` is a recipient's explicit claim, distinct from automatic transport `delivered_local`. `ack` never manufactures a work report. Reports accept no arbitrary explanations, body, attachments, labels, or extra fields. They never create native inbox entries, pending counts, wakes, commands, automatic retries of work, or policy changes. `needs_approval` neither requests nor grants host permission. All statuses, including `completed`, remain unverified recipient claims. Fixed choices and timing still convey limited information.

## Human monitoring

Monitoring stores messages addressed to existing native sessions separately from their inboxes. It does not subscribe to every conversation in a repository. Read it explicitly from a normal terminal; no Claude/Codex session environment is required:

```sh
llm-bus monitor
llm-bus monitor --repository /Users/alice/Projects/xyz --limit 20
llm-bus monitor --repository /Users/alice/Projects/xyz --before 27 --limit 20
llm-bus monitor --all
```

Default scope is the current repository, including linked worktrees, or the current folder outside Git. `--repository PATH` selects that canonical scope; `--all` explicitly selects all retained scopes. Those selectors are mutually exclusive. Rows are newest first. The default limit is 100, with an allowed range of 1–100. `--before` takes a positive monitor integer ID and reads older rows. Empty output means no retained rows in that selection.

Each row prints its monitor ID, timestamp, UUID, qualified addresses, repository, original scope, relay/machine connection binding, and body. Every displayed string uses JSON string escaping in ordinary terminal text: newlines, ESC/OSC/BEL/CR, C1 controls, and Unicode formatting controls stay escaped. Output is human terminal text, not a JSON integration or native inbox feed.

Monitor IDs are separate pagination cursors. They are not native message IDs for `ack` or `report`, even if integers coincide. Reading does not acknowledge transport, create `received`, or submit a report. The bridge sends a durable relay receipt only after monitor storage commits, so the sender can see `delivered_local` independently of human viewing. No native messages, proxy agents, pending counts, hooks, wakes, or agent acknowledgements result from monitoring.

UUID deduplication spans inbox and monitor admissions. The first stored class wins: changing monitor to inbox never promotes old monitored text on replay. Changed content for a stored UUID is rejected. Inbox history also retains its original class if policy later changes to monitor; current policy can withhold its pending exposure.

Monitor history has no automatic local expiry. It remains explicitly readable after blocking a peer, removing a repository or configuration, or losing current relay access. Reads use the stored scope and connection snapshot; they need no current token or remote configuration. Native `history` and its viewer remain separate. Back up and protect the mailbox like other untrusted message history. This is bus delivery separation, not file isolation from another process sharing the account.

## Policy changes

Current local policy is checked before enqueue/upload, incoming admission, pending native exposure, report exchange, and wake dispatch. Reducing send permission holds queued text instead of uploading it. Reducing reception before import prevents local storage. Blocking a peer also hides already imported pending text from `inbox` and hook pending counts and prevents future wake dispatch. Stored native and monitor history is retained for explicit reading. Missing or invalid remote configuration denies these remote paths while local-only messaging remains available.

A permissive sender can still upload text to the relay if server admission permits, including through raw API calls. Local `deny` means no local admission or receipt, not rejection of relay storage. Pending relay text can be admitted if the owner later permits it, until expiry. Reinstating permission can also expose retained native pending messages. Local policy cannot recall text already read or wakes already submitted, nor atomically cancel a network operation already in flight. Do not interpret transport storage or a valid work report as approval.

## Revoke or rotate access

From `cloudflare/`, revoke a machine without affecting other machines:

```sh
bunx wrangler d1 execute llm-bus --remote --command "UPDATE machines SET revoked_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id='whitenoise';"
```

Remove access to one repository:

```sh
bunx wrangler d1 execute llm-bus --remote --command "DELETE FROM repo_grants WHERE machine_id='whitenoise' AND repo_id='xyz';"
```

Every request checks current server permissions. Revocation or grant removal also withholds pending downloads from that machine or sender. Previously downloaded copies cannot be retracted. Positive authorization is not cached in the Worker.

To rotate a token, generate a new private file, update `machines.token_hash` to its hash, and update `credential_file` on the owner machine. Tokens are static, without automatic expiration, until revoked or rotated. Keep the original private file until the owner has the replacement; update the server hash when ready to cut over. The old token immediately stops authenticating after that update.

The relay authenticates machines, not individual processes or actual checkouts. Processes sharing an operating-system user are mutually trusted. A holder of a machine token can impersonate that machine's sessions within its server grants. The relay operator can read message plaintext. Message bodies remain untrusted coordination input, never user permission or approval.

## Migration and rollback

For an existing installation, stop every remote bridge before changing clients or policy. Back up the local SQLite mailboxes, existing configuration, and D1 using Cloudflare's backup facilities. Keep tokens private and outside repositories.

1. The relay operator applies additive `0002_reports.sql` and `0003_reply_to.sql` migrations after `0001_bus.sql`, then deploys the matching Worker, using the [relay commands](#deploy-the-relay). Keep the existing machine credentials, grants, messages, and session data. Upgrading the relay does not select local peers or activate a bridge.
2. On each machine, close the agents and update the CLI from the checkout root with `uv tool install --force .`, then follow the [hook installation procedure](../README.md#install-and-verify). Preserve `[remote]`, local repository paths, and shared IDs. Add top-level `policy_version = 2`, remove repository `send_to` and `receive_from`, and choose explicit `[[repositories.peers]]` capabilities. Old repository-wide permissions cannot be translated into an allow-all peer list. Review both endpoints independently against the [examples](#configure-each-machine).
3. Verify `llm-bus status --help`, `llm-bus send --help`, `llm-bus history --help`, `llm-bus bridge --help`, `llm-bus guidance --help`, `llm-bus report --help`, and `llm-bus monitor --help` on the updated client. With the reviewed policy in place, use `llm-bus bridge --once` on each machine, inspect errors and `remote-status`, then start each supervised bridge. This pass performs real transfers; it is not a dry run. A nonzero exit leaves durable state for correction and retry.

Old or mixed configuration fails closed; there is no legacy permissive fallback. Local-only commands still work. New SQLite tables initialize additively on use. Deploying a new Worker before its required migration can break report requests. Updating the client before a compatible relay preserves ordinary transport when expiry metadata is omitted, but work-report polling remains unavailable until a compatible response supplies a valid deadline. An earlier acknowledged message without a saved deadline is not automatically backfilled. Do not fabricate an acceptance date from the local enqueue timestamp or resend an original to recover reports.

Synchronization scheduling uses additive local schema version 2 tables; wake leases, explicit coverage, and guidance versions extend the local schema to version 3. No additional D1 migration is needed for batch reads or local wake coordination. Existing mailboxes migrate transactionally and retain history. Legacy `wake_claimed` values are retained as historical data and do not establish coverage; pending legacy imports start uncovered. Clients that enforce older local schema versions refuse the newer mailbox, so use a compatible client or the pre-upgrade backup when rolling back. Keep the upgraded database intact.

Local schema version 4 adds the nullable parent ID and reply index transactionally; existing rows remain roots. Relay reply support requires `0003_reply_to.sql` before the matching Worker. These code changes do not apply D1 migrations or deploy the Worker automatically.

Single-message `status` adds no schema migration and reads missing legacy metadata as null. Reading status does not upgrade an old mailbox; an ordinary current-client operation performs the transactional additive upgrade. This does not make old clients safe for a newer mailbox: retain the pre-upgrade backup and stop bridges before rollback. Mixed relays retain single-item reads and unlinked-message support; linked messages require explicit reply capability. Do not strip reply links or resend to bypass an incompatible relay.

D1 holds unreceived messages for a 30-day delivery window independently of Queue retention. The report window also ends 30 days after relay acceptance, independently of delivery/acknowledgement. Expired messages are excluded from delivery and report `expired`; relay rows remain stored for history/status rather than being deleted automatically. There is no automatic local purge of native messages, monitoring, or stored report snapshots. The operator owns relay archival and backup policy.

Stopping the bridge suspends remote transfers without affecting local messaging. Remove or move the configuration file to hide remote peers and disable remote sends; retain databases. Pending outbox entries can resume when the same connection and permissions return. Previously stored history is not erased by blocking.

Before downgrading to a client without peer policy, stop all remote bridges and keep them stopped. Older clients cannot preserve inbox revocation or monitor/report isolation. Review existing pending native records before running an older agent-facing client; stopping the bridge alone does not add the new pending checks to old code. Do not restore the old permissive remote settings and resume old bridges expecting these restrictions. Retain additive database tables and backups; no destructive schema rollback is required for local-only recovery.

## Development verification

From the repository root:

```sh
uv run poe check
uv build
```

From `cloudflare/`:

```sh
bun install --frozen-lockfile
bun run check
bun run test
bun run build
```

Worker tests use isolated Miniflare D1 and Queues. Real HTTP/Python integration covers offline enqueue/restart, delivery and acknowledgement, reports after acknowledgement, and a PC/VPS/outsider topology. It proves raw-relay and forwarded normal text can be accepted remotely while denied locally, and monitor replay after a receipt interruption never becomes a native inbox entry. Python tests additionally cover policy changes during upload/admission/wake/report exchange, retained history, malformed legacy policy, report revisions, and terminal rendering. No cloud credentials or real agent wakes are used. `bun run build` performs a dry-run Worker bundle; it does not deploy.
