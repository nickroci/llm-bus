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

Canonical execution ledger and role reports are under /Users/davidfava/.local/state/llm-bus/flows/coordination-stall-detection/. T1–T4 implementation and task reviews are accepted through `8dbfd09838191050724ae1e0c76999f47389f587`. T5 updates existing guidance and references, extends observer-specific trust-boundary assertions, and runs integrated verification. T5 review, aggregate review and PR delivery remain pending. The installed CLI, skill, live mailbox and relay have not been upgraded by this flow.
