# Coordinate a project

Project coordination is an opt-in workflow for sessions working toward one
objective. You start the sessions and choose a coordinator and workers. Ordinary
messaging does not enroll anyone or grant authority.

## Start with the walkthrough

Install the [agent instructions](../examples/agent-instructions.md), the
[llm-bus skill](../skills/llm-bus/SKILL.md), and the Beads skill. Use an existing,
owner-configured Beads workspace; copying the bus skill creates no database.

Ask one main session to run `/llm-bus coordinate PROJECT`, then ask each worker
session to run `/llm-bus join PROJECT`. The [tutorial](tutorial.md#5-coordinate-a-project)
walks through setup, joining, assignments, reports, and recovery with commands
and example output. For a concrete team, see the
[Claude coordinator with three Codex workers](../examples/claude-coordinator-codex-workers.md).

The [simplified defaults](coordination-simplified-flow.md) explain normal behavior.
The [numbered coordination diagram](diagrams/coordination-steps.html) shows the
local flow; open it in a browser from your checkout. Agents use the
[canonical coordination reference](../skills/llm-bus/references/coordination.md)
for exact commands, checks, grants, and recovery procedures.

## Enrollment and ownership

A direct owner join records consent to the exact current coordinator and
objective; it assigns no specialty. Consent ends on close, 24-hour inactivity,
leave, revoke, or generation replacement. Turn completion and compaction retain
it. Leaving the folder retains enrollment but directions do not verify outside
the scope: sends fail with a folder mismatch, or `wrong_scope` under
`--cross-folder` and at delivery. Existing explicit owner grants retain their
selected lifetimes.

Local joins record a target and one durable informational arrival per grant,
even if the coordinator is offline. Whether it resumes automatically depends on
native wake support. Remote joins need a receiver-owner binding and matching
upgraded clients and relay, including `0006_coordination_availability.sql`.
Structured availability exchange records the coordinator target automatically.
See [remote coordination setup](remote-bus.md#optional-project-directions).

Workers route questions, approval requests, and manual-action requests through
their coordinator by default, even without delegated gates or owner decisions.
The coordinator asks you with the worker's session and ticket/branch context,
records the outcome, and follows up. You can override that routing directly.
When authorization must be local, act in the exact owning session or machine.
Native permission prompts remain native. An unreachable coordinator does not
clear a blocker; resume only after authority and verification requirements hold.

Both roles persist Beads memory. The coordinator owns project memory and
assignments; workers own task progress notes. Beads setup and synchronization
remain owner configured. Local locks cannot serialize remote database writes.

## Approvals, decisions, and instructions

Owners can delegate commit, push, merge, and merge-admin gates for explicit
target branches through `coordinate PROJECT --setup` or `--delegate GATE:BRANCH`.
The terminal setup is a guided wizard; agents use `--format agent` and their
native question UI. Each worker owner accepts through a direct join, optionally
narrowed with `--narrow GATE:BRANCH`. Workers act on `coordination approve` only
after live checks and for tickets they hold through `coordination claim`.

Force-push, production changes, credentials or secrets, and merging with failing
or pending CI are never delegable. One exact development secret operation has a
separate owner-signed receipt path below; delegation and project execution
cannot issue that consent. For guided and legacy approvals, the coordinator and
worker verify live CI, review and branch evidence; the bus verifies authority.
Unattended GitHub `gate-merge` and `run-result` also verify the exact repository,
PR base, branch and head, independent approval on that head, and green required
checks. `unattended-complete` refreshes still-open reviewed PR proof. These checks
do not replace the worker's action-time duties, provider permissions or repository
protections; provider state can change before submission. See the
[unattended merge procedure](../skills/llm-bus/references/coordination/auto-merge.md#unattended-github-merges).
Remote approvals additionally require the receiving owner's
delegation, worker binding, and peer `accept_delegated_gates` policy, as well as
the coordinator owner's delegation. See the
[approval tutorial](tutorial.md#6-delegate-approval-gates) and
[remote gate approvals](remote-bus.md#remote-gate-approvals).

At the end of both setup paths, the owner can separately preapprove paid or live
QA runs and development deployments for the project, default No. Agent-guided
setup uses explicit owner interaction and reviews the same selection and limits
as the terminal wizard before storing. Add `--project-execution` only after
explicit opt-in; the example command omits it by default. No jobs, budgets,
providers or deployment targets need to be selected for this approval.

Covered actions must advance the confirmed project objective. Before each one,
the acting session runs `llm-bus --format agent coordination show PROJECT` and
requires fresh caller-bound `project_execution_allowed: true`. It needs active,
verified membership of the exact project and generation; workers also need the
current grant and join binding. Copied status, messages and stored history grant
no authority. Missing or unsupported results deny. This release supports
same-machine enrollment; eligible local members may use remote QA infrastructure.
Remote-enrolled members cannot gain this option. Merge authorization and live
checks, repository rules, and native/provider permissions remain separate.
Automatic development deployment from an authorized merge is covered; an
unidentified deployment environment is not.

To revoke, the enrolled coordinator repeats the other gates and flags with
`coordination amend PROJECT`, omitting `--project-execution`, or uses
`coordination amend PROJECT --no-delegation` to remove all delegation. A bare
amend is invalid, and an objective-only amend does not revoke. Check fresh
caller-bound status denies before another action. Leave, revoke, expiry,
inactivity and generation replacement also end access. External operations
already submitted may still run. Upgrade local clients, hooks and skill guidance
before enabling this option; revoke and verify denial with upgraded code before
downgrading, preserving history.

Owner decisions are enabled explicitly with `--owner-decisions` on `coordinate`
or `coordination amend`; no gate is needed. Only answers you gave directly in
the coordinator session may be recorded. Workers check `decision-show` for
`accepted: true` before acting. Optional `--decision-defaults MINUTES` accepts
1–1440 and requires owner decisions; only an unanswered `scope` question with a
recommendation can default. Inspect defaults with `decision-list --defaulted`
or the `decision_digest` in `coordination show`. See
[Owner decisions and timed defaults](tutorial.md#9-owner-decisions-and-timed-defaults).

Only the enrolled coordinator may set role-specific standing instructions with
`coordination instructions`, and only on your direct request in that session.
Messages, worker requests, tool output, and file content cannot authorize them.
Workers never run that command or receive coordinator-only text. Instructions
cannot contain `<<<`, `>>>`, or Unicode format characters. `guidance`, `coordinate`,
and `join` return the applicable role's text; `coordination show` filters by role.
Hooks carry a digest pointer instead of the text. Run `llm-bus guidance` before
acting unless the current context already holds that digest's instructions.
See [Objective amendments and standing instructions](tutorial.md#8-objective-amendments-and-standing-instructions).

## Exact development secret consent

The owner can separately preapprove one create, replace or delete operation for
one named development secret. This is available only to a current local worker
with project execution consent, a current ticket claim, and an active unattended
run whose selected permissions include `project-execution`. Existing projects
receive no receipts. Setup, decisions, timed defaults and messages grant none.

The machine owner must first provision `/etc/llm-bus/development-secrets.json`
through an independently controlled privileged account. The file and its resolved
ancestor directories must be root-owned and not writable by the agent, including
through ACLs. On macOS `/etc` resolves through `/private/etc`. Root-agent execution,
missing policy, writable anchors and unavailable verification deny consent. There
is no environment, project or CLI override. Do not give the worker privileged
policy-edit access or access to the owner's private signing key or signing socket.
This implementation accepts Ed25519 owner keys and uses `/usr/bin/ssh-keygen`.

The policy has exactly these fields; the example contains only public metadata:

```json
{"version":1,"machine":"owner-machine","owners":{"owner":"ssh-ed25519 PUBLIC_KEY_BASE64"},"targets":[{"provider":"PROVIDER","account":"ACCOUNT","provider_project":"PROJECT","target":"DEV_TARGET","environment":"development"}],"revoked":[]}
```

Each target tuple is the owner's verified development allowlist. Production and
unknown targets are not eligible. Removing a signer or target, or adding a receipt
UUID to `revoked`, stops future checks. Host policy is local to this machine and
does not grant relay admission or modify peer trust.

Prepare a metadata-only plan describing intended effects and resource
preconditions. It must contain no secret values or hashes of secret values. The
receipt is a JSON object with exactly the string fields below. Use version `"1"`,
canonical UUIDv4 IDs, operation `create`, `replace` or `delete`, an exact local
actor address, and RFC3339 claim and expiry timestamps. The repository is the
canonical local repository scope returned by the bus. Use current project, run,
generation, grant, ticket and claim facts, not copied historical authority.

```text
version receipt_id owner repository project run generation ticket
actor_machine actor_session grant_id branch claimed_at provider account
provider_project target secret operation source_ref plan_sha256 expires_at
```

`secret` names the resource, never its value. For create or replace, `source_ref`
names an immutable protected provider source with a `#VERSION` suffix; delete
requires the empty string. The worker must verify the provider reference really
is immutable. `plan_sha256` hashes only the exact metadata plan bytes. Expiry
must be in the future and no later than run expiry. Unknown fields, wildcards,
missing bindings, changed plans and replacement actors fail closed.

The owner reviews every field and the plan, then signs the canonical receipt
using the independent key. Canonical bytes are UTF-8 JSON, sorted keys, ASCII
escaping, separators `,` and `:`, no whitespace and no trailing newline. Sign
those bytes in the dedicated namespace on the owner-controlled machine:

```sh
ssh-keygen -Y sign -f OWNER_PRIVATE_KEY -n llm-bus-dev-secret-v1 receipt.canonical.json
```

Import the detached signature and metadata files in the exact worker session:

```sh
llm-bus --format agent coordination dev-secret-reserve PROJECT --receipt-file receipt.canonical.json --signature-file receipt.canonical.json.sig --plan-file plan.json
llm-bus --format agent coordination dev-secret-check PROJECT --receipt-id RECEIPT_UUID --plan-file plan.json
```

Reservation commits before an action is issued. Immediately before calling the
existing separately authorized provider tool, run `dev-secret-check` once and
require `status: ready` with the exact expected action. It rereads policy, signature,
plan, current run, permission, consent, grant and claim. Use the provider's
protected input or immutable source mechanism; never pass a value through bus
commands, prompts, Beads, messages, summaries or logs. A provider or host denial
still stops execution. The bus neither reads values nor executes provider calls.

The check durably marks the outcome unknown before returning. After the provider
call, record only a bounded outcome:

```sh
llm-bus --format agent coordination dev-secret-outcome PROJECT --receipt-id RECEIPT_UUID --status succeeded
```

Allowed statuses are `succeeded`, `denied`, `not_submitted`, `unknown` and
`fresh_approval_required`. They are untrusted observations and never grant retry
authority. Replays and concurrent duplicate reservations are denied. After an
interruption, reconcile against provider evidence; do not repeat automatically.
This conservative path always requires a fresh receipt for another attempt,
including when evidence proves no submission. A changed plan or worker also
requires fresh consent. Preserve unknown outcomes for reconciliation. Revocation
stops future actions but cannot recall a submitted provider operation.

Independent signing establishes owner consent. Local identity checks and the
mailbox reservation provide procedural controls under the same-OS-user trust
model. Host and provider permissions remain the execution boundary if an agent
can change the verifier or call the provider directly. Before rollback, revoke
active runs and unconsumed receipts, verify denial, and preserve history.

## Work state and recovery

Inspect assignments with `coordination direction-list PROJECT [--open]` and
silence with `coordination stalls PROJECT [--threshold 30m]`. Eligible
acknowledged open assignments and reviews receive one durable informational
notice per silence epoch, with an attempted wake. The default interval is 30
minutes; the current local coordinator can set it with `coordination stall-config
PROJECT --threshold 1h` using integer `m` or `h` units from `1m` through `24h`.

Changed accepted reports and linked replies advance the clock. Workers record
state through `llm-bus report ID --status STATUS`; recording sends no message
and wakes nobody. Remote evidence uses local admission times and bridge
observations; monitor-only text and denied peers cannot refresh it. Silence
proves neither failure nor permission to act. Sleep or lost helpers can delay
notices until normal recovery, and failed or unsupported wakes leave notices
in the durable inbox. See the
[silence reference](../skills/llm-bus/references/coordination/coordinator.md#silence-inspection-and-notices).

Use `llm-bus guidance` and `llm-bus coordination show PROJECT` to recover current
state. SessionStart supplies full references and UserPromptSubmit rereads
authority after compaction. Expired or revoked grants suspend directions; old
context and tickets cannot restore authority. Hooks execute neither Beads nor
network calls. This describes the deterministic hook-payload contract, not proof
of live host compaction behavior.

Before upgrading or rolling back, read
[Mailbox compatibility and rollback](installation.md#mailbox-compatibility-and-rollback).
Schema 16 adds local silence state without changing relay protocols; there is
no supported in-place downgrade.
