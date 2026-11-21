"""Bounded, atomic deletion of main-mailbox content and its dependent copies."""

# SQL identifiers below are fixed internal schema names, never caller input.
# ruff: noqa: S608

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from llm_bus.errors import BusError
from llm_bus.mailbox.retention_schema import normalize_age

if TYPE_CHECKING:
    from sqlite3 import Connection

RETENTION_DAYS = 14
BATCH_SIZE = 100
MAINTENANCE_SECONDS = 60


def _select(db: Connection, table: str, key: str, cutoff: str, limit: int) -> list[int]:
    # Missing normalized ages sort first in the age index and must never be skipped.
    if db.execute(f"SELECT 1 FROM {table} WHERE retention_at IS NULL LIMIT 1").fetchone():
        raise BusError("Invalid message retention timestamp; maintenance stopped")
    rows = cast(
        "list[tuple[int, str]]",
        db.execute(
            f"SELECT {key},retention_at FROM {table} WHERE retention_at<=? "
            f"ORDER BY retention_at,{key} LIMIT ?",
            (cutoff, limit),
        ).fetchall(),
    )
    for _, age in rows:
        if normalize_age(age) != age:
            raise BusError("Invalid message retention timestamp; maintenance stopped")
    return [row[0] for row in rows]


def _messages(db: Connection, ids: list[int]) -> None:
    if not ids:
        return
    marks = ",".join("?" for _ in ids)
    bindings = cast(
        "list[tuple[str, str, str]]",
        db.execute(
            "SELECT DISTINCT scope,project_id,generation FROM coordination_stall_state "
            f"WHERE message_id IN ({marks})",
            ids,
        ).fetchall(),
    )
    for table in ("remote_imports", "remote_outbox"):
        db.execute(
            f"INSERT OR IGNORE INTO remote_replay_guard SELECT id FROM {table} "
            f"WHERE local_id IN ({marks})",
            ids,
        )
    remote_ids = (
        f"SELECT id FROM remote_imports WHERE local_id IN ({marks}) UNION "
        f"SELECT id FROM remote_outbox WHERE local_id IN ({marks})"
    )
    db.execute(f"DELETE FROM remote_sync_work WHERE key IN ({remote_ids})", ids + ids)
    for table in ("remote_report_submissions", "remote_reports", "remote_report_windows"):
        db.execute(f"DELETE FROM {table} WHERE id IN ({remote_ids})", ids + ids)
    db.execute(f"UPDATE messages SET reply_to=NULL WHERE reply_to IN ({marks})", ids)
    db.execute(
        "UPDATE coordination_stall_state SET notice_message_id=NULL "
        f"WHERE notice_message_id IN ({marks})",
        ids,
    )
    for table in (
        "wake_coverage",
        "coordination_messages",
        "coordination_approvals",
        "coordination_work_reports",
        "coordination_stall_state",
    ):
        db.execute(f"DELETE FROM {table} WHERE message_id IN ({marks})", ids)
    # Clear stale leases when their entire pending membership is removed.
    db.execute(
        "UPDATE wake_dispatch SET token=NULL,lease_until=NULL,next_attempt_at=NULL,attempts=0,"
        "dispatched_generation=requested_generation "
        f"WHERE recipient IN (SELECT recipient FROM messages WHERE id IN ({marks})) "
        "AND NOT EXISTS(SELECT 1 FROM messages m WHERE m.recipient=wake_dispatch.recipient "
        f"AND m.acknowledged_at IS NULL AND m.id NOT IN ({marks}))",
        ids + ids,
    )
    for table in ("remote_imports", "remote_outbox"):
        db.execute(f"DELETE FROM {table} WHERE local_id IN ({marks})", ids)
    db.execute(f"DELETE FROM messages WHERE id IN ({marks})", ids)
    # Existing timer invocations recheck their token and current directions before effects.
    for binding in bindings:
        db.execute(
            "DELETE FROM coordination_stall_timers WHERE scope=? AND project_id=? AND generation=? "
            "AND NOT EXISTS(SELECT 1 FROM coordination_stall_state "
            "WHERE scope=? AND project_id=? AND generation=?)",
            (*binding, *binding),
        )


def purge(db: Connection, *, now: datetime, limit: int = BATCH_SIZE) -> dict[str, int]:
    """Delete at most limit messages and limit monitors inside the caller's transaction.

    Each class gets its own bounded share, so a message backlog cannot starve monitors.
    Migrations never call this function. Replay IDs and durable authority remain intact.
    """
    if not db.in_transaction:
        raise ValueError("Retention purge requires a write transaction")
    if not 1 <= limit <= BATCH_SIZE:
        raise ValueError("Invalid retention batch size")
    cutoff = normalize_age((now - timedelta(days=RETENTION_DAYS)).isoformat())
    messages = _select(db, "messages", "id", cutoff, limit)
    monitors = _select(db, "remote_monitor", "monitor_id", cutoff, limit)
    _messages(db, messages)
    if monitors:
        marks = ",".join("?" for _ in monitors)
        db.execute(
            "INSERT OR IGNORE INTO remote_replay_guard SELECT id FROM remote_monitor "
            f"WHERE monitor_id IN ({marks})",
            monitors,
        )
        db.execute(f"DELETE FROM remote_monitor WHERE monitor_id IN ({marks})", monitors)
    return {"messages": len(messages), "monitors": len(monitors)}


def maintain(db: Connection, *, now: datetime | None = None) -> None:
    """Rate-limit across processes, using the writable caller's existing connection.

    Finish maintenance before yielding that connection so existing callers can retain
    their own explicit BEGIN IMMEDIATE boundaries. Read-only connections never call here.
    """
    moment = datetime.now(UTC) if now is None else now
    at = normalize_age(moment.isoformat())
    due = cast(
        "tuple[str] | None",
        db.execute("SELECT next_at FROM mailbox_maintenance WHERE id=1").fetchone(),
    )
    if due is not None and due[0] > at:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        due = cast(
            "tuple[str] | None",
            db.execute("SELECT next_at FROM mailbox_maintenance WHERE id=1").fetchone(),
        )
        if due is None or due[0] <= at:
            purge(db, now=moment)
            db.execute(
                "INSERT INTO mailbox_maintenance VALUES (1,?) "
                "ON CONFLICT(id) DO UPDATE SET next_at=excluded.next_at",
                (normalize_age((moment + timedelta(seconds=MAINTENANCE_SECONDS)).isoformat()),),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise
