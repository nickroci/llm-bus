# Coordination blocker reporting and stall detection

## Registration

No GSD tooling wrote this record. This client exposes no installed GSD quick workflow; this tracked record uses the personal workflow fallback.

The approved revision `stalls-v1` plan is preserved verbatim in `261114-s7n-PLAN.md`. The canonical private draft is .planning/drafts/coordination-stall-detection/PLAN.md in the primary checkout. The user approved implementation after independent design review. Approval and execution evidence remain in the parent-owned flow records; historical planning-only statements in the preserved plan describe its authoring stage.

## Decisions

- Workers send same-step blocker or decision requests and phase-change status through linked messages. Native subagents report only to their parent.
- Coordinators receive informational durable stall notices for eligible open assignments and reviews, with a configurable local interval defaulting to 30 minutes.
- Reuse detached timers, durable mailbox notices and existing wake delivery. Notices grant no authority and trigger no automatic remediation.
- Preserve decentralized machine policy, authenticated admission, report isolation, generation and grant boundaries, and retained history.
- Execute T1–T5 sequentially in the isolated feature worktree. Installation, deployment, live database changes and active-session restarts remain outside this task.

## Discovery

Existing work-state projection and report validation supply the lifecycle and evidence seams. Stall detection adds additive schema-16 local state, a read-only inspection command, a local configuration command and bounded timer reconciliation. Remote evidence uses local admission time; unknown historical remote observations require an explicit recovery baseline. Machine sleep, process loss and unsupported wakes require recovery through existing normal entry points.

Implementation starts from main at `d1b8712de466f7b6f9bc9f31c5546ef7a74c79cc` on branch `feat/coordination-stall-detection` in /Users/davidfava/Projects/Personal/llm-bus-stall-detection. The primary checkout has unrelated untracked `.vscode/`, which remains untouched. Global client loaders apply; no per-worktree loader is required.

Canonical execution ledger and role reports are under /Users/davidfava/.local/state/llm-bus/flows/coordination-stall-detection/. T1–T5 implementation and task reviews are accepted. Aggregate/PR review R1 found one Medium issue: an admitted linked availability event could reset the communication baseline. The repair excludes these events from ordinary-reply evidence and adds regression coverage for overdue and already-notified assignments. Review R2 is complete and Clean, with requirements and quality Approved on `1166e361549d3755bd8f1779bc67586049cc5974`; no findings remain.

`verification-r2.md` binds that committed content to `pr-fix-r1-check.log` and its explicit successful `uv run poe check` receipt: 957 tests passed in 202.45s, with static, type and package checks. `conformance-r2.md` establishes conformance to `stalls-v1` for the identical frozen repair. Earlier 956-test results remain historical. Normal commit and push hooks passed. Both Repository and Relay quality jobs passed in CI run `43649733983` on the reviewed head; the earlier cancelled run is not a success receipt.

PR #90 is open into `main`: https://github.com/wnz99/llm-bus/pull/90. The final tracking-only summary is prepared during phase 11 after code-review convergence. It records completed implementation, not a completed merge or flow. Fresh tracking validation and CI on the final pushed head remain required before merge; phase 13 starts after merge. The installed CLI, skill, live mailbox and relay have not been upgraded by this flow. See `261114-s7n-SUMMARY.md` for results and rollout limits. No GSD tooling validation is claimed.
