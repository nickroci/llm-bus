# Maintenance-aware llm-bus upgrade

## Registration

This tracked fallback uses the repository's quick-task format. No GSD tooling wrote it.
Draft: .planning/drafts/maintenance-upgrade/PLAN.md

The owner approved revision 1 for implementation on 2026-11-23. Development
uses an isolated feature worktree and disposable macOS/Linux environments.
The separate production cutover uses the previously verified client release.

The supported path retains the maintenance response across uv replacement
using a stable launcher and private generation directories. It preserves
existing host settings, peer policy, and the installed skill from the accepted
release. Relay shutdown and migration remain under operator control.
