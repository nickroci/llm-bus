"""Content-free availability bindings survive deletion of their notice envelopes."""

from __future__ import annotations

import json
from datetime import UTC
from typing import TYPE_CHECKING, cast

from llm_bus.coordination.models import timestamp
from llm_bus.errors import BusError

if TYPE_CHECKING:
    from sqlite3 import Connection


def migrate(db: Connection) -> None:
    """Detach availability identity from content without changing authority or targets."""
    columns = _columns(db, "coordination_availability_events")
    if "project_id" not in columns:
        rows = cast(
            "list[tuple[int, str, str, str, str, str, str, str]]",
            db.execute(
                "SELECT a.message_id,a.grant_id,a.sender,a.sent_at,a.event_id,"
                "m.recipient,m.recipient_scope,r.envelope FROM coordination_availability_events a "
                "LEFT JOIN messages m ON m.id=a.message_id "
                "LEFT JOIN remote_imports r ON r.local_id=a.message_id"
            ).fetchall(),
        )
        db.execute(
            "CREATE TABLE coordination_availability_bindings (message_id INTEGER PRIMARY KEY, "
            "grant_id TEXT NOT NULL,sender TEXT NOT NULL,sent_at TEXT NOT NULL,"
            "event_id TEXT NOT NULL,"
            "recipient TEXT NOT NULL,scope TEXT NOT NULL,project_id TEXT NOT NULL,"
            "generation TEXT NOT NULL,UNIQUE(sender,grant_id))"
        )
        for row in rows:
            try:
                envelope = cast("dict[str, object]", json.loads(row[7]))
                event = cast("dict[str, object]", envelope["availability"])
                project_id, generation = event["project_id"], event["generation"]
                if (
                    not all(
                        isinstance(value, str) and value
                        for value in (row[5], row[6], project_id, generation)
                    )
                    or event["grant_id"] != row[1]
                ):
                    raise ValueError("Missing binding")  # noqa: TRY301 - one body-free diagnostic
                age = timestamp(row[3]).astimezone(UTC).isoformat(timespec="microseconds")
            except (ValueError, TypeError, KeyError):
                raise BusError("Invalid availability binding; migration stopped") from None
            db.execute(
                "INSERT INTO coordination_availability_bindings VALUES (?,?,?,?,?,?,?,?,?)",
                (*row[:3], age, row[4], row[5], row[6], project_id, generation),
            )
        db.execute("DROP TABLE coordination_availability_events")
        db.execute(
            "ALTER TABLE coordination_availability_bindings "
            "RENAME TO coordination_availability_events"
        )
    columns = _columns(db, "coordination_local_availability")
    if "remote_id" not in columns:
        db.execute(
            "CREATE TABLE coordination_local_availability_bindings (grant_id TEXT PRIMARY KEY "
            "REFERENCES coordination_grants(grant_id),message_id INTEGER NOT NULL UNIQUE,"
            "remote_id TEXT)"
        )
        db.execute(
            "INSERT INTO coordination_local_availability_bindings "
            "SELECT a.grant_id,a.message_id,o.id FROM coordination_local_availability a "
            "LEFT JOIN remote_outbox o ON o.local_id=a.message_id"
        )
        db.execute("DROP TABLE coordination_local_availability")
        db.execute(
            "ALTER TABLE coordination_local_availability_bindings "
            "RENAME TO coordination_local_availability"
        )
    db.execute(
        "CREATE INDEX IF NOT EXISTS coordination_availability_order "
        "ON coordination_availability_events(sender,recipient,scope,project_id,generation,"
        "sent_at,event_id)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS mailbox_maintenance "
        "(id INTEGER PRIMARY KEY CHECK(id=1),next_at TEXT NOT NULL)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS remote_sync_work_key ON remote_sync_work(key)")
    db.execute(
        "CREATE INDEX IF NOT EXISTS coordination_stall_notice "
        "ON coordination_stall_state(notice_message_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS coordination_stall_binding "
        "ON coordination_stall_state(scope,project_id,generation)"
    )


def _columns(db: Connection, table: str) -> set[str]:
    rows = cast(
        "list[tuple[int, str, str, int, object, int]]",
        db.execute(f"PRAGMA table_info({table})").fetchall(),
    )
    return {row[1] for row in rows}
