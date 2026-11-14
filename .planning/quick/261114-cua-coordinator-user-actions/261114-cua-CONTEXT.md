# Coordinator as user contact

## Registration

No GSD tooling wrote this record. This client exposes no installed GSD quick workflow; this tracked record uses the personal workflow fallback.

The approved revision 1 plan is preserved verbatim in `261114-cua-PLAN.md`. The canonical private draft is .planning/drafts/coordinator-user-actions/PLAN.md in the primary checkout. The user approved implementation after independent plan review.

## Decisions

- Workers route questions, approval requests and manual-action requests through their current coordinator by default.
- Routing does not grant consent, expand permissions, bypass native prompts or change existing approval contracts.
- Reuse existing messages, reports, gates and owner-decision records. Add no transport, schema, service or delegation default.
- Installation, deployment and live provider operations are outside this task.

## Discovery

Guidance is emitted by `src/llm_bus/cli/guidance.py` and recovered through selected canonical coordination references. Existing guidance tests cover role selection, versions and hook size; extend them for routing across capability states. Authority implementations remain unchanged.

Implementation starts from main at `4535098e536902f186f828162ab700eee5813c3f` in the owned `feat/coordinator-user-actions` worktree. The primary checkout has unrelated untracked `.vscode/`, which remains untouched.

## Implementation progress

Guidance version 11 and the matching canonical marker now carry coordinator routing for active roles across capability states. Canonical references and user documentation preserve direct consent, machine permissions, native prompts and existing gate/decision acceptance contracts.

Local verification passed: focused guidance coverage (42 tests), unchanged authority coverage (121 tests), and `uv run poe check` (all static checks, 907 tests and isolated package checks). Independent task review is Clean/Approved, planner conformance is CONFORMS, and final local verification is PASS on the same reviewed source. PR publication is in progress; the final PR review and current-head CI gate remain pending. Installation, deployment and live installed-agent validation remain outside this task.

Authoritative implementation, task review, conformance, verification and publication reports are under /Users/davidfava/.local/state/llm-bus/flows/coordinator-user-actions/. No completion summary is written at this milestone.
