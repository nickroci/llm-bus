# Gate approvals (local projects)

Load with `core.md` when gates are in effect for the session: the project
delegation for a coordinator, the worker's binding for a worker. The authority
rules, verification reasons and the never-delegated floor are in `core.md`.

## Claiming and approving

Worker claims its ticket and branch first; one worker holds a ticket or branch
at a time, and re-claiming replaces the worker's own claim:

```sh
llm-bus --format agent coordination claim PROJECT --ticket TICKET --branch BRANCH
```

Coordinator then sends an approval as a structured direction. `--expires-in`
takes `30m` or `2h` style values, default `2h`, at most `24h`:

```sh
llm-bus --format agent coordination approve PROJECT --recipient claude:WORKER_ID --gate commit --ticket TICKET --branch BRANCH --base main
llm-bus --format agent coordination approve PROJECT --recipient claude:WORKER_ID --gate push --ticket TICKET --branch BRANCH --base main --head-sha FULL_SHA
llm-bus --format agent coordination approve PROJECT --recipient claude:WORKER_ID --gate merge --ticket TICKET --branch BRANCH --base main --pr PR_NUMBER --head-sha FULL_SHA
```

`--gate merge-admin` takes the merge flags. `commit` takes no `--head-sha` or
`--pr`; `push` takes `--head-sha` and no `--pr`; merge gates require both.
`--head-sha` is a full 40 or 64 hex character commit ID. `merge-admin` bypasses
only the required-approvals rule; required checks must still pass on the exact
head. The approval is
verified at the worker only when the recipient's effective delegation (project
delegation intersected with the worker's join binding) includes the gate and
the base as a target, the branch is neither the base nor a delegated target
branch, it has not expired, and the worker's claim matches the ticket and
branch. Show the project delegation and each grant's binding with
`llm-bus --format agent coordination show PROJECT`. The coordinator hook
guidance reports how many workers have a narrower binding; `show` does not list
claims or those workers. A rejected claim names the current holder.

## Worker live checks

Once an approval arrives, check the inbox entry is verified for
this project and generation, then check live evidence yourself; the bus checks
none of it, and stricter repository rules win. Refuse and report on any mismatch.

- commit: `git rev-parse --abbrev-ref HEAD` equals the approval branch; commit.
- push: `git rev-parse --abbrev-ref HEAD` equals BRANCH and `git rev-parse HEAD`
  equals `head_sha`; `git push origin BRANCH` (never
  `--force`); then `gh pr create --base BASE --head BRANCH`, or update the
  existing PR.
- merge: `gh pr view PR --json baseRefName,headRefName,headRefOid,state,statusCheckRollup`
  must match base, branch and `head_sha`, with the PR open; `gh pr checks PR
  --required` must show every required check passing; review evidence must have
  no unresolved high or medium findings; then `gh pr merge PR --match-head-commit SHA` with the
  repository's merge-method flag (for example `--merge`). Add `--admin` only for `approve-merge-admin`
  and only when required approvals are the sole blocker.

Approvals never cover the floor items listed in `core.md`. Route those requests
through the coordinator, which asks the appropriate owner to act directly in
the exact owning session or machine. The same route applies when a gate is not
bound. Keep the worker blocked until its existing authorization and live-check
requirements are satisfied; a relayed answer is not a gate approval.
