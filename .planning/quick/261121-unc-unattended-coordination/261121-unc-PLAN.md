<!-- Tracking-record fallback registration: no GSD tooling wrote this record.
Approved private source: .planning/drafts/unattended-coordination/PLAN.md in the primary worktree.
Revision: 2. Approved source SHA-256: 7205727c1ed092e191b96764d518a8b85d0f68a090038f2d61601bc2c0a6adee.
The approved plan body below is reproduced verbatim. -->

# unattended-coordination — Unattended coordination

## Requirements and decisions

**R1.** Put Unattended first in terminal and agent-guided setup. The guided path keeps individual permission choices. Unattended preselects the eligible permissions for the chosen scope and shows the owner the final selection before activation. The owner can activate the same mode later for an existing project.

**R2.** Let the coordinator advance the approved objective with the confirmed permissions until completion, expiry, or revocation. Preserve worker consent, machine-local trust, host and provider permissions, and repository merge rules. Production changes, force-push, and merges with pending or failing CI remain outside unattended authority.

**R3.** Record routine decisions made by the coordinator during unattended runs, including wake turns. Preserve applicable ticket decisions when a verified replacement takes ownership.

**R4.** Recover safe-to-retry work, continue independent work alongside blockers, and provide a durable outcome summary.

**R5.** Support separate owner preapproval of one exact development secret change. The owner must sign each approval. Unattended mode, project execution, coordinator decisions, timed defaults, and ordinary delegations grant no secret authority. Keep secret values out of prompts, bus records, Beads, command arguments, summaries, and logs.

## Acceptance criteria

**A1.** Each wizard offers Unattended first and writes settings after confirmation. Existing projects start in guided mode. Later activation previews scope, expiry, and each permission before applying the selection.

**A2.** Expiry, revocation, completion, and changes to the approved objective or coordinator generation stop new unattended authority and invalidate outstanding run-bound actions. The wrong caller, worker, ticket, repository, or PR head cannot use that authority. Independent manual permissions follow their existing rules.

**A3.** A coordinator resolves an eligible routine choice on a wake with explicit coordinator provenance. An authorized replacement can use the applicable ticket decision after verified handoff. The old worker and unrelated callers cannot use the decision. Existing owner-answer and timed-default flows remain available.

**A4.** An unattended run completes a routine ticket through independent review and green required CI on the same head without a new owner prompt. The scenario exercises a restart, safe worker replacement, a recoverable failure, and progress while an independent ticket remains blocked. The outcome summary includes results, PRs, head SHAs, checks, decisions, handoffs, blockers, and why the run ended.

**A5.** A valid owner-signed receipt permits one exact operation against a verified development target. Reject missing or invalid signatures, an unauthorized signer, changed fields or plan SHA, expired or revoked authority, production or unknown targets, stale claims, replacement actors, replay, and concurrent duplicate consumption. A restart after reservation does not automatically repeat the operation. Existing provider and host authorization can still deny it. Verify that output and retained records contain only approved metadata and status.

## Exact development secret approval

**C1.** A receipt authorizes one create, replace, or delete operation against one named developer secret resource. Its signed, versioned canonical payload includes a unique receipt ID, owner identity, repository, project, run, generation, ticket, authenticated actor machine and session, current claim, exact provider account and project, development target, secret identifier, operation, immutable source reference when needed, plan SHA-256, and expiry no later than run expiry. The plan contains intended effects and resource preconditions. Exclude secret values and hashes of secret values. Reject unknown fields, wildcards, missing bindings, and unverified development targets.

The owner reviews and signs this payload using an independently controlled key. Import a detached SSH signature under a dedicated `llm-bus-dev-secret-v1` namespace. Obtain the owner public key and allowed development targets from owner-controlled host policy outside the agent's writable authority. An agent-controlled signing socket, a project setting, an environment override, or an editable local pin cannot establish owner consent. Keep the path unavailable without independent signer and target configuration. An owner-controlled machine can sign without exposing the key to workers.

At consumption, verify the signature, owner policy, payload, plan digest, active run, permission, claim, target, expiry, and revocation. Atomically reserve the receipt before issuing its record-bound action. The bound worker verifies it again immediately before using its existing provider tool. Use the provider's protected input or secret-reference mechanism for the value. Store only receipt metadata, the signature, the reservation, and bounded outcome status.

Reconcile an interrupted or unknown outcome against provider evidence. Retry only if the provider can guarantee idempotency for that exact operation and receipt, or evidence proves submission never occurred. Otherwise, record the blocker and require fresh approval. Worker replacement and plan changes require fresh receipts. Revocation stops future actions. A submitted provider operation may finish after revocation.

The signature establishes consent when the owner independently controls the signer and trust anchor. Actor IDs, run checks, reservations, and the local verifier provide procedural controls for work through the bus. Host and provider permissions enforce execution access if an agent can modify the verifier or call the provider directly.

## Tasks and dependencies

Execute the tasks in order in a dedicated feature worktree after plan approval. Existing project permissions and provider permissions continue to apply.

### T1. Setup and run lifetime

Reuse `coordination/setup.py`, `coordination/cli.py`, and the existing delegation and mailbox store. Both wizard entry points and later activation use the same permission and scope validation. The owner confirms the objective revision, finite ticket set, completion condition, deadline, and permission selection. Require explicit owner action to activate, widen, or renew a run. Retain prior runs as history and expose caller-bound active status through `coordination show`.

Bind run state to the exact project, generation, coordinator, and approved objective. End authority on completion, expiry, revocation, or invalidation by a changed objective or generation. Follow the existing direct-owner interaction contract and deny recognized wakes for activation or widening. Require the owner-operated terminal flow where the host cannot establish the expected context. Treat turn origin as a procedural check under the same-OS-user trust model. Start with local run authority and preserve existing remote capabilities and receiver policy. Verify A1 and the run lifecycle in A2.

### T2. Decisions and gate enforcement

Extend the existing decision, approval, gate-record, and direction paths. Distinguish coordinator answers, owner answers, and timed defaults. Bind new coordinator decisions to the active run, ticket, verified claim, actor, and rationale or evidence. Allow routine scope choices within approved acceptance criteria. Objective expansion, security, cost, data, irreversible actions, provider authorization, and repository-mandated human decisions require their existing authority.

Record run and ticket references transactionally when issuing unattended directions and approvals. Verify current scope, permission, grant, claim, and run at action time, including after waits. Enforce current independent review and green required CI for the exact open PR, base, and head before merging. Use the provider's head-matching merge condition where available. Missing, pending, failed, stale, or unknown checks deny merge. Keep the existing force-push and production restrictions. Credential and secret changes require direct owner action except through C1. A submitted external operation can finish after revocation.

Reuse `decision-show`, current gate verification, and the existing direction path. An authorized worker waiting for a timed default needs the effective deadline and wake behavior without exposing the decision to unrelated callers. Reuse legacy readers and wire parsing. Verify wrong-party and stale-action denial, provenance, revocation after issuance, and the CI conditions in A2–A3.

### T3. Development secret preapproval

Use T1's run binding and T2's action-time checks to implement R5, C1, and A5 in the existing approval, gate-record, direction, and mailbox paths. Add the dedicated receipt format, detached signature verification, and atomic reservation and outcome state. Reuse the `ssh-keygen -Y` mechanics demonstrated in `collab/identity.py` with the dedicated consent namespace and trust source in C1. Preserve collaboration behavior and existing broad credential-delegation rejection.

Present exact preapproval separately from the wizard's unattended permission selections. Existing projects receive no receipts on migration. Update the blanket wording in setup output, coordination references, and user docs to describe the exact exception. Verify both the exception and generic delegation denial.

### T4. Recovery, handoff, and outcome summary

Reuse Beads, claims, direction state, stall notices, the durable inbox, wakes, and guidance. Reconcile checkpointed state against live evidence on activation, wake, and resume. Retry eligible failures within a bounded policy, checking for existing side effects before replay. Continue independent ready tasks while other work waits.

Verify that the prior worker stopped or accepted cancellation before transferring ownership. Reconcile worktree and PR evidence and use an atomic current-claim check to avoid duplicate ownership. Require an enrolled and verified replacement. Transfer only applicable decisions with a reference to original provenance and current ticket ownership. Require fresh gates for the replacement. Treat silence as a signal to investigate. Persist a blocker if safe handoff cannot be verified.

Keep the latest progress and final result available to the owner from project status or resume. Include the evidence in A4. Reuse supported host wakes. A sleeping machine or unavailable host delays work until a supported entry point runs. A coordinator generation change requires fresh authority. Verify A3–A4 with duplicate and reordered wakes, stale worker claims, and an interruption. Update the documented setup prompt order and blocker procedures where the new behavior supersedes them. The terminal wizard and agent profile both have live callers, and the legacy decision and stall paths still have consumers.

### T5. Acceptance and packaging

Run A1–A5 through public commands, installed guidance, and a supported wake route. Use isolated mailboxes, worktrees, and deterministic provider fixtures for failure, recovery, and secret changes. Verify required CI and review refer to the same head. A supported native wake smoke test covers the path without a user prompt. Record any unavailable host or live-provider proof. Clean up owned disposable resources.

## Verification and rollback

Add an upgrade test from mailbox schema 16 with unattended mode initially off. Verify cancellation, later activation, expiry and revocation, local and remote authority boundaries, pre-deadline worker status, claim transfer, and preservation of manual paths. Run focused setup, decision, delegation, work-state, stall, and remote authority tests. Run `uv run poe check` before pushing.

Extend existing approval, gate-record, delegation, and setup tests for A5. Cover byte-level signature binding, protected trust source, untrusted key overrides, concurrent reservation, and crash recovery. Use sentinel values to check that prompts, bus records, errors, and summaries exclude secret values.

Upgrade the executable, hooks, and guidance together. Before rollback, revoke active unattended authority and unconsumed receipts, verify denial, and follow the supported mailbox migration and backup procedure. Preserve history and unknown outcomes for reconciliation.

## Source evidence

- Both setup paths pass through `cli/app.py` and `coordination/setup.py`. Keep the user-facing setup profile and terminal wizard aligned.
- `coordination/decisions.py` checks the worker grant and binding without checking the ticket claim. Verify the claim for new unattended decisions.
- `coordination/cli.py` rejects overlapping current claims. Coordinator replacement moves to a new generation and revokes prior grants.
- `coordination/gate_records.py` uses record-bound approvals. The coordination gate reference already requires live merge verification.
- `coordination/stalls.py` produces durable informational notices and attempts a wake. Recovery uses these as evidence alongside current host and claim state.
- `collab/identity.py` already demonstrates detached SSH signatures. Collaboration key discovery, local key generation, and test key overrides do not establish owner consent for a dev secret change.
- Existing credential restrictions appear in `coordination/delegation.py`, setup output, and the coordination and owner-decision guides. Preserve broad delegation rejection when describing the single-operation exception.

The planner checked these paths at `bef31ec`. No preexisting code removal was confirmed.
