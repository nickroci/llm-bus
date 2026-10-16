# Local machine permissions implementation plan

## Features and effects

This proposal lets people collaborate through sandboxed VPS agents while attaching personal computers with narrower permissions. Each machine owner edits only their own configuration. There is no shared topology file or central topology administrator. The relay continues to authenticate machines and enforce existing repository admission grants.

### F1: Local permissions per repository and peer

- Problem and current behavior: remote configuration enables sending and receiving for an entire repository. It cannot distinguish trusted machines from other participants.
- Proposed behavior: each mapped repository lists exact peer machine IDs. Each peer independently permits outbound messages and chooses inbound handling: agent inbox, human monitor, or deny. Omitted peers and capabilities deny access. Effective communication requires the sender's outbound permission and receiver's inbound permission.
- Effect on code: a shared local configuration owner supplies policy to CLI, bridge, inbox, pending-count, and wake boundaries. No topology policy is uploaded to the relay.
- Scope and compatibility: local messaging stays unchanged. Old repository-wide remote permissions require explicit migration; they never imply an allow-all peer rule. Existing token and repository grants remain necessary, but are insufficient to authorize agent delivery locally.
- Acceptance outcomes: an allowed peer can communicate; another machine in the same repository cannot bypass local restrictions using raw relay calls. Linked worktrees share one policy mapping.
- Execution mapping: Tasks 1 and 4.

### F2: One-way updates with constrained return reports

- Proposed behavior: a PC can send normal messages to a VPS while accepting only built-in status/reason pairs for its own outgoing messages. Reports are recipient assertions, not verified execution outcomes or approvals.
- Effect on code: add a report endpoint and durable report state alongside existing transport acknowledgements. Reports never enter ordinary message bodies or native wake notices.
- Scope and compatibility: preserve transport states `queued_local`, `accepted_remote`, `delivered_local`, `acknowledged`, and `expired`. Transport acknowledgement must not stop subsequent work-report polling. No free-text report field or custom vocabulary.
- Acceptance outcomes: wrong-machine reports, unknown message IDs, extra fields, invalid combinations, stale revisions, and post-terminal changes are rejected. Valid reports update status only; they cannot trigger commands, retries, permission changes, or agent wakes.
- Execution mapping: Tasks 2–4.

### F3: Human monitoring and retained history

- Proposed behavior: inbound free text may be stored only in a separate monitor history, never in agent inboxes. Blocking a peer stops pending agent access and future wakes while retaining already stored history.
- Effect on code: separate monitor persistence and an explicit terminal monitor command. Existing human history viewer remains usable for native history; it must not accidentally pull monitor records into agent-facing commands.
- Scope: monitoring covers messages addressed to this machine's existing registered sessions. It does not silently subscribe to all repository conversations, introduce broadcast, or fabricate native session identities. The monitor command works without registering a new Claude/Codex session.
- Acceptance outcomes: monitor records survive restart and policy revocation; hooks and ordinary inboxes never expose them. Display escapes terminal control sequences. Reading monitor history has no acknowledgement or execution side effects.
- Execution mapping: Tasks 1, 3, and 4.

## High-level design and rationale

Reuse the existing authenticated relay, qualified addresses, durable outbox, import UUIDs, native inbox, and wake adapter. Add local per-peer capability checks at the existing boundaries. Extract configuration and policy into one Python module because the CLI, bridge, and pending-message queries all need the same rules without circular imports. Keep transport/storage in their existing modules.

The receiver is the security boundary. A remote machine can bypass its own client and upload a denied message to the relay; the protected client must reject it before native inbox import. This is acceptable transport storage, not permission to deliver. Ordinary relay sends therefore remain governed by authenticated repository admission, without a centrally maintained route graph.

Status reports use a separate typed channel. The relay authenticates the reporting machine and derives ownership from the original stored message. The recipient PC also validates the response and applies its own current report policy. The relay never supplies display labels, command text, or policy decisions.

Local policy is reread at each remote admission, upload, inbox/pending query, and wake attempt. A config change applies to the next guarded operation; a wake already submitted cannot be recalled. Invalid or missing remote policy disables remote delivery and pending remote exposure, while local-only commands continue to work. No permissive fallback to an earlier config snapshot.

History retention is deliberate. Already read content cannot be withdrawn. Explicit history/monitor commands can display retained content, but hooks and ordinary inboxes exclude newly blocked pending remote messages. Manual access to retained text is outside the automatic-delivery guarantee. Processes sharing the same OS user and credentials remain mutually trusted.

Security claim: the bus enforces local peer capabilities and restricts return reports to finite built-in data. This is not a zero-information channel: status choice and timing convey information. Reports are displayed as untrusted claims and do not authorize agent behavior. SSH, repository writes, shared files, and other non-bus channels are outside this feature.

Migration is additive for storage and fail-closed for remote policy. No live credentials, user configuration, grant tables, or deployments are modified by implementation. Rollback must disable remote bridges before restoring an older client that lacks these restrictions; downgrading while leaving remote operation enabled is not a safe rollback.

## Design diagrams

```text
Local sender policy -> durable outgoing message -> authenticated relay
                                                       |
                           local receiver policy <-----+
                             |        |        |
                           inbox    monitor   deny
                             |        |
                       native wake  separate history; no wake

Recipient report -> relay checks original message ownership -> sender polls
                     -> local enum/policy validation -> report status only
```

The graph emerges from independent endpoint policies; no shared graph configuration exists.

## Data-flow analysis

| Stage | Input and source | Processing and owner | Output or side effect | Material failure handling |
| --- | --- | --- | --- | --- |
| Configure | Owner's TOML, exact peer IDs | Local policy parser checks version, capabilities, duplicate peers, and repository mapping | Immutable policy for one guarded operation | Malformed/legacy policy denies remote work with migration guidance |
| Send/upload | Native sender and qualified destination | Local policy checks peer and repository before enqueue and again before upload | Existing durable outbox | Revoked permission holds unsent entries without deleting them |
| Receive | Relay-authenticated envelope | Check shape, local machine, peer, repository, native session scope, current receive mode | Agent import, separate monitor row, or no admission | Denied entries do not get agent rows, counts, receipt claims, or wakes; pagination still advances within scan |
| Expose/wake | Previously imported remote message | Current policy recheck before inbox/pending exposure and each wake | Allowed native access only | Retain blocked history; never convert it to a local message to bypass policy |
| Report | Status/reason and original message UUID | Relay resolves original recipient machine; validates built-ins and revision | Idempotent durable report, polled by original sender machine | Reject conflicts, spoofed ownership, invalid fields, and illegal terminal transitions |
| Display | Stored validated report/monitor record | Explicit command renders local labels or escaped monitor text | Human-readable output | No hook injection, wake, execution, resend, or approval effect |

## Execution instructions

**Tech stack:** existing Python/SQLite CLI, TypeScript Worker/D1/Queues, uv, bun; no new runtime dependency or web application.

### Global constraints

- Baseline inspected: `origin/main` at `b8422bf`; planning worktree `/private/tmp/llm-bus-machine-policy`, branch `feat/machine-policy`. Root checkout remains untouched.
- Application modules directly in `src/`; tests in `tests/`. Use `uv` for Python commands.
- User decisions: configuration purely local; built-in report vocabulary; separate human monitoring; retain history after blocking.
- No automatic execution based on reports. Never read, print, commit, or transmit credential file contents through the bus.
- Preserve repository scope, persist-before-wake, UUID deduplication, local messaging, native identities, and independent transport states.
- Before pushing, run `uv run poe check`. Conventional Commits; no `Co-Authored-By` trailers. No deployment or production configuration change is included.
- Human monitoring is a separate bus delivery path, not an OS-level guarantee that another process sharing the account cannot read its database.

### Local configuration contract

Use TOML, matching the existing configuration format. Require `policy_version = 2` at the top level; preserve `[remote]` connection/credential settings and repository paths/IDs. Replace `send_to` and `receive_from` with exact peer entries:

```toml
policy_version = 2

[remote]
url = "https://bus.example.com"
machine_id = "whitenoise"
credential_file = "/private/location/whitenoise.token"

[[repositories]]
path = "/path/to/starnet-observer"
repo_id = "starnet-observer"

[[repositories.peers]]
machine_id = "herdr-dev-1"
send_messages = true
receive_messages = "deny"
send_reports = []
receive_reports = ["received", "proceeding", "not_proceeding", "completed", "failed"]
```

`receive_messages` is exactly `deny`, `monitor`, or `inbox`; its default is `deny`. Boolean send permission defaults false. Report arrays default empty and contain unique built-in status names only. No wildcard peer or implicit reciprocal permission. Selected statuses permit their fixed built-in reasons; arbitrary reason sets and custom strings are not configurable. Reject unknown keys in policy tables and mixed legacy/new configuration. Duplicate peers, duplicate repository scopes/IDs, malformed identities, and self-machine entries are errors. Preserve existing private token-file and origin validation.

The corresponding VPS peer entry sets `receive_messages = "inbox"`, `send_messages = false`, `send_reports` to its allowed built-in statuses, and `receive_reports = []`. Policies for other peers are independent.

### Built-in report contract

| Status | Exact reason values |
| --- | --- |
| `received` | `null` |
| `proceeding` | `started`, `resumed` |
| `not_proceeding` | `needs_approval`, `missing_information`, `dependency_blocked`, `out_of_scope`, `declined` |
| `completed` | `null` |
| `failed` | `validation_failed`, `execution_failed`, `permission_denied`, `unavailable` |

`received` means the recipient explicitly reports receipt; automatic durable delivery remains `delivered_local`. Existing `ack` remains the transport/agent acknowledgement interface and does not manufacture work reports.

`POST /v1/reports` accepts exactly `{message_id, expected_revision, status, reason}`. The original recipient machine may report only for a message already receipted locally and currently authorized by repository grants. Derive its repository and source/destination identities from stored rows. Revision starts at zero; successful changes increment it atomically. An identical retry of the last accepted report with the same expected revision returns the same result. Other stale/concurrent submissions conflict. Terminal `completed`/`failed` allow identical retry only. Nonterminal states may change; repeated `received` cannot overwrite a later lifecycle state. A recipient can skip optional intermediate reports. Polling returns the latest revision, not an unbounded report log.

`GET /v1/reports?message_id=UUID` is restricted to the original sender machine and current grants. It returns exactly `{message_id, revision, status, reason}` for an existing report, or `{message_id, revision: 0, status: null, reason: null}` before the first report. No recipient-provided labels, timestamps, bodies, or error details. Unknown and unauthorized messages return the same denial class. Both Python and Worker validate identical status/reason fixtures.

Successful `POST /v1/messages` and `GET /v1/status` transport responses additionally include `report_expires_at`, an explicit UTC timestamp computed from the relay's stored message acceptance time plus its existing 30-day window. Include it on initial acceptance, identical replay, and race-replay responses. The client validates and persists this relay-owned deadline to bound report polling, independently of transport acknowledgement. Local enqueue time cannot serve this purpose because a message may wait offline before relay acceptance. A legacy relay that omits this field still supports normal transport; report polling remains unavailable until a compatible relay supplies a valid deadline. Invalid supplied deadlines are protocol errors and cannot enable reports or silently extend a stored deadline. This metadata does not change the exact report response schema or add central topology configuration.

### Acceptance criteria traceability

| Feature and requirement | Tasks | Verification |
| --- | --- | --- |
| F1: local per-peer deny-by-default and migration | 1, 4 | `uv run pytest tests/test_bus_policy.py tests/test_bus_remote.py tests/test_bus.py` |
| F1: policy changes stop pending exposure and wakes; retain history | 1, 4 | Revocation cases in the same suites, including restart and queued outbox |
| F2: report ownership, vocabulary, replay, terminal handling | 2, 4 | `uv run pytest tests/test_bus_reports.py tests/test_bus_remote.py`; `bun run test` in `cloudflare/` |
| F2: no report-to-inbox/wake/command path | 2, 4 | Spy wake and CLI tests; Worker/bridge integration verifies native pending counts unchanged |
| F3: monitor storage and terminal output isolation | 3, 4 | `uv run pytest tests/test_bus_remote.py tests/test_bus.py tests/test_herdr_viewer.py` |
| Packaging, documentation and supported behavior | 4 | `uv run poe check`; `uv build`; Worker checks/tests/dry-run build |

### Execution topology

| Wave | Task | Mode | Prerequisites | Isolation and integration | Wave verification |
| --- | --- | --- | --- | --- | --- |
| 1 | Local policy and enforcement | sequential-only | none | Feature worktree; shared configuration/store boundaries | Focused Python suites |
| 2 | Structured reports | sequential-only | Task 1 reviewed | Same branch; additive D1/SQLite changes | Python report tests and Worker tests |
| 3 | Monitor delivery | sequential-only | Tasks 1–2 reviewed | Same branch; bridge/CLI integration | Monitor and native regression suites |
| 4 | Integration and docs | sequential-only | Tasks 1–3 reviewed | Same branch; no live cloud mutation | Full repository and Worker gates |

- Parallel implementation permission: not-requested. No parallel edits are needed.
- Host/model policy: Codex; current host handles planning (runtime model controls not exposed to the author). Implementer/fixer follows its task skill defaults; independent reviewer uses `gpt-6.1-sol`, medium, when supported. No user model override exists. No independent approval of this written plan is claimed.

### Task 1: Local policy and all agent-delivery boundaries

**Goal / delivers:** F1 and the pending-history portion of F3.

**Execution:** Wave 1, sequential-only; no prerequisites. Own configuration, CLI admission, bridge admission, native pending filters, and their tests on this feature branch. Review before Task 2.

**Files:** Create `src/llm_bus_policy.py`, `tests/test_bus_policy.py`. Modify `src/llm_bus_remote.py:Repository, Config, load_config, enqueue_remote, remote_peers, Bridge`; `src/llm_bus.py:_hook, _registered_command`; `src/llm_bus_store.py:Store.inbox, Store.pending_count`; `src/llm_bus_remote_store.py:RemoteStore.import_message`; `tests/test_bus_remote.py`; `tests/test_bus.py`; `pyproject.toml` module registration. Docs: migration section delivered with Task 4.

**Read first / current-state evidence:** `llm_bus_remote.py:_repository` produces repository-wide send/receive booleans; `enqueue_remote` checks repository, not peer. `Bridge._receive` imports before native wake. `RemoteStore.import_message` retains the qualified sender and original scope; use these immutable identities for later policy checks. `_hook` obtains its count through `Store.pending_count`; filtering only the bridge would miss already imported pending content.

**Intended edits / interfaces:** Move the existing config/credential-origin policy parser to `llm_bus_policy.py`, updating actual imports and tests instead of maintaining compatibility re-exports. Define `PeerPolicy(machine_id, send_messages, receive_messages, send_reports, receive_reports)` and `Config.peer(repo_id: str, machine_id: str) -> PeerPolicy | None`. Preserve `Config.repository(project)` and connection binding semantics. The store's existing remote-import identity provides the guard input; use the shared policy owner for remote pending queries, while native senders need no remote configuration. Config absence/parse failure must never expose remote inbox content. Hold denied outgoing rows and skip denied inbound rows without aborting other allowed traffic. Recheck before native wakes, including deduplicated imports and process restart. No raw remote body enters denial diagnostics.

**Dead-code scope:** Extracted config definitions become obsolete in `llm_bus_remote.py`; update CLI/tests/packaging imports and remove the old definitions once consumers move. Keep all supported relay, native store, and wake APIs. No deletion based on search counts alone.

The hook's capability guidance must come from local policy only: explain allowed send/report options and denied reply capabilities without embedding remote report values or monitor text. A denied send returns a local capability error rather than inviting an alternate route.

**Review target:** Peer/repository identity checks, old-config fail-closed behavior, pending inbox/count/wake bypasses, linked worktrees, outbox retention, and local-only operation.

- [ ] Step 1: Add policy parser cases, raw-relay bypass simulation, and pending revocation tests; inventory current `Repository`/`Config` consumers.
- [ ] Step 2: Run `uv run pytest tests/test_bus_policy.py tests/test_bus_remote.py tests/test_bus.py`; confirm failures come from missing peer enforcement, not invalid fixtures. No source removal yet.
- [ ] Step 3: Implement the common policy owner and guards; remove migrated definitions and update packaging/consumers together.
- [ ] Step 4: Rerun those suites; verify deny leaves existing history intact and local-only hooks functional. Search final imports for obsolete config owners.
- [ ] Step 5: Run `uv run poe check`; verify no duplicate config parser or registered-but-unused module.
- [ ] Step 6: Independent read-only code review explicitly using `code-reviewer`; record tests, findings, cleanup evidence, and any policy-reload race limits.

### Task 2: Typed work reports on a separate return channel

**Goal / delivers:** F2.

**Execution:** Wave 2, sequential-only after Task 1. Own report protocol, local report state, endpoint handlers, CLI, and protocol fixtures. D1 migration belongs only to this task. Review before Task 3.

**Files:** Create `src/llm_bus_reports.py`, `tests/test_bus_reports.py`, `cloudflare/migrations/0002_reports.sql`. Modify `cloudflare/src/index.ts:receipt, status, fetch`; `cloudflare/test/relay.test.ts`; `src/llm_bus_remote_store.py:RemoteStore`; `src/llm_bus_remote.py:Bridge._upload, Bridge.sync`; `src/llm_bus.py:_parser, main`; `tests/test_bus_remote.py`; `pyproject.toml`. Put shared report-case fixtures in `tests/fixtures/report-cases.json` and load them from both languages.

**Read first / current-state evidence:** Worker `receipt` authenticates the original recipient machine; reuse that ownership pattern. `_upload` polls transport status only; `RemoteStore.outgoing` excludes transport-acknowledged rows. Independent report polling must keep eligible nonterminal original messages visible. Current successful message/status responses contain no relay acceptance/deadline timestamp, so report retention needs the additive metadata contract above. `RemoteStore.import_message` is the native inbox path and must never consume a report.

**Intended edits / interfaces:** Implement the exact report API above and built-in validators, plus the relay-owned transport deadline and backward-compatibility behavior above. Add an additive D1 `message_reports` table keyed by original message ID with revision/status/reason/last expected revision; original transport records remain unchanged. Add durable SQLite outgoing report submissions and latest received report snapshots alongside `RemoteStore`; use exact local receipt metadata to locate original UUIDs. Introduce `llm-bus report MESSAGE_ID --status STATUS [--reason REASON]` for a recipient's imported native message ID; missing required reasons and reports for outgoing/local-only messages fail. Queue reports durably, enforcing current `send_reports` before enqueue and upload. Extend `remote-status` with separate transport state and validated work-report fields. Poll original outgoing messages only while current `receive_reports` permits reports, within original delivery retention and until terminal work report. Report expiry means unknown/no new reports, never permission for automatic retry. Schema and local binding reject replay from a different message/peer/repository. Unknown report fields/values fail validation before persistence or display. Verify delayed/offline acceptance, deadline expiry, replay consistency, and a legacy transport response without the new field.

**Dead-code scope:** Reuse transport acknowledgements without renaming or collapsing them into work reports. Trace `_upload`, `outgoing`, `statuses`, and report command wiring; remove only duplicated enum validators superseded in Python. Worker/Python vocabulary parity is checked by shared case data, not a new schema runtime.

**Review target:** Machine-authenticated ownership, no trusted submitted routing fields, compare-and-set concurrency, immutable terminal states, strict field types, report and transport polling independence, no native wake path.

- [ ] Step 1: Add report vocabulary fixtures, wrong-recipient/unknown-ID cases, concurrent/retried reports, and spy assertions for zero report-triggered wakes.
- [ ] Step 2: Run `uv run pytest tests/test_bus_reports.py tests/test_bus_remote.py` and `bun run test` in `cloudflare/`; confirm missing endpoint/state failures. Preserve existing acknowledgement tests.
- [ ] Step 3: Implement additive storage, relay routes, validators, durable report submissions, and status polling. No production migration is run.
- [ ] Step 4: Rerun focused tests; verify old revisions, wrong types including booleans-as-integers, extra fields, and non-receipted messages reject. Trace all new command/route consumers.
- [ ] Step 5: Run `uv run poe check`, then `bun run check` and `bun run test` in `cloudflare/`; verify fixtures and modules ship or remain test-only as intended.
- [ ] Step 6: Independent `code-reviewer` review of report ownership and both persistence models; record proof and cleanup scope before Task 3.

### Task 3: Separate monitor history

**Goal / delivers:** F3 monitoring, without an agent inbox or wake.

**Execution:** Wave 3, sequential-only after Task 2. Own monitor storage and CLI; shared bridge paths prevent parallel editing. Review before aggregate verification.

**Files:** Modify `src/llm_bus_remote_store.py:RemoteStore`, `src/llm_bus_remote.py:Bridge._receive`, `src/llm_bus.py:_parser, main`, `tests/test_bus_remote.py`, `tests/test_bus.py`. Read `plugins/herdr-history/viewer.py:message_lines` and `tests/test_herdr_viewer.py` for existing terminal rendering conventions; retain native viewer behavior.

**Read first / current-state evidence:** `_receive` currently sends all accepted messages to `import_message`, then requests a wake. Native `history` reads `messages`. Monitor delivery therefore needs separate storage, not a special message body or recipient flag inside native history.

**Intended edits / interfaces:** Add `remote_monitor` rows containing immutable envelope identity and content, deduplicated by UUID. Route `receive_messages = "monitor"` there exclusively and receipt only after durable commit. Replay must not migrate a UUID previously accepted into one delivery class into the other; retain first admission class and reject mismatched content. Deny-to-allow changes may admit relay-pending records later; changing monitor-to-inbox never promotes previously monitored text. Add `llm-bus monitor [--repository PATH | --all] [--before ID] [--limit N]`, outside host-registration dispatch. Render a plain escaped terminal history; no HTML, shell links, terminal escape sequences, or implicit JSON-to-agent integration. No separate dashboard/service. Retained monitor history remains explicitly readable after peer removal or config loss; connection binding and repository snapshot remain stored. Do not import monitor contents into hooks, inbox, native history, pending counts, or report display. `received` work reports remain explicit, not automatically generated from monitor reads.

**Dead-code scope:** Native history/viewer remain supported. No replacement of their reader or removal is planned. Keep monitor entrypoints isolated and trace parser -> store -> display; avoid a parallel agent registry.

**Review target:** Monitor/inbox exclusivity across policy changes and retries, durable receipt order, terminal escapes, explicit history retention, absence from every automatic agent path.

- [ ] Step 1: Add monitor delivery/restart/duplicate tests, config-revocation history reads, hostile terminal content, and pending/wake exclusion assertions.
- [ ] Step 2: Run `uv run pytest tests/test_bus_remote.py tests/test_bus.py tests/test_herdr_viewer.py`; confirm missing monitor path failures without changing native-history expectations.
- [ ] Step 3: Implement separate table and command, rendering, admission-class deduplication, and receipt-after-commit. Keep the existing native viewer contract.
- [ ] Step 4: Rerun focused tests and final-reference searches for monitor consumers; confirm none feed hook/inbox/wake code.
- [ ] Step 5: Run `uv run poe check`; verify non-session monitor invocation does not register a fake native agent.
- [ ] Step 6: Independent `code-reviewer` review of isolation, retention, and rendering; record no native viewer removal and all consumer traces.

### Task 4: Topology integration, migration guide, and delivery gates

**Goal / delivers:** Aggregate F1–F3 with reviewable setup and proof.

**Execution:** Wave 4, sequential-only after Tasks 1–3. Own integration tests and user documentation. No cloud account, credentials, live policy, or remote checkout is mutated.

**Files:** Modify `cloudflare/test/bridge.test.ts`, `tests/test_bus_remote.py`, `tests/test_bus.py`, `docs/remote-bus.md`, `README.md`. Keep this plan as proposed design until actual proof is recorded.

**Read first / current-state evidence:** `cloudflare/test/bridge.test.ts` already uses a real local Worker, isolated D1/Queues, and temporary Python mailboxes. `docs/remote-bus.md` owns setup, grants, token handling, retention, and rollback. Update those contracts rather than add a second setup guide.

**Intended edits / interfaces:** Expand the existing local integration to three machines: trusted PC, controlled VPS, outsider. The VPS accepts outsider text; the PC accepts only reports from its configured VPS. Assert direct and VPS-forwarded normal messages cannot reach the PC inbox; a valid report is accepted, arbitrary text cannot enter its report fields, and a false valid report remains only data. Add a monitor-only recipient, independent sender/receiver policy disagreement, config removal, policy reduction between queue and import, pending revocation, restart, lost receipts, delayed report revisions, report transport acknowledgement independence, and terminal-state checks. Explicitly distinguish content blocked by local policy from transport uploaded to the relay.

Document TOML version/migration, complementary endpoint examples, per-peer capability semantics, monitor scope, report CLI/reasons, policy-change timing, retained history, no-central-topology ownership, and safe rollback. Add a brief README link to the owning guide. Do not describe reports as proof of work completion, stronger than machine authentication, or a zero-information channel.

**Dead-code scope:** Audit updated docs/CLI examples for obsolete `send_to`/`receive_from` setup and old all-peers claims. Historical schema documentation may remain explicitly historical; active setup and fixtures must use the new policy. Retain supported native CLI/tests and Worker admission-grant functionality.

**Review target:** Requirement traceability, independent local policy behavior, reverse-message bypasses, upgrade/downgrade safety, full native regression coverage, and evidence-backed documentation.

- [ ] Step 1: Add the three-machine integration and migration characterization tests; identify every current active-config example.
- [ ] Step 2: Run the focused new integration under `bun run test` in `cloudflare/`; confirm the intended security assertions expose gaps before changing integration code.
- [ ] Step 3: Complete integration wiring and update owning documentation/examples; remove active references that imply repository grants alone trust all peers.
- [ ] Step 4: Rerun integration and focused Python suites. Verify retained history and protected inbox/pending counts after restart and revocation.
- [ ] Step 5: Run `uv run poe check`, `uv build`, and `bun run check`, `bun run test`, `bun run build` in `cloudflare/`. Expect all gates to pass; tests use isolated resources and mock native wakes.
- [ ] Step 6: Fresh independent `code-reviewer` review of the complete diff and cleanup evidence. Record exact results, review limitations, and residual risks. No deployment, push, PR, or production readiness claim without its corresponding verification.

## Plan self-review and evidence limits

- Requirements map to F1–F3 and all four tasks. No shared topology policy, implicit wildcard, arbitrary report text, or automatic action route remains in the proposal.
- Source evidence inspected at the stated baseline: configuration, CLI/hook dispatch, remote enqueue/import/status/report-adjacent paths, native pending/history composition, Worker routes and schema, package gates, existing integration tests, and history viewer tests.
- Dead-code audit mode: read-only. No source diff or confirmed obsolete runtime artifact exists yet. Task 1 owns removal of extracted config definitions; all other existing transport/native/acknowledgement contracts remain reachable and supported. Tests and docs must migrate with those owners.
- Author self-review is not independent approval. Runtime tests are intentionally not claimed for a plan-only change. Implementation will require fresh task gates and independent reviews.
- No unresolved product decision remains. Monitoring is scoped to addressed messages; broader subscriptions are outside this feature. Deployment and real-machine setup remain separate authorized operations.
