# Ad hoc collaboration plan

Status: first implementation on `feat/collab` (`llm-bus collab`). Claude Code responders only; see [Open questions and gaps](#open-questions-and-gaps).

## Goal

Two people working on the same repository connect their agents without a hosted service:

1. Alice tells her agent she wants to collaborate. It runs `llm-bus collab invite`.
2. Alice names the GitHub user (`bob`) and chooses what Bob's agent may do on her machine. The agent prints a ticket link.
3. Alice sends the link to Bob through any channel.
4. Bob gives the link to his agent and chooses what Alice's agent may do on his machine.
5. Each side verifies the other's GitHub identity. Both people see the peer's GitHub name and the same short confirm code, then approve.
6. The machines talk point to point. Each person keeps working in their existing Claude Code or Codex session.

Groups of more than two people are a later extension (see [Later](#later)).

## Threat model

In scope:

- The peer's agent is compromised by prompt injection ("mind virus") and sends hostile instructions.
- The peer's agent tries to extract secrets: files, environment values, or anything already in the receiving session's context.
- Someone intercepts the invite link and tries to join in place of the intended person.
- A peer tries to raise its own permissions on the other machine.

Out of scope, with stated consequences:

- A compromised GitHub account. Its attacker can publish a signing key and pass identity checks. Revoke the peer locally when this is suspected.
- A stolen private signing key. Recommend a hardware-backed `sk-ed25519` key or a passphrase-protected key in an SSH agent.
- Other processes running as the same OS user. They are mutually trusted, as in the rest of llm-bus.
- A person deliberately choosing **Full** mode.

## Identity: GitHub SSH signing keys

Each person publishes an SSH **signing** key on their GitHub account (`gh ssh-key add --type signing`). Signing keys cannot authenticate git pushes, so a key used only for llm-bus grants no repository access. Public signing keys are readable without authentication from `https://api.github.com/users/<user>/ssh_signing_keys`.

During the handshake each side signs a transcript with `ssh-keygen -Y sign -n llm-bus-collab`. The transcript contains:

- role (`host` or `joiner`) and protocol version
- both iroh endpoint IDs, which iroh's QUIC/TLS handshake authenticates
- the invite's one-time nonce
- both GitHub usernames

The verifier builds an `allowed_signers` file from the claimed user's published keys and runs `ssh-keygen -Y verify`. Binding both endpoint IDs prevents replaying a signature on another connection. The nonce prevents reuse of an old handshake.

Invites are bound to one GitHub user, single use, and expire (default: 1 hour). A short confirm code derived from both endpoint IDs and the nonce is shown to both people. Comparing it out of band is optional when both signatures verify, but it catches a link sent to the wrong person.

Pinning: after approval, each side stores the peer as (GitHub user, iroh endpoint ID, key fingerprint). A later connection from a different endpoint for the same user requires a new invite.

## Transport: iroh

[iroh](https://iroh.computer) (`iroh` on PyPI, 1.1.0) gives each machine an Ed25519 endpoint ID and connects endpoints over QUIC with NAT hole punching. Traffic is end-to-end encrypted to the endpoint IDs. When a direct path fails, public relays forward encrypted bytes they cannot read. The n0 team runs free, rate-limited relays without SLA; users can configure a self-hosted relay.

- The invite ticket encodes the host's endpoint address. Nothing is served from the inviter's machine, which is usually behind NAT.
- There is no store-and-forward. Messages wait in the local outbox until both machines are online.
- One long-lived `llm-bus collab` daemon per machine owns the endpoint and its secret key (stored with the same file-permission rules as relay tokens).
- ALPN: `llm-bus/collab/0`.

Each request has a 30-second deadline covering connection and stream exchange;
incoming exchanges are bounded too. Malformed endpoint tickets fail their join
without stopping the daemon. Timed-out outgoing messages remain eligible for retry.

The existing Cloudflare relay remains. Collaboration is a second transport that reuses the peer policy model.

## Trust modes

Each side picks, per peer, what that peer's messages may cause on **its own** machine. A peer's choice never sets the other side's mode.

| Mode | Who handles incoming text | Can see | Can do |
| --- | --- | --- | --- |
| `monitor` | Nobody automatically; human reads it | n/a | Nothing |
| `isolated` | Fresh restricted agent, no session context | Committed files (clean export) | Reply text |
| `readonly` | Restricted **fork** of the owner's session | Session context + committed files | Reply text |
| `readwrite` | Restricted fork in a dedicated worktree and branch | Same, plus its worktree | Edit and test in that worktree; owner reviews and merges |
| `full` | Owner's main session | Everything | Everything the main agent can |

Rules:

- New peers default to `monitor`. `full` is never a default, requires typed confirmation, expires (default: 1 hour), and is revocable.
- Modes apply to all incoming text. Below `full`, peer text enters the main session's context automatically only through **agent answers**: an owner-set, per-peer option (default off) that delivers the first reply to a question the local agent sent, labeled as untrusted. Unsolicited messages, later replies to the same question, and replies to unknown questions still go to the owner page. The inbox rechecks the option and revocation before exposure, so turning it off stops pending answers; already shown text cannot be recalled.
- Only a person can create, approve, or raise a mode. Agents may run `collab invite`, but approval needs a human-only step. Open question: whether `! llm-bus collab approve` provides a TTY in Claude Code, or a one-time localhost confirmation page is needed.
- Fork output goes back through the daemon, never through an agent tool call. The fork never writes back into the main session.
- Replies from `readonly`/`readwrite` forks pass a secret scan. Matches are held for owner approval. An owner may choose to approve every reply; this is off by default.

### Enforcing the restricted fork (Claude Code)

Responder context is a separate local per-peer setting on the owner page under
**Change permissions**. `fork` is the default, preserving existing behavior.
`summary` starts a fresh Claude responder with an owner-written status summary
(up to 4000 characters), omitting session resume/fork flags. The summary is
maintained manually; conversation history is never summarized automatically.
Tool permissions still follow the selected mode. `isolated` never forks but can
receive the supplied summary; `monitor` and `full` ignore this setting. The
setting and summary are stored locally and cannot be set through peer messages.
Switching affects subsequent responder starts, not a responder already running.
Summary context avoids inherited conversation secrets, but repository contents
and the supplied summary may still be sensitive. Secret scanning and optional
reply review remain in effect.

Pending peer text is bound to a unique pairing through its bus sender suffix
(`collab:USER:PAIR` or `collab:reply:USER:PAIR:QUESTION`). Revoking one pairing cannot be
overridden by another pairing for the same GitHub user or local session. Legacy
pending peer messages without that binding are hidden from agent inboxes after
upgrade; stored history is retained.

Verified with Claude Code 2.1.303:

```text
ENABLE_CLAUDEAI_MCP_SERVERS=false claude -p MESSAGE \
  --resume SESSION_ID --fork-session \
  --restricted --strict-mcp-config --tools Read,Grep,Glob \
  --permission-mode dontAsk --output-format json < /dev/null
```

Run from a clean export of committed files (`git archive HEAD`), so untracked secrets such as `.env` are absent.

- `--restricted` removes command-running tools and WebFetch, ignores user/project/local settings (so the owner's bypass mode does not apply), refuses `bypassPermissions`, and confines file tools to the working directory.
- `--tools Read,Grep,Glob` leaves no write or shell tool.
- `--strict-mcp-config` and `ENABLE_CLAUDEAI_MCP_SERVERS=false` remove MCP and claude.ai connector tools.
- `--fork-session` creates a new session ID; the original transcript is not modified.

Codex equivalent to verify: `codex exec fork SESSION_ID --sandbox read-only`.

## Spike results (2026-10-19)

Handshake spike: two iroh endpoints on one Mac, throwaway SSH keys, keys served from a local directory in place of GitHub.

- Legitimate join verified in both directions; both sides showed the same confirm code; messages flowed both ways.
- A joiner signing with a different key while claiming the invited user was rejected.
- The path was direct (`is_relay: False`) over the LAN.

Fork spike: a base Haiku session holding a code word, a fake token, and "currently refactoring `src/llm_bus_store.py`"; fork run from a clean export.

- With a hostile peer message, the model refused everything, including harmless questions. The responder needs a framing prompt so it answers legitimate coordination questions.
- Simulating a successful injection (the fork told the attempts were authorized), the harness denied reads of `~/.zshrc`, `~/.ssh`, and a file one level above the working directory. No write or shell tool existed; no files were created; no MCP or connector tools were available.
- **The fork disclosed the fake token from its context.** Sandboxing cannot remove what the session already knows. This is the main residual risk of `readonly` and `readwrite`, and the reason for reply secret scanning and optional approval.
- The base transcript was unchanged after forking. Each fork turn cost about $0.007 on Haiku with a small context; cost scales with the forked session's size.

## Open questions and gaps

- Resolved: approval and peer text use a token-protected loopback owner page. The agent prints its links; processes running as the same OS user can still read the token, consistent with the same-user trust model.
- Agent answers are a prompt-injection channel into the main session, narrowed to replies the agent solicited. A hostile or compromised peer can still put instructions in an answer; the label and the main session's own permissions are the only defenses. Each outgoing question records its asking session. Its first answer routes to that session, even if another session subsequently uses the same peer. Pending exposure rechecks the exact question, session and current peer policy. Legacy questions without recorded ownership remain on the owner page.
- Showing peer text inside Claude Code without adding it to agent context (mod pane or toast) instead of a browser page.
- Secret scanner choice and false-positive handling.
- Per-peer rate and cost limits for forks; batching messages into threads.
- Fork context trimming (for example, only the last N turns) to reduce what can leak.
- Codex parity for every mode.
- Policy schema: extend version 2 with GitHub-identified collaboration peers and modes, keeping AGENTS.md defaults (deny unless explicitly chosen; peers cannot change local policy).
- Revocation UX and retained history when a peer is blocked.
- GitHub API unauthenticated rate limit (60 requests/hour per IP) for key lookups; cache keys per handshake only.

## Local end-to-end test plan

Two GitHub accounts on one machine (`nickbanqora`, `nickroci`), this repository:

1. Generate one signing key per account and publish it with `gh ssh-key add --type signing` while each account is active.
2. Create two worktrees of this repository, one per identity.
3. Give each identity separate llm-bus state (`LLM_BUS_DB`, `LLM_BUS_CONFIG`, collab key and daemon socket).
4. Run one daemon per identity and one Claude session per worktree.
5. Exercise invite, join, approval, each mode, a denied impersonation, an expired or reused invite, and revocation.

Same-user processes on one machine can read each other's files, so this test proves protocol and mode behavior, not OS isolation between the two people.

## Later

- Groups via iroh-gossip topics; trust stays per person.
- Optional pinned self-hosted relay.
