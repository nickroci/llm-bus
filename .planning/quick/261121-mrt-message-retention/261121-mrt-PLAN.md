# message-retention — Message delivery and retention

## Requirements and decisions

**R1.** Unreceived relay messages become ineligible for delivery 48 hours after relay acceptance.

**R2.** Delete messages and message-bearing copies at 14 days in the relay and local agent mailbox, regardless of acknowledgement or coordination status. Include native messages, queued and imported envelopes, monitor records, and message-linked reports. The separate opt-in collaboration database is outside this plan. Copies in the main agent mailbox follow its policy.

**R3.** Preserve deduplication, identifier integrity, local trust and admission checks, coordination authority, and replay protection for provider operations.

**R4.** Apply the new cutoffs to existing records when authorized maintenance starts. Drain the backlog in bounded transactions.

**R5.** Provide body-free previews and a documented rollout procedure. Production deployment and deletion require separate operator authorization.

## Contracts

### C1. Age and delivery

Use one captured UTC instant per operation. Relay age starts at acceptance. Native and locally queued messages age from their stored enqueue timestamps. Imports and monitor records age from the original relay acceptance timestamp. Acknowledgement, imports, retries, or recent replies never reset a message's age. Validate and normalize timestamps before using them for destructive indexed comparisons. Stop with a body-free diagnostic if a legacy record has an invalid timestamp.

Apply the 48-hour window to all relay inbox classes, first receipts, queue dispatch, and scheduled repair. The receiver rejects a first local admission after the delivery deadline, including a response buffered across the boundary. An item durably admitted earlier remains subject to local acknowledgement and 14-day retention. Already delivered messages keep their transport status and can be acknowledged until retention ends. Repeated receipts remain idempotent until then. Local-only delivery follows its existing behavior until the message is deleted.

### C2. Maintenance and visibility

Use the existing relay cron and bridge cycles. Run bounded maintenance on normal writable local mailbox activity with a rate limit. Keep purge separate from schema migration. Read-only status commands do not migrate or delete data. An idle local mailbox physically purges when the bus next runs. Normal history, inbox, pending, monitor, upload, report, and wake paths enforce the cutoff regardless of the purge backlog.

### C3. Deletion, replies, and identifiers

In one transaction, select a bounded batch, record content-free replay identifiers, detach surviving children, delete dependent rows, and remove original messages. Recent reply bodies and timestamps survive. Clear nullable database parent links for surviving children and preserve existing accepted envelope JSON while the child remains retained. Reject a new reply to a parent outside retention. Admit an already accepted recent child with a missing local parent only when a replay guard confirms the parent was purged and independent policy and authority checks pass. Preserve current checks for unknown parents.

Retain only purged remote UUIDs in a content-free replay ledger. Configure insertion to reject UUID reuse atomically. Delayed queue jobs cannot recreate deleted records. A purged status request uses the existing unavailable or denied route. Keep local native message and monitor IDs monotonic even when the highest ID or every record is removed. Use persisted high-water allocation in the existing write transaction and seed it from stored rows and durable references.

### C4. Reports and compatible versions

Preserve the immutable report deadline already advertised for legacy records. Store a 14-day report deadline for new relay records. Use the 30-day fallback only for legacy stored rows. Advertise a separate `retention_expires_at` field with the 14-day deadline. Report access requires both current retention and the original report deadline. Updated clients apply the earlier limit without rewriting saved legacy report deadlines. Remove message-linked report submissions, snapshots, and synchronization rows with their message. The separate client transport observation horizon keeps its current meaning.

The first complete release implementing C1–C6 sets the minimum compatible version for relay workers and local clients. Record the exact release identifiers before production deployment. After activation, every relay worker sharing the database and every process accessing an activated mailbox must meet this floor. Use the offline cutover in C6 to replace legacy workers and already-open local writers.

### C5. Coordination and recovery

Delete message-specific direction, approval, wake coverage, work report, and stall rows. Clear surviving stall-notice references to deleted messages. Maintain timer and wake invariants without generating new authority or reopening work. Preserve projects, grants, claims, runs, decisions, independent gate records, and provider reconciliation state. Give availability ordering and deduplication durable content-free bindings so that deleting an old envelope cannot recreate a notice or reintroduce an older target generation.

### C6. Offline cutover, recovery, and backups

Use existing host and provider controls. All implementation tasks must pass before either policy is activated. Obtain separate permission for production deployment and the first deletion.

1. Block relay HTTP ingress, pause queue consumption and cron, and stop local bridges, hooks, daemons, and other mailbox processes, including automatic restarts. Disable relay access for endpoints that cannot meet C4. Finish or terminate in-flight operations and verify that legacy invocations and local readers or writers have stopped. Leave queued jobs for the compatible release.
2. While access is stopped, record versions and preview expired delivery, deletion candidates, and the oldest eligible timestamps without reading content. Validate legacy timestamps. Apply migrations separately from purge, then repeat the preview. Uncertain process or version state and invalid data block activation. Deploy the compatible relay and upgrade or restart every process accessing a local mailbox, including any process with an existing `Store`.
3. Review the actual backlog and cutoffs. After deletion authorization, allow the compatible release to begin writable activity and bounded maintenance. Resume HTTP ingress, queue consumption, cron, and local automation only after confirming compatible versions. Keep unupgraded endpoints disconnected until their mailbox cutover completes.

If cutover fails, block ingress, pause queue and cron, and stop local delivery, uploads, wakes, and maintenance before diagnosing the failure. Resume only a verified compatible release after previews and integrity checks. Keep older releases off activated stores, including during rollback.

Deletion removes content from active application tables. Backups, disk snapshots, and host transcripts follow their own policies. Restore backups offline. Before resuming any access, apply the compatible schema, reconcile replay UUIDs and ID high-water marks with post-cutover state, and apply current retention checks and authorized deletion. If replay or identifier safety cannot be confirmed, keep the restored service stopped.

## Acceptance criteria

1. Prove delivery behavior immediately before, at, and after 48 hours in each relay message class, first and repeated receipts, delayed dispatch, repair, and local admission. Verify delivered and acknowledged status survives the delivery cutoff.
2. At 14 days, remove acknowledged, pending, denied, monitor, never-uploaded, active coordination, and linked report cases. Keep a newer reply after deleting its old parent. Verify fair bounded backlog progress and idempotence.
3. Verify duplicate uploads, delayed queue jobs, reconnects, report retries, empty mailboxes, and highest-ID deletion cannot resurrect content or reuse an identifier.
4. Verify availability deduplication and ordering, provider-operation reconciliation, stale stall notice suppression, and policy denial. Keep monitor and report data out of the native inbox and wake path.
5. Migrate supported legacy databases without changing advertised deadlines or breaking foreign keys. Verify that a compatible response preserves an already advertised 30-day deadline and consistently advertises 14 days for new records. Rehearse C6 on disposable stores with older HTTP, queue, cron, and already-open local `Store` processes. Verify they cannot reach activated stores. Resume compatible processes and verify that delayed jobs and repeated uploads cannot recreate purged UUIDs. Inject migration and purge failures and verify transaction rollback and the stopped recovery state.
6. Verify body-free previews against disposable fixtures and document what happens on first activation, on an inactive host, and during authorized restoration.

## Tasks and dependencies

Execute sequentially in an isolated worktree from current `main`. Revalidate the starting revision before source edits.

1. **Migration and identifier safety.** Extend existing schema and insertion owners with normalized age indexes where needed, replay guards, monitor ages, monotonic local IDs, and stable relay deadline storage. Verify additive migration and ID behavior in real SQLite and D1 fixtures. Replace implicit ID allocation at each main-mailbox insertion site.
2. **Relay delivery and bounded purge.** Consume the new relay storage contract. Replace the overloaded 30-day delivery and report predicate. Apply delivery eligibility at all entry points and run atomic bounded deletion with dependency cleanup, reply detachment, and replay protection. Preserve legacy report metadata and test first/repeated receipts, queue repair, expiry, and concurrent retries.
3. **Local purge and coordination.** Consume the new local storage contract. Delete dependent envelope, monitor, report, synchronization, wake coverage, message-approval, and stall rows in order. Replace availability's message-backed deduplication and ordering with retained content-free bindings. Protect authority and provider reconciliation records. Wire bounded maintenance into existing writable and bridge paths.
4. **Exposure and synchronization.** Consume both stores' age and deletion contracts. Apply age checks across history, pending, monitor, native wakes, remote upload and admission, and report paths. Handle concurrent deletion and already accepted recent replies without reviving old records. Keep trust and local policy checks at the same effect boundaries.
5. **Preview, rollout, docs, and proof.** Add a read-only local CLI preview that returns candidate counts and oldest eligible timestamps. Document equivalent relay preview queries, the exact release identifiers, and the C6 cutover and recovery steps. Rehearse the cutover on disposable services with existing fixtures. Verify focused Python and Cloudflare suites, then required aggregate checks, including `uv run poe check` before push. Revise current indefinite-retention and 30-day delivery documentation and remove owned disposable resources.

## Verification and rollback

Run boundary, migration, permission, replay, and concurrency tests on disposable data. Use query plans to justify new indexes for age-ordered batch selection and dependent lookup. Verify the C6 cutover rehearsal before production deployment. Keep migration, deployment authorization, and first-deletion authorization separate. Rollback and restoration preserve the compatible-version floor.

## Source evidence

The planner inspected `origin/main` at `a3ed833c1e6318b6ffc81c7c7b211ccf9888fa9f`. Delivery and report predicates live in `cloudflare/src/messages.ts` and `reports.ts`. The existing cron runs through `cloudflare/src/index.ts`. Local schema, inserts, and pending paths live under `src/llm_bus/mailbox`. Remote outbox, monitor, admission, and reports contain retained copies. `reports/snapshots.py` rejects changes to saved deadlines. `coordination/availability.py`, `stalls.py`, and `recovery.py` own message-linked state with distinct lifetimes. The plan replaces the overloaded relay predicate and message-backed availability joins while retaining report validation and provider replay guards.
