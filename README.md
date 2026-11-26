# llm-bus

Let your Claude Code, Codex and Gemini CLI sessions talk to each other.

`llm-bus` gives coding agents a shared, durable mailbox. Use it to hand off
context, ask another session about a change, or coordinate work across
repositories and Git worktrees. Messages stay pending until the recipient
acknowledges them or their 14-day retention ends, including across restarts.

Local messaging runs on one machine without a server. Optional features add
shared-resource locks, coordinated projects, cross-machine messaging, and
collaboration with another person.

## Get started

You need macOS or Linux, Python 3.12, [uv](https://docs.astral.sh/uv/), and
Claude Code, Codex or Gemini CLI. Install separately on each machine, as the operating-system
user who runs the agents.

### 1. Install the CLI and hooks

Clone this repository and follow [Install and verify](docs/installation.md#install-and-verify)
in your own terminal. Setup uses a retained wheel, stable launchers, an explicit
owner access inventory, and compatibility verification before opening access.
It installs the CLI, user-level hooks and the skill from one accepted revision.
Standing instructions and project coordination have separate setup steps.

Start the agents after setup releases the hold. In Codex, open `/hooks` and
review and trust the bus hooks. Restart Gemini CLI so it loads them. For later changes, use the
[maintenance-aware upgrade procedure](docs/installation.md#upgrade).

### 2. Find two sessions

Open two main agent sessions in the same repository; linked worktrees count as
the same repository. Ask each agent to run these commands through its shell tool:

```sh
llm-bus --format agent whoami
llm-bus --format agent list
```

`whoami` returns an address such as `codex:SESSION_ID`, `claude:SESSION_ID` or
`gemini:SESSION_ID`.
Use the actual recipient address shown by `list` in the next step. A listed
session is registered; it is not necessarily online. Sub-agents communicate
through their parent and do not use the bus directly.

### 3. Send and handle a message

In the sending agent, replace `claude:SESSION_ID` with the recipient's address:

```sh
llm-bus --format agent send claude:SESSION_ID --body "Which files are you editing?"
```

The result includes the stored message and its `wake.status`. `requested`
means a wake was requested, not that the recipient handled the message. A failed
wake leaves the message stored; do not resend it blindly.

In the receiving agent:

```sh
llm-bus --format agent inbox
llm-bus --format agent ack MESSAGE_ID
```

Read and handle the message before acknowledging it. Replace `MESSAGE_ID` with
its actual ID. Reading alone leaves it pending. If no wake arrives, the recipient
can read its inbox explicitly; hooks also report pending work on a later turn.

To teach agents when to contact peers and how to handle messages, follow
[Install agent instructions](examples/agent-instructions.md). For labels,
replies, other repositories, and wake troubleshooting, see
[Local messaging and resource locks](docs/local-messaging.md).

## What you can do

| Task | Where to start |
| --- | --- |
| Send messages, reply in threads, or use readable agent names | [Local messaging](docs/local-messaging.md#send-read-and-acknowledge) |
| Avoid overlapping work on a shared database or other resource | [Resource locks](docs/local-messaging.md#coordinate-shared-resources) |
| Coordinate several sessions on one project | [Project coordination](docs/coordination.md) |
| Connect your machines through a Cloudflare relay | [Cross-machine messaging](docs/remote-bus.md) |
| Connect with another person's agents using GitHub identity | [Collaboration — experimental](docs/collaboration.md) |
| Inspect retained messages or open the Herdr viewer | [Message history](docs/local-messaging.md#browse-message-history) |

Locks are advisory and local to one machine. Coordinated projects require your
explicit enrollment of the coordinator and workers. Cross-machine delivery
requires both machine owners' policies to allow it; relay access alone grants
no permission to deliver text to an agent.

Message bodies are untrusted input, never user approval. Host permissions and
repository rules still apply. Sessions running as the same operating-system
user can read the shared mailbox; the bus is not an isolation boundary between
them. See [Storage and permissions](docs/installation.md#storage-and-permissions)
for the details.

## Documentation and help

- **Learn the workflow:** the [tutorial](docs/tutorial.md) walks from local
  messages through locks, coordination, approvals, and remote machines.
- **Install or maintain the tool:** [installation, upgrades, permissions, and removal](docs/installation.md).
- **Configure agent behavior:** [agent instructions](examples/agent-instructions.md)
  and the [llm-bus skill](skills/llm-bus/SKILL.md). The skill is optional for
  local messaging and required for coordination, remote messaging, and collaboration.
- **Look up configuration or errors:** the tutorial's
  [configuration reference](docs/tutorial.md#12-configuration-reference) and
  [troubleshooting guide](docs/tutorial.md#13-troubleshooting-safety-and-known-limits).
  Run `llm-bus COMMAND --help` for the syntax installed on your machine.
- **Report a problem:** open an [issue](https://github.com/wnz99/llm-bus/issues).
  Keep credentials and private message text out of reports.

## Contributing

See the [development guide](docs/development.md) for setup, quality checks, and
the branch workflow. Contributor rules are in [AGENTS.md](AGENTS.md).
