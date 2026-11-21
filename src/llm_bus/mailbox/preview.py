"""Body-free, read-only retention inventory for offline cutovers."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from llm_bus.errors import BusError
from llm_bus.mailbox.retention import RETENTION_DAYS
from llm_bus.mailbox.retention_schema import normalize_age

READ_TRANSACTION = "BEGIN"

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


def _candidates(db: sqlite3.Connection, query: str, cutoff: str) -> dict[str, object]:
    count = 0
    oldest: str | None = None
    for row in cast("Iterable[tuple[str]]", db.execute(query)):
        age = normalize_age(row[0])
        if age <= cutoff:
            count += 1
            oldest = age if oldest is None else min(oldest, age)
    return {"candidates": count, "oldest_eligible_at": oldest}


def preview(path: Path, *, now: datetime | None = None) -> dict[str, object]:
    """Validate original timestamps on legacy and current schemas without mutation.

    Project only age metadata, never bodies or envelopes. Stream the full age inventory
    because invalid legacy timestamps must block activation even outside the backlog.
    """
    instant = datetime.now(UTC) if now is None else now
    cutoff = normalize_age((instant - timedelta(days=RETENTION_DAYS)).isoformat())
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute(READ_TRANSACTION)
            messages = _candidates(db, "SELECT sent_at FROM messages", cutoff)
            monitor_exists = cast(
                "tuple[int] | None",
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='remote_monitor'"
                ).fetchone(),
            )
            monitors = (
                _candidates(
                    db, "SELECT json_extract(envelope,'$.sent_at') FROM remote_monitor", cutoff
                )
                if monitor_exists
                else {"candidates": 0, "oldest_eligible_at": None}
            )
    except sqlite3.Error:
        raise BusError(
            "Cannot preview mailbox retention; check database and timestamp metadata"
        ) from None
    return {
        "as_of": normalize_age(instant.isoformat()),
        "retention_cutoff": cutoff,
        "messages": messages,
        "monitors": monitors,
    }
