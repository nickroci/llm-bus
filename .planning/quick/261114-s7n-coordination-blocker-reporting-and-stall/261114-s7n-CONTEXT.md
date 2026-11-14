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

Canonical execution ledger and role reports are under /Users/davidfava/.local/state/llm-bus/flows/coordination-stall-detection/. T1–T5 implementation and task reviews are accepted through `61420f44e9f531689aa863fcb77108c392570400`. Integrated verification and planner conformance passed on that clean committed state; `verification.md` and `conformance.md` qualify the retained `T5-fullcheck-final.log` result (`uv run poe check`, 956 tests plus static and package checks). The first T5 run failed test-setup lint; the repaired final run passed. PR publication is in progress into `main`; aggregate/PR review, current-head CI and merge remain pending. This phase-11 milestone changes tracking only and does not finalize the quick task. The installed CLI, skill, live mailbox and relay have not been upgraded by this flow.
