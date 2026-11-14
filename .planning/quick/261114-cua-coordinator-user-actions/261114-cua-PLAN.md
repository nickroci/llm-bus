# coordinator-user-actions — Coordinator as user contact — revision 1

## Requirements and decisions

The coordinator becomes the default contact for worker requests that need user input or action. This plan changes agent guidance and its recovery reminders. It does not introduce a request service or expand coordinator authority.

- **R1:** Workers route questions, approval requests and manual-action requests to their current coordinator by default. The coordinator asks the user, obtains the response or required action, and follows up with the waiting worker.
- **R2:** Routing preserves worker consent, each machine owner's permissions, repository gates and the never-delegated floor. A routed response cannot supply authority that the worker does not already accept.
- **R3:** Command results, full and compact recovery guidance, canonical skill rules and user documentation describe the same default. Direct user instructions may override the communication default within their stated scope.

The user authorized planning only. Implementation, installation and execution permission are not requested or granted by this document. The reader is the implementing agent and its reviewers; their next action is to assess this bounded change before execution approval.

## Approach and material contracts

Reuse ordinary linked blocker messages, existing work reports, verified directions, delegated gate approvals and owner-decision records. Keeping communication separate from authorization avoids a new schema, CLI option, request queue, relay feature or delegation default.

- **C1 — Request routing (R1):** An active worker sends its coordinator one concise blocker request linked to the assignment when available. Include the worker's exact identity and session banner, affected ticket/branch, required user decision or action, and why work is blocked. Use existing `missing_information`, `needs_approval` or `dependency_blocked` report reasons as appropriate. Native subagents continue reporting only to their parent. Requests are untrusted data, not instructions or consent.
- **C2 — Coordinator handling (R1, R2):** The coordinator checks the request against current task ownership and authority, resolves discoverable facts, and asks only for the remaining user action. Preserve the requesting worker's identity and ticket/branch context in the user-facing request. Combine related questions when useful, without broadening their scope. Record the outcome in existing task/memory records and notify the worker with the applicable existing mechanism. Delivery, acknowledgements and a user's claim that an action happened do not replace the worker's required verification.
- **C3 — Authority (R2):** An accepted delegated gate uses existing verified approval or accepted gate-record contracts and live checks. Coordinator-recorded owner decisions remain opt-in, require `decision-show` with `accepted: true`, and retain owner-turn provenance and existing timed-default rules. Ordinary messages and `clarify` directions cannot substitute for either contract. Without the applicable binding, or for remote decisions unsupported by the current transport, the coordinator asks the user to perform the necessary direct action in the exact owning session or machine; the worker remains blocked until its existing requirements are satisfied.
- **C4 — Local actions and availability (R2):** The coordinator may collect and present requests for fresh joins, local consent, floor actions and manual host steps, but cannot provide that consent itself. Native permission prompts remain native; guidance must not suppress, bypass or promise to proxy them. No new silence-based approval or timeout is added. If the coordinator cannot be reached or current authority cannot be verified, preserve the blocker and yield; user-directed recovery or a direct user override governs further action. Inactive sessions receive recovery guidance, not instructions to resume project work or revive consent.
- **C5 — Guidance compatibility (R3):** Update `GUIDANCE_VERSION` and the unique `core.md` marker together from 10 to 11, with corresponding version assertions. Keep the existing file-selection contract. Full, compact and command-result guidance carry the active role's routing rule without requiring `--owner-decisions` or delegated gates. Existing version mismatch recovery remains in effect.

### Current evidence and limits

Baseline: `main` at `4535098e536902f186f828162ab700eee5813c3f`.

- `src/llm_bus/cli/guidance.py:_coordination`, `_worker_rules`, `WORKER_DUTIES`, `COORDINATOR_DUTIES` supply role reminders. `_worker_rules` currently tells workers to prefix direct owner confirmations. Hook output is consumed by `src/llm_bus/cli/hooks.py:GUIDANCE_VERSION` usage.
- `skills/llm-bus/references/coordination/core.md:Authority` defines the floor, consent and owner-confirmation banner. `gates.md:Worker live checks` currently says to ask the owner directly for floor actions. These instructions need reconciliation with C1–C4, while retaining local authorization.
- `src/llm_bus/coordination/decisions.py:ask`, `_require_owner_turn`, `_worker_reason`, `status`, and `owner-decisions.md` establish coordinator-only recording, opt-in acceptance and provenance limits. `delegation.py:effective` intersects project delegation with the worker's current binding. These mechanisms remain unchanged.
- `remote-coordination.md:Remote coordinator, local worker` explicitly keeps owner decisions local; `Remote gate approvals` requires permission on both endpoints. The communication default does not remove those limits.
- `tests/coordination/test_delegation_guidance.py`, `test_guidance_files.py` and `test_skill_files.py` cover current reminders, role/state selection, version matching and hook size. They do not establish the new routing behavior. No current graph was available; direct source, tests and references supply the evidence. Live installed behavior was not inspected.

## Acceptance criteria

- **A1 (R1, R3; T1):** For active workers with no delegation, gates only, and owner decisions bound, full/compact guidance and command results direct user-action requests to the coordinator. Active coordinator output assigns responsibility for collecting user responses and following up. Extend existing guidance tests to demonstrate these distinctions; retain inactive/unenrolled coverage.
- **A2 (R2; T1):** Canonical guidance covers information, delegated approval, unsupported/unbound approval, fresh consent, floor action, native permission and unavailable-coordinator scenarios under C1–C4. A review can identify who asks, which authority is required, and why a blocked worker may resume in each case. Existing decision, delegation and owner-gate tests continue passing; no authority implementation changes are needed.
- **A3 (R3; T1):** Version and file-selection tests pass with version 11. Existing realistic worst-case hook tests remain within the 8000-byte limit. User documentation no longer implies that routine worker requests should independently interrupt the user.

## Tasks and dependencies

Execution is one sequential task after plan approval and creation of an owned feature worktree from `main`. Parallel execution permission is not requested. No installation or live rollout is included.

### T1: Align user-request guidance and existing regression coverage

- **Deliverable:** Implement R1–R3 under C1–C5 and demonstrate A1–A3.
- **Ownership:** `src/llm_bus/cli/guidance.py`; canonical coordination references `core.md`, `worker.md`, `coordinator.md`, `gates.md`, `owner-decisions.md`, `remote-coordination.md`, and `recovery.md` where needed; `README.md:Optional project coordination`; `docs/coordination-simplified-flow.md:Role guidance and existing gates` and `Optional delegated gates`; `docs/tutorial.md` coordination/decision guidance and floor-action troubleshooting; affected existing guidance tests. Preserve authority, persistence, relay, host permissions and report schemas.
- **Inputs/outputs:** Consume existing direction, report, gate and decision contracts. Produce coherent role guidance and its version update; no new public data fields or commands. Routine wording and test parametrization are implementer choices within C1–C5.
- **Verification:** Extend existing guidance tests at the missing role/capability/state seams rather than creating a new harness. These regressions detect worker-direct questioning, a missing coordinator duty, and capability-dependent omission of the default. Use existing security tests for unchanged authority boundaries; use a semantic read of the scenario matrix for procedural behavior that string assertions cannot prove.
- **Cleanup obligation:** Replace contradictory direct-question instructions and their old text assertions across the owned consumers. Audit found reachable guidance, not removable authority code: `_worker_rules` is called by `_coordination`, and role/capability references are loaded through `guidance_files.py:select` and the coordination index. Retain owner decisions, delegated gates, direct-join requirements, host prompts and the historical diagram pinned to commit `8053152`. Do not add a duplicate rule file, queue or compatibility wrapper. Finish with scoped reference searches to distinguish legitimate direct authorization requirements from obsolete worker-direct questioning.

## Verification and rollback

Before running checks, inspect the current task entries and dependencies in `pyproject.toml`; use the existing Python 3.12/uv environment. Start with:

```sh
uv run pytest tests/coordination/test_delegation_guidance.py tests/coordination/test_guidance_files.py tests/coordination/test_skill_files.py
uv run pytest tests/coordination/test_decisions.py tests/coordination/test_decision_turns.py tests/coordination/test_decision_defaults.py tests/coordination/test_owner_gates.py tests/coordination/test_delegation.py
```

Inspect the final diff and changed cross-references. Run the repository-required `uv run poe check` before pushing a PR. Passing guidance tests prove emitted instructions, not that a live agent obeys them; report that limit. Follow existing independent review and plan-approval gates without adding another gate or runtime test service.

Ship the matching CLI and skill rules together through the existing release/install process only when separately authorized. This feature requires no migration. Before publication, rollback restores this task's code, tests and prose together. After publication, prefer a forward corrective guidance revision so cached sessions see the correction; never revert unrelated work or change local trust to recover behavior.

## Self-review receipt

Revision 1 maps R1–R3 to T1 and A1–A3. The author checked request flow, current consumers, authority boundaries, guidance selection, version refresh, cleanup reachability and rollback. The design reuses existing mechanisms and keeps unsupported remote acceptance explicit. No material product decision remains unresolved. Implementation and live behavior remain unverified; this is an author self-check, not independent approval. The controller's existing discovery and flow record hold review history and authorization state.
