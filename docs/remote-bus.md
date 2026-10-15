# Communicate across machines

Remote messaging is opt-in. Each machine runs one bridge, which connects to a Cloudflare Worker over HTTPS. The Worker uses D1 for repository permissions and durable mailboxes, and Queues for delivery jobs. Local messaging continues to work without remote configuration or a running bridge.

The first version permits communication only between sessions mapped to the same shared repository ID. A repository may have different filesystem paths on each machine. Git worktrees share their repository's mapping. Folder names and Git remote URLs never grant access.

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

The example domain must belong to the operator's Cloudflare account. `workers_dev` is disabled in the supplied configuration. Apply the schema and deploy:

```sh
bunx wrangler d1 migrations apply llm-bus --remote
bunx wrangler deploy
```

One Worker handles the HTTP API, Queue consumer, and scheduled dispatch repair. Queue jobs contain message IDs; message bodies remain in D1. The supplied cron repairs missed Queue publication every minute. The dead-letter queue retains failed jobs for operator inspection; pending D1 dispatch rows also remain eligible for repair.

## Issue one static token per machine

Choose a unique machine ID such as `alice-mac`. Machine IDs, shared repository IDs, and native session IDs use letters, digits, underscores, or hyphens, with a maximum of 128 characters. Generate an independent token for every machine. Never reuse a token across people or machines.

From the repository root, this command creates a private token file and prints only its SHA-256 hash. Use an absolute destination outside repositories. The exclusive file creation prevents overwriting an existing token.

```sh
uv run python - /absolute/private/alice-mac.token <<'PY'
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

Register the machine and its repository grant in D1, substituting the printed hash:

```sh
bunx wrangler d1 execute llm-bus --remote --command "INSERT INTO machines(id,token_hash) VALUES ('alice-mac','PRINTED_SHA256_HASH');"
bunx wrangler d1 execute llm-bus --remote --command "INSERT INTO repo_grants(machine_id,repo_id) VALUES ('alice-mac','xyz');"
```

Repeat with a different token for `bob-linux`, granting it `xyz` too. Shared repository IDs are operator-assigned; choosing the same name in a local file cannot create a server grant. There is no public enrollment or permission-administration endpoint.

## Configure each machine

Update/install the Python CLI from this checkout using the normal installation procedure. Create `~/.config/llm-bus/config.toml`:

```toml
[remote]
url = "https://bus.example.com"
machine_id = "alice-mac"
credential_file = "/absolute/private/alice-mac.token"

[[repositories]]
path = "/Users/alice/Projects/xyz"
repo_id = "xyz"
send_to = ["xyz"]
receive_from = ["xyz"]
```

On Bob's machine, use `bob-linux`, its token file, and its own checkout path. Relative paths resolve from the configuration directory. Map a Git repository once, including all its linked worktrees. Duplicate repository IDs or Git common-directory mappings are rejected. Unmapped repositories cannot communicate remotely.

`send_to` and `receive_from` default to the mapped repo ID when omitted. Set either to `[]` to disable that direction. Other repo IDs are rejected in v1. `--cross-folder` cannot override these rules or server grants. Multiple `[[repositories]]` entries permit a machine to participate in several independent repositories.

Set `LLM_BUS_CONFIG` to select another configuration file. Once remote state exists, a SQLite mailbox is bound to its relay URL and machine ID; use another `LLM_BUS_DB` when changing that identity. Rotating the token does not change the binding. HTTPS is required except for loopback HTTP during local testing. Redirects are never followed with credentials.

## Run and use the bridge

Run one bridge per machine in a separate terminal or under your preferred process supervisor:

```sh
llm-bus bridge
```

The bridge polls every five seconds. It rereads configuration and credentials between passes, publishes session activity, updates its remote peer cache, transfers messages, and synchronizes acknowledgements. A heartbeat does not extend agent activity: native session hooks and commands own that activity. Multiple bridge processes deduplicate local imports; only one claims a wake attempt, but one process per mailbox avoids unnecessary polling.

For manual synchronization:

```sh
llm-bus bridge --once
```

The command needs no Claude/Codex session environment. It prints counts of uploads, new local imports, synchronized acknowledgements, and failed operations. An incomplete one-shot pass exits nonzero; durable pending state remains for retry. One denied outgoing message does not prevent other outgoing messages or incoming delivery. Continuous mode retries failed operations on later passes. Local filesystem or malformed-configuration errors stop the process, requiring correction and restart.

Within an agent session:

```sh
llm-bus list
llm-bus send bob-linux/xyz/claude:SESSION_ID --body "Migration is ready for review"
llm-bus remote-status
llm-bus inbox
llm-bus ack 123
```

`list` combines native peers with the most recently synchronized remote peers for the current repository. `list --all` includes remote peers for every locally permitted repository; the cache is not proof of connectivity or current server access. The server rechecks authorization before any remote delivery. Absolute checkout paths and local host socket endpoints are never sent to the relay.

A remote address includes machine ID, repo ID, and native agent address. Moving a native session to another repository changes its remote address. Incoming messages are imported only when the local recipient registration still matches the original repository mapping. Pending messages for moved sessions remain in the relay until the session returns to that repository or they expire.

Remote `send` commits a message and UUID outbox record locally, then returns `queued_local`. It does not wait for networking. The bridge uploads later. The original repository context stays attached to the outbox entry even if the sender session ends or moves before upload. Sending to a known ended session still stores the message remotely; a wake may fail and the recipient can read it on a later turn. An unknown remote address remains queued until registration permits delivery.

`remote-status` shows the latest 100 outgoing messages with local integer IDs, transport UUIDs, recipients, and the latest synchronized state:

| State | Meaning |
| --- | --- |
| `queued_local` | Durable on the sender machine; relay acceptance is pending. |
| `accepted_remote` | Durable in the relay mailbox; destination receipt is pending. |
| `delivered_local` | Destination committed its SQLite inbox copy. |
| `acknowledged` | Destination agent acknowledged its local message and the bridge synchronized it. |
| `expired` | An unreceived message reached the relay's 30-day delivery window. |

A Queue acknowledgement, a destination receipt, a host wake, and an agent acknowledgement are separate events. Host wake is best effort and uses the existing local adapter. Reads never consume messages. A failed wake leaves the inbox message pending for the recipient's next hook or turn. A crash after claiming a wake can defer notification until that next hook; the durable message is retained.

The bridge retries uploads, imports, receipts, and agent acknowledgements. UUIDs prevent duplicate mailbox rows. A repeated UUID with different content is rejected. Mailbox cursors restart at zero each synchronization pass, so out-of-order Queue delivery cannot skip earlier messages. Queues may reorder work; no total conversation ordering or exactly-once agent handling is promised.

## Revoke or rotate access

Revoke a machine without affecting other machines:

```sh
bunx wrangler d1 execute llm-bus --remote --command "UPDATE machines SET revoked_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id='alice-mac';"
```

Remove access to one repository:

```sh
bunx wrangler d1 execute llm-bus --remote --command "DELETE FROM repo_grants WHERE machine_id='alice-mac' AND repo_id='xyz';"
```

Every request checks current server permissions. Revocation or grant removal also withholds pending downloads from that machine or sender. Previously downloaded copies cannot be retracted. Positive authorization is not cached in the Worker.

To rotate a token, generate a new private file, update `machines.token_hash` to its hash, and update `credential_file` on the owner machine. Tokens are static, without automatic expiration, until revoked or rotated. Keep the original private file until the owner has the replacement; update the server hash when ready to cut over. The old token immediately stops authenticating after that update.

The relay authenticates machines, not individual processes or actual checkouts. Processes sharing an operating-system user are mutually trusted. A holder of a machine token can impersonate that machine's sessions within its server grants. The relay operator can read message plaintext. Message bodies remain untrusted coordination input, never user permission or approval.

## Recovery and local-only rollback

D1 holds unreceived messages for a 30-day delivery window independently of Queue retention. Expired messages are excluded from delivery and report `expired`; rows remain stored for history/status rather than being deleted automatically. Messages already committed locally retain the native mailbox's existing retention behavior. Monitor and archive relay rows according to operator storage needs.

Stopping the bridge suspends remote transfers without affecting local messaging. Remove or move the configuration file to hide remote peers and disable remote sends; leave local databases intact. Queued outbox entries can resume when the same configuration returns. Back up the SQLite mailbox and use Cloudflare's database backup facilities before destructive maintenance.

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
```

Worker tests use Miniflare D1. The bridge integration starts an isolated local Worker with Queues and exchanges a message between two temporary Python mailboxes, including recipient acknowledgement. No cloud credentials or real agent wakes are used. `bun run build` performs a dry-run Worker bundle; it does not deploy.
