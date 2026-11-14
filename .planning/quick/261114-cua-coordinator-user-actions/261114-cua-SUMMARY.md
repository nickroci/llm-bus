# 261114-cua — Summary

PR [#89](https://github.com/wnz99/llm-bus/pull/89) targets `main` from `feat/coordinator-user-actions`. Implementation and independent PR review passed; merge remains pending.

## Results

Active workers now route questions, approval requests and manual-action requests through their current coordinator by default. The coordinator preserves the worker's identity and task context, collects the user's response or required action, records the outcome and follows up with the worker.

Guidance version 11 and its matching canonical skill marker carry this default across full, compact and command-result guidance, including workers without delegated gates or owner decisions. Canonical references, README, the simplified flow and tutorial describe the same behavior. Direct user instructions may override the communication default within their stated scope.

Routing preserves existing authority contracts. Messages do not grant consent or expand permissions. Delegated gates, accepted owner decisions, direct local authorization, fresh joins, the never-delegated floor and native permission prompts retain their requirements. Unsupported remote authorization and an unreachable coordinator leave the worker blocked under existing recovery rules. No transport, schema, queue, migration or delegation default was added.

## Verification and reviews

The implementation report records these successful commands:

- `uv run pytest tests/coordination/test_delegation_guidance.py tests/coordination/test_guidance_files.py tests/coordination/test_skill_files.py`: 42 passed.
- `uv run pytest tests/coordination/test_decisions.py tests/coordination/test_decision_turns.py tests/coordination/test_decision_defaults.py tests/coordination/test_owner_gates.py tests/coordination/test_delegation.py`: 121 passed.
- `uv run poe check`: exit 0, including static checks, all 907 tests and isolated sdist/wheel installation checks.

Independent task review approved requirements and quality with a Clean outcome. Planner conformance returned CONFORMS, and final verification returned PASS. Aggregate PR review R1 approved requirements and quality with a Clean outcome and no findings on `ac8c5cb7c54d99110906f2afc926101d1b00d752`.

Both `Repository / quality` and `Relay / quality` passed on that reviewed head in [CI run 43623741103](https://github.com/wnz99/llm-bus/actions/runs/43623741103). The final tracking-only commit requires fresh documentation validation and CI on its own head before merge; the earlier full review is not a claim that this later commit was reviewed.

Canonical evidence is retained under /Users/davidfava/.local/state/llm-bus/flows/coordinator-user-actions/: implementation.md, task-review-r1.md, conformance.md, verification.md and pr-review-r1.md. The approved revision 1 plan remains verbatim in `261114-cua-PLAN.md`.

## Pending work and limits

After tracking validation and current-head CI pass, merge PR #89 using the repository's merge-commit policy. Final flow reconciliation and temporary-resource cleanup follow the merge. This summary and the completed-task row record premerge delivery, not a completed merge or flow.

Installation, deployment and live provider operations require separate authorization. Ship the matching CLI and canonical skill rules together through the existing install process when authorized. Emitted-guidance tests establish the instructions produced; they do not establish live agent compliance or actual native permission interactions.
