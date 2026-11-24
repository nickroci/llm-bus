# Collaborate with another person (experimental)

`llm-bus collab` connects your agents with another person's agents for one
repository, without a hosted service you must operate. Each person proves a
GitHub identity with an SSH signing key published on their account. Connections
use [iroh](https://iroh.computer): encrypted QUIC with NAT traversal and public
relays forwarding only ciphertext when a direct connection is unavailable.

## Set up a connection

1. Follow [Install and verify](installation.md#install-and-verify), including the
   `collab` extra in the retained wheel installation. Existing managed installs
   preserve their recorded extras on upgrade/reinstall; do not force-install over
   public launchers to change extras. Run `llm-bus collab setup --github-user YOU`
   and follow its `next_steps` to create and publish a signing key.
2. Run `llm-bus collab daemon`, one per identity. It serves your private owner
   page on `127.0.0.1`.
3. Ask your agent to run `llm-bus collab invite`. On the resulting owner page,
   choose the collaborator's GitHub user and permissions, then send them the
   invite text.
4. Their agent runs `llm-bus collab join INVITE`. They choose your permissions
   on their own page. Both sides verify signing keys and show the same confirm code.
5. Agents exchange messages with `llm-bus collab send USER`.

You approve invites, joins, modes, and held replies on your owner page. The
[agent skill reference](../skills/llm-bus/references/collab.md) gives the full
setup sequence; the [collaboration plan](collab-plan.md) documents the threat
model, enforcement details, and known gaps.

## Choose what incoming messages can cause

Each side sets its own mode per collaborator:

| Mode | Local effect |
| --- | --- |
| `monitor` | Only you read incoming messages. |
| `isolated` | A fresh sandboxed agent answers from committed files. |
| `readonly` | A sandboxed fork of your session answers. |
| `readwrite` | The fork can edit a separate review branch. |
| `full` | Peer text reaches your main agent; this permission expires. |

Below `full`, unsolicited peer text never enters the main agent automatically.
It gets a notice; you read or forward the message on your page. To receive an
answer to an agent's question in that session, enable **agent answers** for the
peer. Only the first reply to each sent question is delivered, labeled untrusted.

Under **Change permissions**, **Responder context** selects **Fork** (the
default, copying session history) or **Summary** (a fresh agent with a summary
you supply, up to 4000 characters). Summary needs no bound Claude session but
still uses Claude as responder and preserves tool limits. Update it manually
as work changes. Isolated mode never copies history; monitor and full ignore
this setting.

Forked responders can repeat anything in session context. Summary responders
receive your summary and repository access; include only information the peer
may receive. Replies that look like secrets are held for review, and you can
choose to review every reply.

## Multiple identities on one machine

For your own accounts or testing, use a separate terminal per identity. Set
`GH_CONFIG_DIR` for its own gh login and `LLM_BUS_COLLAB_HOME` for its signing
identity, daemon, and page before starting the agent. Do not use `gh auth switch`;
it changes the account for every terminal. Accounts sharing one macOS user can
read each other's files, so this is not isolation between people.
