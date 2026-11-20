# Project execution consent · Revision 2

## Outcome and scope

- **R1.** Add one default-off approval at the end of coordination setup for project-related paid or live QA runs and development deployments, including automatic deployment from an otherwise authorized merge. Owners can approve before any jobs, budgets, providers or deployment targets are known. Covered actions need no individual approval.
- **R2.** Approval applies only to active, verified members of the exact project and generation. Leaving, removal or revocation ends access. Approval cannot travel with a copied message or be reused in another project.
- **R3.** Production operations, credential or secret changes, force-pushes and merges with failing or pending CI remain outside approval. Native and provider permissions, repository rules and existing merge authorization still apply.
- **R4.** Existing projects default to off. Unknown, malformed, stale or unsupported evidence grants no authority.
- **R5.** The terminal wizard and agent-guided setup expose the same choice and limits.

The proposed first release covers same-machine project enrollment. An eligible member can use remote QA infrastructure. Cross-machine enrollment continues to use its existing permissions. This is a plan for owner review.

## Approach and material contracts

### C1. Store approval in the existing delegation

Extend `Delegation` with optional Boolean `project_execution`, default `false`, item `project-execution` and local CLI flag `--project-execution`. The flag can stand alone. Use the current generation-scoped delegation and per-grant join binding. Encode the field only when true. Reject invalid types and unknown fields. Keep the remote gate cap in `only_gates`.

Direct join accepts the current delegation, subject to `--narrow`. Dropping the flag through the existing project amendment path makes it ineffective immediately. Re-enabling or widening authority follows current join rules. Coordinator replacement starts without delegation. Preserve the existing owner and role rules for amendments and revocation.

### C2. Derive current authority for the caller

Add `project_execution_allowed` to `coordination show`. Use one authority check for this value and guidance. It requires the exact routing scope, project and generation, a live native session, current enrollment and effective consent. Workers require a verified current grant and join binding. Coordinators require current coordinator enrollment. Unsupported remote sessions return false. Reuse lifecycle and grant verification.

Before a covered action, the agent checks fresh status for its own session and project. An unavailable or missing result grants no authority. Messages, directions, copied status and retained delegation records cannot confer approval. The owner can revoke future authorization. External operations already submitted may still run.

### C3. Collect approval in the wizard

After existing setup choices, ask **Preapprove paid or live QA runs and development deployments for this project?** with a default of **No**. The review screen explicitly names both activities, summarizes the selection and states its limits. Store the final reviewed settings atomically after explicit setup confirmation. Cancelling leaves setup unstored.

The agent-guided setup profile offers the same default-off question through the host’s owner question interface. Its review summary names paid or live QA runs and development deployments and states the limits. The example command omits the flag by default. The agent adds it after explicit owner opt-in. The existing recognized-wake check cannot authenticate an owner’s words. Use explicit owner interaction and existing local role restrictions. This feature uses the existing local procedural trust model. External tools retain their own authorization controls.

The action must advance the confirmed project objective. A development deploy triggered by merge carries separate approval requirements for the merge itself. If the effect cannot be identified as development, the agent cannot use this approval for the deployment.

### C4. Compatibility and rollback

Old records load with the option off. A client that cannot interpret the new field can reject enabled delegations. Upgrade participating local clients, hooks and skill guidance before enabling the option. Before downgrading, revoke it using upgraded code. Verify that current status denies and preserve project history. No schema table, relay change or provider integration is required.

## Acceptance criteria

| ID | Requirements | Observable proof |
| --- | --- | --- |
| A1 | R1, R5 | Both setup paths offer one default-off choice explicitly covering paid or live QA runs and development deployments. Both summaries name that scope and its limits. Explicit approval persists it. Blank, negative and cancelled inputs do not enable it. No target or budget inputs appear. |
| A2 | R2, R4 | Current status allows only the exact active coordinator or appropriately bound worker. An unrelated session, repo or project, and leave, revoke, expiry, replacement, inactivity and narrowed consent all deny. Widening respects the direct-join boundary. |
| A3 | R1, R3 | Guidance covers paid or live QA runs and development deployments while requiring a separate valid merge gate, live CI and review checks where applicable, and native and provider permissions. Permanent floor actions stay blocked. |
| A4 | R4 | Legacy records stay off. Malformed fields fail closed. Remote setup and amendment cannot grant this option. Remote caps still discard flags. |
| A5 | R1, R2 | A worker can check current approval without a new owner prompt or any approval message. Copied output and retained history cannot reinstate revoked authority. |

## Tasks and dependencies

Execute sequentially after plan approval. Implementation, parallel work and publication require their applicable authorizations.

### T1. Extend delegation and setup

Produce C1 and C3 for A1 and A4. Own `src/llm_bus/coordination/{delegation,cli,setup}.py` for flag parsing, binding, setup and amendment, plus their focused tests. Keep settings and the initial delegation in one setup transaction. Verify the user can explicitly disable the option after enabling it. Replace the blanket floor descriptions for all paid runs and all deploys while preserving R3 and validation against using them as ordinary `--delegate GATE:BRANCH` specs.

### T2. Derive authority and expose it

Consume C1 and produce C2 and C4 for A2 through A5. Extend `coordination/authority.py`, the coordination CLI `_show` path and `src/llm_bus/cli/guidance.py`. Check the effective approval against fresh local state. Handle approval-only delegation in guidance. Remove outdated blanket denial and conditions that recognize only gates or owner decisions. Preserve ordinary gate verification and remote caps. Add caller and lifecycle cases using existing fixtures.

### T3. Align coordination guidance and verify

Consume C1 through C4. Update the owner and worker guidance, the tutorial, examples and the coordination flow diagram source and published wording where they describe the floor. Cover confirmed project ownership and revocation, prohibited actions, and the same-machine enrollment limit. Keep supported gate checks and the diagram renderer. Extend existing setup, delegation, guidance, lifecycle and remote tests for the new cases. Use fixtures or stubs for any external action examples.

## Verification and rollback

Run focused setup, delegation, guidance, owner-role, lifecycle and remote refusal cases first. Inspect `uv run poe check` prerequisites and run it before any future PR push. Verify reversal via C4. Verification uses local code and test fixtures.

## Author self-review

Revision 2 uses baseline `ebe813a4ed3bdadfebd68e1be80ab0484940e934`. The planner traced setup through `create_for`, join through the delegation binding, amendment through effective delegation, and the authority, guidance and remote consumers. The revised wording aligns C3, A1 and A3 with R1. Cleanup is limited to superseded wording and consent-excluding guidance conditions. Existing authority entry points are still reachable. Review the same-machine scope with the owner during plan approval.
