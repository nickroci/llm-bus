"""Retention predicates shared by exposure and synchronization boundaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from llm_bus.mailbox.retention_schema import normalize_age

if TYPE_CHECKING:
    from sqlite3 import Connection

DELIVERY_HOURS = 48


def cutoff(now: datetime | None = None, *, hours: int = 14 * 24) -> str:
    return normalize_age(((now or datetime.now(UTC)) - timedelta(hours=hours)).isoformat())


def remote_retained(db: Connection, key: str, *, now: datetime | None = None) -> bool:
    """Check original storage, never a cached envelope or a synchronization row."""
    retained_after = cutoff(now)
    return (
        db.execute(
            "SELECT 1 FROM messages m WHERE m.retention_at>? AND m.id IN "
            "(SELECT local_id FROM remote_outbox WHERE id=? UNION ALL "
            "SELECT local_id FROM remote_imports WHERE id=?) UNION ALL "
            "SELECT 1 FROM remote_monitor WHERE id=? AND retention_at>? LIMIT 1",
            (retained_after, key, key, key, retained_after),
        ).fetchone()
        is not None
    )


def report_open(db: Connection, key: str, *, now: datetime | None = None) -> bool:
    instant = now or datetime.now(UTC)
    return (
        db.execute(
            "SELECT 1 FROM messages m LEFT JOIN remote_report_windows w ON w.id=? "
            "WHERE m.retention_at>? AND m.id IN "
            "(SELECT local_id FROM remote_outbox WHERE id=? UNION ALL "
            "SELECT local_id FROM remote_imports WHERE id=?) "
            "AND (w.expires_at IS NULL OR julianday(w.expires_at)>julianday(?)) "
            "AND (w.retention_expires_at IS NULL OR "
            "julianday(w.retention_expires_at)>julianday(?))",
            (key, cutoff(instant), key, key, instant.isoformat(), instant.isoformat()),
        ).fetchone()
        is not None
    )
