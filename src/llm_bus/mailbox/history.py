from __future__ import annotations

from typing import TYPE_CHECKING, cast

from llm_bus.coordination.delivery import verification
from llm_bus.errors import BusError
from llm_bus.mailbox.eligibility import cutoff
from llm_bus.mailbox.models import (
    MAX_BATCH,
    HistoryMessage,
    message_from_row,
)
from llm_bus.routing import routing_scope

if TYPE_CHECKING:
    import sqlite3

    from llm_bus.mailbox.models import HistoryRow


def history(  # noqa: C901, PLR0913, PLR0912 - independent public history filters compose here
    db: sqlite3.Connection,
    *,
    folder: str | None,
    repository: str | None = None,
    before: int | None = None,
    after: int | None = None,
    limit: int = MAX_BATCH,
    conversation: int | None = None,
    peer: str | None = None,
) -> list[HistoryMessage]:
    """Read stored messages, newest page first or new items after a cursor."""
    # Optional public filters preserve the existing keyword API and compose one SQL query.
    # pylint: disable=too-many-arguments,too-many-locals,too-many-branches
    if not 1 <= limit <= MAX_BATCH:
        raise BusError("History limit must be between 1 and 100")
    if (before is not None and before < 1) or (after is not None and after < 0):
        raise BusError("History cursors must be nonnegative message IDs")
    if before is not None and after is not None:
        raise BusError("Use either --before or --after, not both")
    if folder is not None and repository is not None:
        raise BusError("Use either folder or repository scope, not both")
    retained_after = cutoff()
    clauses: list[str] = ["retention_at>?"]
    parameters: list[str | int] = [retained_after]
    if peer is not None:
        clauses.append("(sender = ? OR recipient = ?)")
        parameters.extend((peer, peer))
    prefix = ""
    if conversation is not None:
        if isinstance(conversation, bool) or conversation < 1:
            raise BusError("Conversation ID must be a positive integer")
        clauses.append("id IN (SELECT id FROM conversation)")
    if folder is not None:
        clauses.append("(sender_cwd = ? OR recipient_cwd = ?)")
        parameters.extend((folder, folder))
    if repository is not None:
        scope = routing_scope(repository)
        clauses.append(
            "(sender_scope = ? OR recipient_scope = ? OR "
            "(sender_scope IS NULL AND sender_cwd = ?) OR "
            "(recipient_scope IS NULL AND recipient_cwd = ?))"
        )
        parameters.extend((scope, scope, repository, repository))
    if before is not None:
        clauses.append("id < ?")
        parameters.append(before)
    if after is not None:
        clauses.append("id > ?")
        parameters.append(after)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    order = "ASC" if after is not None else "DESC"
    parameters.append(limit)
    if conversation is not None:
        root = _conversation_root(db, conversation, retained_after)
        prefix = (
            "WITH RECURSIVE conversation(id) AS (SELECT ? UNION "
            "SELECT m.id FROM messages m JOIN conversation c ON m.reply_to=c.id) "
        )
        parameters.insert(0, root)
    rows = cast(
        "list[HistoryRow]",
        db.execute(
            prefix + "SELECT id,sender,recipient,body,"  # noqa: S608
            "sender_cwd,recipient_cwd,sent_at,acknowledged_at,"
            "wake_status,wake_via,wake_reason,"
            f"wake_checked_at, reply_to FROM messages{where} ORDER BY id {order} LIMIT ?",
            parameters,
        ).fetchall(),
    )
    messages = [
        HistoryMessage(
            **message_from_row(row[:7], row[12]),
            acknowledged_at=row[7],
            wake_status=row[8],
            wake_via=row[9],
            wake_reason=row[10],
            wake_checked_at=row[11],
        )
        for row in rows
    ]
    for message in messages:
        authority = verification(db, message["id"])
        if authority is not None:
            message["coordination"] = authority
    return messages


def _conversation_root(db: sqlite3.Connection, message_id: int, retained_after: str) -> int:
    seen: set[int] = set()
    while True:
        if message_id in seen:
            raise BusError("Corrupt conversation cycle")
        seen.add(message_id)
        row = cast(
            "tuple[int | None] | None",
            db.execute(
                "SELECT CASE WHEN EXISTS(SELECT 1 FROM messages p WHERE p.id=m.reply_to "
                "AND p.retention_at>?) THEN reply_to END FROM messages m "
                "WHERE id=? AND retention_at>?",
                (retained_after, message_id, retained_after),
            ).fetchone(),
        )
        if row is None:
            raise BusError("Unknown conversation message")
        if row[0] is None:
            return message_id
        message_id = row[0]
