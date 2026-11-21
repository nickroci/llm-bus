# 261121-unc — Unattended coordination

## Registration

This record uses the personal feature workflow's tracking-record fallback. No GSD tooling wrote or validated it. The approved private plan is .planning/drafts/unattended-coordination/PLAN.md in the primary worktree. The owner approved independently reviewed revision 2, whose SHA-256 is `7205727c1ed092e191b96764d518a8b85d0f68a090038f2d61601bc2c0a6adee`.

The integration branch already tracks `.planning/STATE.md` and `.planning/quick/`, so this record is tracked in the dedicated `feat/unattended-coordination` worktree. Implementation has not started.

## Decisions

- Offer unattended coordination first during setup while preserving individual permission controls. Allow later owner activation after open points are resolved.
- Bind unattended authority to the confirmed objective, run, generation, finite ticket scope and expiry. Preserve machine-local trust, worker consent, provider permissions and repository merge rules.
- Keep production changes, force-push and merges with pending or failing CI outside unattended authority.
- Require a separate owner-signed receipt for each exact development secret operation. The signer and development target policy must remain outside agents' writable authority.
- Carry the verified five-minute setup-default patch from `fix/decision-default-five-minutes` into T1. Preserve explicit configured durations.
- Execute T1–T5 sequentially. This registration authorizes no live operation or installed rollout.

## Discovery summary

Reuse the terminal and agent setup paths, delegation and mailbox state, decisions, record-bound gates, claims, durable inbox, wakes and stall notices. Stall notices indicate silence; they do not prove safe handoff. Existing detached SSH signature mechanics provide an implementation reference, but collaboration key discovery does not establish owner consent for a secret change.

Acceptance covers setup and lifetime, caller-bound decisions, exact secret receipts, recovery and handoff, and packaging through public commands with isolated fixtures. Record unavailable host or live-provider proof explicitly. The approved plan contains the complete requirements, contracts and verification scope.
