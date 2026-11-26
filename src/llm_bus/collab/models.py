"""Collaboration models ownership."""

from __future__ import annotations

import re
from typing import NamedTuple, TypedDict

from llm_bus.hosts.kinds import KIND_PATTERN

HOME_ENV = "LLM_BUS_COLLAB_HOME"
MODES = ("monitor", "isolated", "readonly", "readwrite", "full")
RESPONDER_MODES = frozenset({"isolated", "readonly", "readwrite"})
DEFAULT_MODE = "monitor"
FULL_MODE = "full"
INVITE_MINUTES = 60
FULL_MAX_MINUTES = 24 * 60
MAX_BODY_BYTES = 64 * 1024
MAX_INVITE_CHARS = 4096
MAX_REPOSITORY_NAME = 200
AUTO_REPLIES_PER_HOUR = 20
INVITE_PREFIX = "llmbus-invite-v0:"
NOTICE_SENDER = "collab:_daemon"
FORWARD_SENDER = "collab:_owner"
REPLY_SENDER_PREFIX = "collab:reply:"
SENDER_PREFIX = "collab:"
WINDOWS_DATA_ENV = "LOCALAPPDATA"
JOIN_AWAITING = "awaiting_owner"
JOIN_APPROVED = "approved"
JOIN_CONNECTED = "connected"
JOIN_FAILED = "failed"
MESSAGE_FAILED = "failed"
MESSAGE_CANCELLED = "cancelled"
MESSAGE_SENT = "sent"
MESSAGE_REJECTED = "rejected"
GITHUB_USER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
SESSION = re.compile(rf"^(?:{KIND_PATTERN}):[A-Za-z0-9_-]{{1,128}}$")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
MAX_STATUS_SUMMARY = 4000


class CollabError(ValueError):
    """Invalid collaboration input or state."""


class Invite(TypedDict):
    v: int
    ticket: str
    nonce: str
    host_user: str
    joiner_user: str
    repository_name: str
    grant: str
    expires_at: str


class Peer(TypedDict):
    responder_context: str
    status_summary: str
    id: int
    pair: str
    github_user: str
    repository: str
    endpoint_id: str
    ticket: str | None
    mode: str
    full_until: str | None
    review_replies: bool
    agent_replies: bool
    wake_on_answer: bool
    their_grant: str | None
    session: str | None
    confirm_code: str
    created_at: str
    revoked_at: str | None


class Message(TypedDict):
    id: int
    peer_id: int
    direction: str
    kind: str
    uid: str
    in_reply_to: str | None
    body: str
    state: str
    detail: str | None
    created_at: str


class Grant(NamedTuple):
    """One owner's choice of what a peer's messages may cause locally."""

    mode: str
    full_until: str | None
    review_replies: bool
    agent_replies: bool = False
    wake_on_answer: bool = False


class ApprovedJoin(NamedTuple):
    """A join the owner approved; the daemon performs its handshake."""

    id: int
    invite: str
    repository: str
    session: str | None
    choice: Grant


class NewMessage(NamedTuple):
    """Fields for one stored collaboration message."""

    direction: str
    kind: str
    uid: str
    body: str
    state: str
    in_reply_to: str | None = None
    detail: str | None = None
    origin_session: str | None = None


class NewPeer(NamedTuple):
    """Verified peer facts recorded after a successful handshake."""

    pair: str
    github_user: str
    repository: str
    endpoint_id: str
    ticket: str | None
    key_fingerprint: str
    their_grant: str
    session: str | None
    confirm_code: str


PEER_COLUMNS = (
    "id, pair, github_user, repository, endpoint_id, ticket, mode, full_until, review_replies, "
    "their_grant, session, confirm_code, created_at, revoked_at, agent_replies, wake_on_answer, "
    "responder_context, status_summary"
)
MESSAGE_COLUMNS = "id, peer_id, direction, kind, uid, in_reply_to, body, state, detail, created_at"
type PeerRow = tuple[
    int,
    str,
    str,
    str,
    str,
    str | None,
    str,
    str | None,
    int,
    str | None,
    str | None,
    str,
    str,
    str | None,
    int,
    int,
    str,
    str,
]
type InviteRow = tuple[str, str, str | None, int, str, str | None, str, str | None, int, int]
type MessageRow = tuple[int, int, str, str, str, str | None, str, str, str | None, str]
