from __future__ import annotations

import re
from typing import Literal, NamedTuple, NotRequired, TypedDict

from llm_bus.errors import BusError
from llm_bus.hosts.kinds import HOST_KINDS

SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_MESSAGE_BYTES = 64 * 1024
MAX_BATCH = 100
PENDING_PAGE_ERROR = "Pending page size must be between 1 and 100"
PENDING_COUNT_ERROR = "Pending count limit must be positive"


class Agent(TypedDict):
    """Registered session address."""

    address: str
    kind: str
    project: str
    last_seen: str
    ended_at: str | None
    label: NotRequired[str | None]


class Message(TypedDict):
    """Unacknowledged mailbox item."""

    id: int
    sender: str
    recipient: str
    body: str
    sender_cwd: str | None
    recipient_cwd: str | None
    sent_at: str
    reply_to: int | None
    coordination: NotRequired[dict[str, object]]


class HistoryMessage(Message):
    """Stored message with delivery state for read-only history views."""

    acknowledged_at: str | None
    wake_status: str | None
    wake_via: str | None
    wake_reason: str | None
    wake_checked_at: str | None


class SenderContext(NamedTuple):
    """Invocation identity and folder used together for an atomic send."""

    kind: str
    session_id: str
    project: str


class PendingRow(NamedTuple):
    """One bounded scan row with authenticated remote metadata when available."""

    id: int
    sender: str
    recipient: str
    body: str | None
    sender_cwd: str | None
    recipient_cwd: str | None
    sent_at: str | None
    stored_scope: str | None
    envelope: str | None
    connection: str | None
    reply_to: int | None


def address(kind: str, session_id: str) -> str:
    """Build stable address from host session ID."""
    if kind not in HOST_KINDS:
        raise BusError("Agent kind must be claude, codex, gemini or agy")
    if not SESSION_ID.fullmatch(session_id):
        raise BusError("Host session ID has invalid characters or length")
    return f"{kind}:{session_id}"


def is_local_address(value: str) -> bool:
    """Validate a complete local identity using the registration address contract."""
    kind, _, session_id = value.partition(":")
    try:
        return address(kind, session_id) == value
    except BusError:
        return False


type PendingTuple = tuple[
    int,
    str,
    str,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    int | None,
]


type PendingContent = Literal[
    "NULL,NULL,NULL,NULL", "m.body,m.sender_cwd,m.recipient_cwd,m.sent_at"
]


type HistoryRow = tuple[
    int,
    str,
    str,
    str,
    str | None,
    str | None,
    str,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    int | None,
]


def message_from_row(
    row: tuple[int, str, str, str, str | None, str | None, str], reply_to: int | None
) -> Message:
    """Decode the common stored message projection."""
    return Message(
        id=row[0],
        sender=row[1],
        recipient=row[2],
        body=row[3],
        sender_cwd=row[4],
        recipient_cwd=row[5],
        sent_at=row[6],
        reply_to=reply_to,
    )
