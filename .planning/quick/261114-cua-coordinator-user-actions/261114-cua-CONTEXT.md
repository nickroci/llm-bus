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

Local verification passed: focused guidance coverage (42 tests), unchanged authority coverage (121 tests), and `uv run poe check` (all static checks, 907 tests and isolated package checks). Independent task review is Clean/Approved, planner conformance is CONFORMS, and final local verification is PASS on the same reviewed source. PR #89 aggregate review R1 is Clean/Approved on `ac8c5cb7c54d99110906f2afc926101d1b00d752`; both required CI checks passed on that head. The premerge summary records these results. Fresh validation of the tracking-only delta, current-head CI and merge remain pending. Installation, deployment and live installed-agent validation remain outside this task.

Authoritative implementation, task review, conformance, verification, publication and PR review reports are under /Users/davidfava/.local/state/llm-bus/flows/coordinator-user-actions/. See `261114-cua-SUMMARY.md` for delivered behavior, verification and pending work. The completed-task table records the premerge delivery milestone; it does not claim the merge or final flow reconciliation happened.
