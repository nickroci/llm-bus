# 261120-pap — Project action preapproval

Both coordination setup paths offer one default-off setting for project-related paid or live QA and development deployments. Current verified membership in the exact project and generation controls access on the same machine. Leaving or revocation ends access. Existing merge checks and provider permissions still apply.

The first independent PR review covered all 25 changed paths on `7d89e3c12fc3872ad297fb838fe73ce705abe995` and found zero issues. That commit passed `uv run --locked poe check` with 995 tests, and both GitHub quality jobs succeeded. The review also passed 197 targeted coordination tests and six remote cases. [PR #92](https://github.com/wnz99/llm-bus/pull/92) · [Review results](https://github.com/wnz99/llm-bus/pull/92#issuecomment-7046118005)

This tracking summary is the final documentation-only change before merge. Its validation and current-head CI are required before merging. Installing the updated CLI, hooks and guidance is a separate rollout.
