# Remote bus implementation plan

## Features and effects

### F1: Machine authentication and repository isolation

Current messages stay on one computer. An operator assigns each machine a random static bearer token and grants explicit repository IDs. The Worker checks the token hash and live grants for every operation. Users need no Cloudflare account. No credentials are stored in the repository. Local user-owned policy maps Git common-directory scopes to shared repository IDs and restricts sending and receiving. The first release supports same-repository communication only; `--cross-folder` cannot bypass this restriction.

Acceptance: two authorized machines can discover and message sessions for `xyz`; an unauthorized machine, another repository, or a revoked token cannot register, discover, send, fetch, or acknowledge those messages. Task 1 implements the server boundary; Task 2 implements local policy.

### F2: Durable remote messaging

A per-machine bridge uploads session activity and outgoing messages, fetches incoming messages, stores them in the existing SQLite inbox, and requests the existing host wake. Messages survive offline machines and bridge restarts. UUIDs deduplicate transmission; local integer IDs preserve inbox, acknowledgement, and history interfaces. Task 2 implements delivery and Task 3 verifies the complete path.

## High-level design and rationale

Use a TypeScript Worker with D1 and one delivery Queue plus a dead-letter Queue. The same Worker exports API, consumer, and scheduled repair handlers. Messages in D1 double as durable dispatch records: an unset ready flag is repaired by the scheduled handler. Queue jobs carry only message IDs. D1 is the mailbox source of truth, so Queue retries and retention cannot lose mailbox contents. Start with HTTPS polling instead of WebSockets or Durable Objects.

Mailbox pagination restarts at cursor zero on every synchronization pass; cursors exist only within that pass to scan past locally withheld sessions. Durable receipts remove stored messages from later fetches. No insertion-order cursor is persisted, so late Queue delivery cannot be skipped. Reversed dispatch, lost receipt, and restart are mandatory integration checks.

Remote session addresses include machine, repository, and native session address. Changing checkout cannot reinterpret an old address. Local bridges expose only mapped sessions; local paths and host socket endpoints never leave the machine. Remote sender/recipient proxy rows are excluded from the native roster and never considered wake endpoints. Existing local routing and wake behavior remain supported.

Static tokens authenticate a machine, not an agent process or actual checkout. Processes sharing an OS user remain mutually trusted. The relay can read message plaintext. Pending cloud messages expire after 30 days, with explicit status; accepted local messages remain under existing retention behavior. Revocation prevents future downloads, not access to copies already downloaded. No cloud deployment, token provisioning, commit, push, or PR is performed during implementation.

## Data-flow analysis

| Stage | Input | Owner and output | Failure handling |
| --- | --- | --- | --- |
| Send | Current session, configured repository, remote address, body | Local SQLite atomically stores message and UUID outbox entry | Deny unmapped/cross-repository/disabled routes before storing |
| Upload | Durable outbox entry | Worker authenticates machine and stores identical UUID once | Same UUID with different payload conflicts; transient failures remain pending |
| Dispatch | D1 pending row | Queue consumer marks mailbox row ready | Scheduled scan repairs missed publication; retry and DLQ preserve evidence |
| Fetch | Authorized recipient and repository | Bridge stores local inbox row and receipt UUID atomically | Current session scope must match original repo; replay returns same local row |
| Wake | Newly imported local row | Existing wake adapter records independent result | Failed/unknown wake leaves message pending for hooks |
| Acknowledge | Agent `ack` | Existing local acknowledgement; bridge synchronizes cloud state | Recipient ownership and current machine grants checked server-side |

## Execution instructions

**Tech stack:** Python 3.12 standard library, SQLite, TypeScript, Cloudflare Workers/D1/Queues, bun, uv.

### Global constraints

- Application Python modules directly in `src/`, tests in `tests/`.
- `uv run poe check` is the repository quality gate.
- Preserve local APIs, migration safety, persist-before-wake, untrusted-message semantics, and no secret output.
- Use a feature worktree; never modify another session's checkout. No commits or deployment.

### Acceptance criteria traceability

| Feature | Tasks | Verification |
| --- | --- | --- |
| F1 token and repo boundary | 1, 2 | Worker API tests against local D1; Python policy tests |
| F2 persistence and deduplication | 1, 2 | Duplicate-send/import and restart tests, Worker retry tests |
| F2 host/inbox integration | 2, 3 | Existing pytest suite plus remote CLI/inbox/ack tests |
| Operational setup | 3 | Worker dry-run bundle, documented provisioning commands, package build |

### Execution topology

All tasks execute sequentially in `/tmp/llm-bus-remote` on `feat/remote-bus`. No parallel implementation permission requested. Current host model handles authoring; independent review uses the host's available inherited model controls. No model override requested.

### Task 1: Relay API and durable dispatch

- Create `cloudflare/src/index.ts`, `cloudflare/migrations/0001_bus.sql`, Wrangler configuration, package scripts, and runtime-backed tests.
- Read first: `src/llm_bus_store.py:Store.send`, `src/llm_bus.py:_send`, approved chat design.
- Interfaces: authenticated `POST /v1/sessions`, `GET /v1/sessions?repo_id=`, `POST /v1/messages`, `GET /v1/inbox?repo_id=&after=`, `POST /v1/receipts`, `POST /v1/ack`, `GET /v1/status?id=`. Session fields: native address, repo ID, last activity, ended state. Message fields: UUID, source native address, destination qualified address, repo ID, UTF-8 body. Worker derives sender machine from authentication.
- D1 owns machines, repo grants, sessions, messages. A single SQL statement conditions each mutation on current authorization; no cached positive authorization. Clients never call Cloudflare management APIs.
- Checks: invalid/revoked tokens, repo spoofing, recipient ownership, duplicate conflicts, queue replay, missing dispatch repair, persisted pending messages.
- Dead-code audit: wholly new server subtree; trace every exported handler through Wrangler and every route through tests. No existing behavior becomes obsolete.
- Gate: `bun run check`, `bun run test`, `bun run build` in `cloudflare/`; independent read-only code review after integration.

### Task 2: Local policy, durable bridge, CLI

- Create `src/llm_bus_remote.py`, `src/llm_bus_remote_store.py`, `tests/test_bus_remote.py`. Extend `src/llm_bus.py` command composition and `src/llm_bus_store.py` transaction boundary; register modules in `pyproject.toml`.
- Current-state evidence: `routing_scope` uses `git rev-parse --git-common-dir`; `_send` stores before wake; `Store.acknowledge` accepts only the owning recipient. Reuse all three contracts.
- Local config: optional `LLM_BUS_CONFIG`, default user config TOML; `[remote]` contains HTTPS URL, machine ID, credential file path; repository entries contain path, shared repo ID, `send_to`, `receive_from` (same repo only for v1). No configuration means fully local operation.
- Credential file must be a regular non-symlink file, with user-only POSIX permissions; errors must never include its contents. Reject URL credentials, redirects, non-HTTPS production URLs, duplicate repo mappings and malformed policy.
- Interfaces: `llm-bus bridge --once` synchronizes once; `llm-bus bridge` polls continuously. `list` includes policy-permitted remote peers from a durable cache; qualified `send` commits to outbox without blocking on network. Outgoing state and imported UUID mappings persist in SQLite. Native `inbox`, `ack`, history, and hooks retain their interfaces. Multiple bridge processes must not create duplicate inbox rows or acknowledgement effects.
- Checks: two stores/restart, current-scope changes, linked worktrees, missing/invalid policy, cache disclosure, transient HTTP errors, idempotent import, lost receipt, acknowledgement ownership, package contents.
- Dead-code audit: retain native store/wake/hook APIs and their tests. Remove no supported local behavior; update parser and packaging consumers together. No shadow implementation of native wake logic.
- Gate: focused remote pytest tests, then `uv run poe check`.

### Task 3: Setup, integration, and final review

- Create `docs/remote-bus.md`; link from `README.md`. Include secure token generation (hash output only), D1 machine/repo grant provisioning, Worker/Queue setup, config examples, manual bridge startup, revocation/rotation, expiry, and local-only rollback.
- Add mandatory end-to-end local Worker/bridge proof, including reversed Queue dispatch, lost receipts, and bridge restart. Never use real cloud credentials or live agent wakes in tests.
- Dead-code audit: docs-only changes have no code reachability impact; verify command/module references and package registration. No generated credentials or runtime databases committed.
- Gate: repository quality gate, Worker checks/tests/dry-run build, Python package build and independent aggregate review. Report cloud deployment as unverified until operator configures resources.
