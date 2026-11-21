from __future__ import annotations

import json
from contextlib import closing
from typing import TYPE_CHECKING, cast

import llm_bus.collab.exposure
import llm_bus.collab.models
import llm_bus.policy.fields
import llm_bus.policy.inbound
from llm_bus.coordination.delivery import exposure_allowed, verification
from llm_bus.errors import BusError
from llm_bus.hosts.sessions import is_subagent
from llm_bus.mailbox.eligibility import cutoff
from llm_bus.mailbox.models import (
    MAX_BATCH,
    PENDING_COUNT_ERROR,
    PENDING_PAGE_ERROR,
    Message,
    PendingContent,
    PendingRow,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Generator

    from llm_bus.mailbox.models import PendingTuple
    from llm_bus.policy.inbound import MessageIdentity


def pending_members(db: sqlite3.Connection, recipient: str) -> list[tuple[int, str]]:
    """Read permitted pending identities across bounded pages, without message bodies."""
    members: list[tuple[int, str]] = []
    with closing(_pending_pages(recipient, db)) as pages:
        for page in pages:
            members.extend((row.id, row.sender) for row in page if _pending_permitted(row, db))
    return members


def _pending_permitted(row: PendingRow, db: sqlite3.Connection) -> bool:
    return exposure_allowed(db, row.id) and _transport_permitted(row, db)


def _transport_permitted(row: PendingRow, db: sqlite3.Connection) -> bool:
    if row.sender.startswith(llm_bus.collab.models.SENDER_PREFIX):
        return llm_bus.collab.exposure.exposure_allowed(row.sender, row.recipient)
    if "/" not in row.sender:
        return True
    # Registration can move between rows already fetched in the same page.
    recipient = cast(
        "tuple[str | None] | None",
        db.execute("SELECT scope FROM agents WHERE address=?", (row.recipient,)).fetchone(),
    )
    if (
        row.envelope is None
        or row.connection is None
        or row.stored_scope is None
        or recipient is None
        or recipient[0] is None
    ):
        return False
    try:
        value = cast("object", json.loads(row.envelope))
        data = llm_bus.policy.fields.object_fields(value)
        if any(not isinstance(data.get(key), str) for key in ("sender", "recipient", "repo_id")):
            return False
        envelope = cast("MessageIdentity", cast("object", data))
        return (
            row.sender == envelope["sender"]
            and row.recipient == envelope["recipient"].rsplit("/", 1)[-1]
            and llm_bus.policy.inbound.permits_inbox(
                envelope, row.stored_scope, recipient[0], row.connection
            )
        )
    except (ValueError, KeyError, TypeError):
        return False


def _pending_query(content: PendingContent) -> str:
    return (
        "SELECT m.id,m.sender,m.recipient,"  # noqa: S608 - projection is internal SQL only
        + content
        + ",m.recipient_scope,CASE WHEN json_valid(r.envelope) THEN "
        "json_object('sender',json_extract(r.envelope,'$.sender'),"
        "'recipient',json_extract(r.envelope,'$.recipient'),"
        "'repo_id',json_extract(r.envelope,'$.repo_id')) END,b.connection,m.reply_to "
        "FROM messages m JOIN agents a ON a.address=m.recipient "
        "LEFT JOIN remote_imports r ON r.local_id=m.id AND "
        "(SELECT COUNT(*) FROM remote_imports i WHERE i.local_id=m.id)=1 "
        "LEFT JOIN remote_binding b ON b.id=1 "
    )


def _pending_pages(
    recipient: str,
    db: sqlite3.Connection,
    *,
    page_size: int = MAX_BATCH,
    content: PendingContent = "NULL,NULL,NULL,NULL",
) -> Generator[list[PendingRow]]:
    """Bound each read and restart from the oldest pending row on every invocation."""
    if not 1 <= page_size <= MAX_BATCH:
        raise BusError(PENDING_PAGE_ERROR)
    if is_subagent(recipient):
        return
    after = 0
    retained_after = cutoff()
    while True:
        rows = [
            PendingRow(*row)
            for row in cast(
                "list[PendingTuple]",
                db.execute(
                    _pending_query(content) + "WHERE m.recipient=? AND m.acknowledged_at IS NULL "
                    "AND m.retention_at>? AND m.id>? ORDER BY m.id LIMIT ?",
                    (recipient, retained_after, after, page_size),
                ).fetchall(),
            )
        ]
        if not rows:
            return
        yield rows
        after = rows[-1].id


def pending_allowed(db: sqlite3.Connection, message_id: int) -> bool:
    """Recheck remote pending exposure against fresh machine-owned policy."""
    result = cast(
        "PendingTuple | None",
        db.execute(
            _pending_query("NULL,NULL,NULL,NULL") + "WHERE m.id=? AND m.retention_at>?",
            (message_id, cutoff()),
        ).fetchone(),
    )
    return (
        result is not None
        and not is_subagent(result[2])
        and _pending_permitted(PendingRow(*result), db)
    )


def inbox(db: sqlite3.Connection, recipient: str, limit: int = MAX_BATCH) -> list[Message]:
    """Read pending messages without consuming them."""
    if not 1 <= limit <= MAX_BATCH:
        raise BusError("Inbox limit must be between 1 and 100")
    messages: list[Message] = []
    with closing(
        _pending_pages(recipient, db, content="m.body,m.sender_cwd,m.recipient_cwd,m.sent_at")
    ) as pages:
        for page in pages:
            for row in page:
                if _pending_permitted(row, db):
                    message = Message(
                        id=row.id,
                        sender=row.sender,
                        recipient=row.recipient,
                        body=cast("str", row.body),
                        sender_cwd=row.sender_cwd,
                        recipient_cwd=row.recipient_cwd,
                        sent_at=cast("str", row.sent_at),
                        reply_to=row.reply_to,
                    )
                    authority = verification(db, row.id)
                    if authority is not None:
                        if authority["verified"] is not True:
                            continue
                        message["coordination"] = authority
                    messages.append(message)
                    if len(messages) == limit:
                        return messages
    return messages


def pending_count(db: sqlite3.Connection, recipient: str, *, limit: int | None = None) -> int:
    """Count permitted pending metadata, optionally stopping at a saturation limit."""
    if limit is not None and limit < 1:
        raise BusError(PENDING_COUNT_ERROR)
    count = 0
    with closing(_pending_pages(recipient, db)) as pages:
        for page in pages:
            for row in page:
                if _pending_permitted(row, db):
                    count += 1
                    if count == limit:
                        return count
    return count
