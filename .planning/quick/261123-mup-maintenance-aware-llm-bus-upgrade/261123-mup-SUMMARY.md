# Maintenance-aware llm-bus upgrade

The stable launcher holds managed commands while the operator verifies stopped
mailbox access. Upgrade and reinstall create a private uv generation, verify its
artifact, integrations, and mailbox compatibility, then publish it. Operations
can recover after interruption. Uninstall removes managed integration and
preserves mailbox data and policy. [PR #101](https://github.com/wnz99/llm-bus/pull/101)

## Verification

- On `fc3ea851dcaba8bf87a2abc84c8f83de3967356b`, `uv run poe check` passed
  all static checks, 1,246 tests, and the installed-package smoke check.
- Independent review round 2 passed with zero findings on that revision. Both
  GitHub quality jobs passed on the same revision. [Quality run](https://github.com/wnz99/llm-bus/actions/runs/45309401500)
- Disposable Linux lifecycle checks used real uv and SQLite. On hosts where
  native `lsof` reported incomplete visibility, the tests substituted handle
  evidence. The production controller keeps the hold when visibility is incomplete.

The final tracking revision still needs current-head CI and merge. Production
rollout requires separate authorization.
