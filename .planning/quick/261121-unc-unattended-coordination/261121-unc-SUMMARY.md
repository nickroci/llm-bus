# 261121-unc — Unattended coordination

## Results

Unattended mode is the first setup choice. Owners can still select individual permissions and can activate an expiring, scoped run after setup. New setup offers a five-minute default for eligible unanswered scope decisions.

The coordinator can record routine decisions under the run, recover from interruptions, transfer current work and applicable decisions to a verified replacement, and leave a durable result. A run can finish with an independently reviewed PR and green required checks or with a verified merge. A separate signed receipt can approve one exact development secret operation.

## Verification

- `uv run --no-sync poe check` passed static checks, 1,118 tests, and the isolated installed-package smoke.
- The offline lifecycle scenario covers a wake callback, restart, retry, worker replacement, blocked and ready tickets, and an exact-head result.
- Independent PR review R2 approved commit `2319b238c201c1dca67bfb44a3135c07990e6bfd`. Repository and relay checks passed on that head. See [PR #93](https://github.com/wnz99/llm-bus/pull/93).

Independent tracking validation confirmed that the final commit changed tracking prose only. Repository and relay CI passed on final head `735f64ebf1f3b80e74a16463bb33193306b95a83`. PR #93 merged into `main` as `f2273f38a9b221f1c73d950cbed8b1bded5f2a5e`.

## Rollout

All three configured hosts, one Mac and two Linux systems, installed the merge commit. Each host verified 124 installed Python modules against that source, mailbox integrity on schema 18, the matching copied skill, six hook registrations, the new coordination commands, and a running bridge. Compatible schema 16 mailbox and package backups were retained on each host.

Acceptance used synthetic host callbacks and provider fixtures. Native frontends load their installed hooks after restart. Exact development secret operations require separately provisioned owner-protected policy and provider authorization.
