from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import llm_bus.collab.models
from llm_bus.coordination.authority import authorize
from llm_bus.coordination.delivery import persist, require_coordinator
from llm_bus.coordination.stalls import observe_message
from llm_bus.errors import BusError
from llm_bus.hosts.sessions import require_main_agent
from llm_bus.mailbox.models import (
    MAX_BATCH,
    MAX_MESSAGE_BYTES,
    Message,
    message_from_row,
)
from llm_bus.mailbox.retention_schema import allocate_id

if TYPE_CHECKING:
    import sqlite3

    from llm_bus.coordination.models import Approval, Direction


def send_in_transaction(  # noqa: PLR0913, C901  # pylint: disable=too-many-arguments,too-many-locals
    db: sqlite3.Connection,
    sender: str,
    recipient: str,
    body: str,
    *,
    cross_folder: bool = False,
    reply_to: int | None = None,
    direction: Direction | None = None,
    approval: Approval | None = None,
) -> Message:
    """Persist for an already registered sender without changing its activity or lifecycle."""
    if not body.strip():
        raise BusError("Message body cannot be empty")
    require_main_agent(sender)
    require_main_agent(recipient)
    if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise BusError("Message body exceeds 64 KiB")
    sender_row = cast(
        "tuple[str, str] | None",
        db.execute("SELECT project, scope FROM agents WHERE address = ?", (sender,)).fetchone(),
    )
    if sender_row is None:
        raise BusError(f"Unknown sender {sender!r}; register the session first")
    sender_project, sender_scope = sender_row
    recipient_row = cast(
        "tuple[str, str] | None",
        db.execute("SELECT project, scope FROM agents WHERE address = ?", (recipient,)).fetchone(),
    )
    if recipient_row is None or recipient.startswith(llm_bus.collab.models.SENDER_PREFIX):
        raise BusError(f"Unknown recipient {recipient!r}; run `llm-bus list` first")
    if sender_scope != recipient_row[1] and not cross_folder:
        raise BusError("Recipient is in another repository or folder; use --cross-folder")
    validate_reply(db, reply_to, (sender, sender_scope), (recipient, recipient_row[1]))
    if direction is not None:
        require_coordinator(db, sender, sender_scope, direction)
        decision = authorize(
            db,
            sender=sender,
            recipient=recipient,
            scope=recipient_row[1],
            direction=direction,
            now=datetime.now(UTC),
            approval=approval,
        )
        if not decision.verified:
            raise BusError(f"Direction denied: {decision.reason}")
    message_id = allocate_id(db, "message")
    db.execute(
        "INSERT INTO messages(id, sender, recipient, body, sender_cwd, recipient_cwd, "
        "sender_scope, recipient_scope, reply_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            message_id,
            sender,
            recipient,
            body,
            sender_project,
            recipient_row[0],
            sender_scope,
            recipient_row[1],
            reply_to,
        ),
    )
    if direction is not None:
        persist(db, message_id, direction, approval)
    elif reply_to is not None:
        observe_message(db, message_id, now=datetime.now(UTC))
    request_dispatch(db, recipient)
    row = cast(
        "tuple[int, str, str, str, str | None, str | None, str] | None",
        db.execute(
            "SELECT id, sender, recipient, body, sender_cwd, recipient_cwd, sent_at "
            "FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone(),
    )
    if row is None:
        raise RuntimeError("Inserted message could not be read")
    return message_from_row(row, reply_to)


def validate_reply(
    db: sqlite3.Connection,
    parent_id: int | None,
    sender: tuple[str, str],
    recipient: tuple[str, str],
) -> None:
    """Bind a retained parent to the same participants and their individual scopes."""
    if parent_id is None:
        return
    if isinstance(parent_id, bool) or parent_id < 1:
        raise BusError("Reply ID must be a positive integer")
    parent = cast(
        "tuple[str, str, str | None, str | None] | None",
        db.execute(
            "SELECT sender,recipient,sender_scope,recipient_scope FROM messages WHERE id=?",
            (parent_id,),
        ).fetchone(),
    )
    if parent is None:
        raise BusError("Unknown reply parent")
    parent_sender, parent_recipient, parent_sender_scope, parent_recipient_scope = parent
    if sorted((parent_sender, parent_recipient)) != sorted((sender[0], recipient[0])):
        raise BusError("Reply participants do not match parent")
    if (
        parent_sender_scope is None
        or parent_recipient_scope is None
        or sorted(
            ((parent_sender, parent_sender_scope), (parent_recipient, parent_recipient_scope))
        )
        != sorted((sender, recipient))
    ):
        raise BusError("Reply participant scopes do not match parent")


def request_dispatch(db: sqlite3.Connection, recipient: str) -> None:
    """Count a new native inbox member in the same insertion transaction."""
    db.execute(
        "INSERT INTO wake_dispatch(recipient,requested_generation) VALUES (?,1) "
        "ON CONFLICT(recipient) DO UPDATE SET "
        "requested_generation=requested_generation+1,next_attempt_at=NULL",
        (recipient,),
    )


def message_acknowledged(
    db: sqlite3.Connection, recipient: str, message_id: int, sender: str
) -> bool:
    """Verify a wake's full identity against durable acknowledgement state."""
    return (
        db.execute(
            "SELECT 1 FROM messages WHERE id=? AND recipient=? AND sender=? "
            "AND acknowledged_at IS NOT NULL",
            (message_id, recipient, sender),
        ).fetchone()
        is not None
    )


def deliver_collab(db: sqlite3.Connection, sender: str, recipient: str, body: str) -> int:
    """Store daemon-delivered collaboration text for a registered local session.

    The sender row is marked ended so it never appears in `list` and cannot be addressed.
    """
    require_main_agent(recipient)
    if not sender.startswith(llm_bus.collab.models.SENDER_PREFIX):
        raise BusError("Collaboration sender must use the collab: prefix")
    if not body.strip() or len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise BusError("Collaboration message is empty or exceeds 64 KiB")
    recipient_row = cast(
        "tuple[str, str] | None",
        db.execute(
            "SELECT project, scope FROM agents WHERE address = ? AND kind != 'collab'",
            (recipient,),
        ).fetchone(),
    )
    if recipient_row is None:
        raise BusError(f"Unknown recipient {recipient!r}")
    db.execute(
        "INSERT INTO agents(address, kind, project, scope, last_seen, ended_at) "
        "VALUES (?, 'collab', '', 'collab', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
        "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) "
        "ON CONFLICT(address) DO UPDATE SET last_seen = excluded.last_seen",
        (sender,),
    )
    message_id = allocate_id(db, "message")
    db.execute(
        "INSERT INTO messages(id, sender, recipient, body, recipient_cwd, recipient_scope) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (message_id, sender, recipient, body, recipient_row[0], recipient_row[1]),
    )
    request_dispatch(db, recipient)
    return message_id


def acknowledge(db: sqlite3.Connection, recipient: str, ids: list[int]) -> int:
    """Acknowledge only own messages; reject mixed or unknown batches atomically."""
    if not ids or len(ids) > MAX_BATCH or any(isinstance(item, bool) or item < 1 for item in ids):
        raise BusError("Provide 1-100 positive integer message IDs")
    unique_ids = list(dict.fromkeys(ids))
    acknowledged = 0
    for message_id in unique_ids:
        exists = cast(
            "tuple[int] | None",
            db.execute(
                "SELECT 1 FROM messages WHERE recipient = ? AND id = ?",
                (recipient, message_id),
            ).fetchone(),
        )
        if exists is None:
            raise BusError("One or more message IDs do not belong to this agent")
        cursor = db.execute(
            "UPDATE messages SET acknowledged_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE recipient = ? AND acknowledged_at IS NULL AND id = ?",
            (recipient, message_id),
        )
        acknowledged += cursor.rowcount
        if cursor.rowcount:
            observe_message(db, message_id, now=datetime.now(UTC))
    return acknowledged
