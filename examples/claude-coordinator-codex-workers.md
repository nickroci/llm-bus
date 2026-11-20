# Example: Claude coordinator with three Codex workers

**Status: implemented in this source version; requires matching installed CLI, hooks and skills.**
See [simplified defaults](../docs/coordination-simplified-flow.md) and the
[canonical workflow](../skills/llm-bus/references/coordination.md) for advanced setup.
No installation or live activation follows from this example.

## Before joining

Start one Claude Code main session and three Codex main sessions in the same
repository manually. Native subagents do not join the bus. Install llm-bus,
host hooks, the shared llm-bus and official Beads skills, and the default and
coordination rule files first, following
[Install agent instructions](agent-instructions.md). An existing,
readable Beads workspace and project memory must be available. Workers use their
own worktrees under the repository workflow.

Assume the owner has already discussed the search objective with Claude:
repository search with a CLI command, documentation and meaningful tests.
Claude discovers that objective and existing memory, asking only for materially
missing facts. These prompts do not authorize tool installation or Beads setup.

## 1. Start Claude as coordinator

Paste into Claude:

```text
/llm-bus coordinate search-feature
```

The role guidance makes Claude the coordinator, ready to receive workers as
they arrive. It waits for relevant events and yields between them. No worker
addresses or coordinator-role paragraph are needed. Waiting does not mean an
always-running service or an infinite tool polling loop.

## 2. Join each Codex worker

Once the project is ready, paste this into each of the three Codex main sessions:

```text
/llm-bus join search-feature
```

This direct owner request records consent for that exact
session under the current coordinator and objective. Grant, enrollment and
target setup happen automatically. Worker guidance tells Codex to verify current
authority, wait for verified assignments, use its own worktree, persist progress,
start accepted unblocked work, record state changes with `llm-bus report`, and
message the coordinator only for blockers and outcomes it must act on or learn.
No extra authorization paragraph or grant-ID
exchange is required.

Each successful join notifies Claude of that worker's availability. Claude
resolves task boundaries, dependencies and ownership before dispatch, following
repository approval gates. For example, it could split search implementation,
CLI integration and documentation across workers, with tests owned alongside
the relevant changes. The actual split depends on repository evidence.

## 3. Give project direction to Claude

Normal owner direction can refine the work:

```text
Prioritize the CLI experience. Have workers report blockers and test evidence.
Do not merge until I approve.
```

Claude persists assignments in Beads and sends structured directions. Workers
act only on current receiver-verified directions within the project scope.
Claude checks results before marking tasks complete. A join notification,
delivery, wake or acknowledgement proves neither continued availability nor
completion.

## Optional: delegate commit, push and merge

If the owner wants workers to commit, push and merge without asking each time,
Claude runs `llm-bus --format agent coordinate search-feature --setup` and asks
the owner, through its native question UI, which gates and which integration
branch to delegate; it never adds a gate the owner did not confirm. Each Codex
worker joins as above (the owner may narrow a worker with
`--narrow GATE:BRANCH`), claims its task with
`llm-bus coordination claim search-feature --ticket TICKET --branch BRANCH`, and
waits for `llm-bus coordination approve` directions. Workers verify branch,
head SHA, required checks and review findings live before each action. Force-push,
production changes, credential or secret changes and merge with failing or
pending CI stay with the owner. In this example "Do not merge until I approve"
simply means the owner does not delegate the merge gate.

Both setup paths also offer paid or live QA runs and development deployments,
default No. After explicit owner opt-in and review of the scope and limits,
Claude adds `--project-execution`. Covered actions must advance the confirmed
project objective. Each acting session checks its own fresh `coordination show
search-feature` for `project_execution_allowed: true` before each action. Active,
verified same-machine membership and a current worker binding are required;
copied status and messages confer no approval. Eligible members may use remote
QA infrastructure, but remote enrollment cannot gain this option. Merge approval,
live CI/review checks, repository rules and native/provider permissions still
apply. Automatic development deploys from authorized merges are covered only
when the environment is identifiable as development. To revoke, Claude repeats
the retained gates and flags in `coordination amend search-feature`, omitting
`--project-execution`, or uses `--no-delegation` to remove all delegation. Fresh
status must deny; submitted external operations may still run.

## Stop, recover or use remote workers

Consent survives turns and compaction. It ends on session close or expiry,
leave, revoke, or coordinator-generation replacement. A resumed session whose
consent ended requires a fresh owner-directed join; old chat and tickets cannot
restore authority. Stopping or replacing a worker also requires reconciling its
active task and uncommitted work before reassignment.

Remote workers require receiver-owner approval of the project binding and
existing relay/peer policy first. Simplified join must not establish trust in
an unknown remote coordinator. See the
[defaults and trust boundaries](../docs/coordination-simplified-flow.md) and
[current recovery and replacement procedure](../skills/llm-bus/references/coordination/recovery.md#revocation-and-replacement).
