"""Additive retention storage, replay guards, and transactional ID allocation."""

# ruff: noqa: S608

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

from llm_bus.errors import BusError

if TYPE_CHECKING:
    from sqlite3 import Connection


def normalize_age(value: str) -> str:
    """Validate stored ages before indexed comparisons; never include content in errors."""
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            raise ValueError("Missing timezone")  # noqa: TRY301 - one body-free diagnostic
        return instant.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError):
        raise BusError("Invalid message retention timestamp; maintenance stopped") from None


def allocate_id(db: Connection, kind: Literal["message", "monitor"]) -> int:
    """Allocate inside the caller's write transaction, including after an empty mailbox."""
    row = cast(
        "tuple[int]",
        db.execute(
            "UPDATE mailbox_id_high_water SET value=value+1 WHERE kind=? RETURNING value",
            (kind,),
        ).fetchone(),
    )
    return row[0]


def _seed_ids(db: Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS mailbox_id_high_water "
        "(kind TEXT PRIMARY KEY, value INTEGER NOT NULL)"
    )
    # Include durable provider references which deliberately have no message foreign key.
    message_sources = (
        ("messages", "id"),
        ("messages", "reply_to"),
        ("remote_outbox", "local_id"),
        ("remote_imports", "local_id"),
        ("wake_coverage", "message_id"),
        ("coordination_messages", "message_id"),
        ("coordination_local_availability", "message_id"),
        ("coordination_availability_events", "message_id"),
        ("coordination_approvals", "message_id"),
        ("coordination_work_reports", "message_id"),
        ("coordination_stall_state", "message_id"),
        ("coordination_stall_state", "notice_message_id"),
        ("coordination_run_merges", "message_id"),
    )
    for kind, sources in (
        ("message", message_sources),
        ("monitor", (("remote_monitor", "monitor_id"),)),
    ):
        high = 0
        for table, column in sources:
            columns = cast(
                "list[tuple[int, str, str, int, object, int]]",
                db.execute(f"PRAGMA table_info({table})").fetchall(),
            )
            if column in {row[1] for row in columns}:
                row = cast(
                    "tuple[int]",
                    db.execute(f"SELECT COALESCE(MAX({column}),0) FROM {table}").fetchone(),
                )
                high = max(high, row[0])
        db.execute(
            "INSERT INTO mailbox_id_high_water VALUES (?,?) ON CONFLICT(kind) "
            "DO UPDATE SET value=MAX(value,excluded.value)",
            (kind, high),
        )


def migrate(db: Connection) -> None:
    """Backfill validated ages without deleting records or changing envelope payloads."""
    for table in ("messages", "remote_monitor"):
        columns = cast(
            "list[tuple[int, str, str, int, object, int]]",
            db.execute(f"PRAGMA table_info({table})").fetchall(),
        )
        if "retention_at" not in {row[1] for row in columns}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN retention_at TEXT")
    rows = cast("list[tuple[int, str]]", db.execute("SELECT id,sent_at FROM messages").fetchall())
    for key, sent_at in rows:
        db.execute("UPDATE messages SET retention_at=? WHERE id=?", (normalize_age(sent_at), key))
    monitors = cast(
        "list[tuple[int, str]]",
        db.execute("SELECT monitor_id,envelope FROM remote_monitor").fetchall(),
    )
    for key, payload in monitors:
        try:
            envelope = cast("dict[str, str]", json.loads(payload))
            age = normalize_age(envelope["sent_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Invalid monitor retention timestamp; maintenance stopped") from error
        db.execute("UPDATE remote_monitor SET retention_at=? WHERE monitor_id=?", (age, key))
    db.execute("CREATE INDEX IF NOT EXISTS messages_retention ON messages(retention_at,id)")
    db.execute(
        "CREATE INDEX IF NOT EXISTS remote_monitor_retention "
        "ON remote_monitor(retention_at,monitor_id)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS remote_outbox_local_id ON remote_outbox(local_id)")
    db.execute("CREATE TABLE IF NOT EXISTS remote_replay_guard (id TEXT PRIMARY KEY)")
    for table in ("remote_imports", "remote_outbox", "remote_monitor"):
        db.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_replay BEFORE INSERT ON {table} "
            "WHEN EXISTS (SELECT 1 FROM remote_replay_guard WHERE id=NEW.id) "
            "BEGIN SELECT RAISE(ABORT,'Purged remote message ID'); END"
        )
    _seed_ids(db)
    # Stored age changes must also update the normalized index key.
    for suffix, operation, condition in (
        ("insert", "INSERT", "WHEN NEW.retention_at IS NULL"),
        ("update", "UPDATE OF sent_at", ""),
    ):
        db.execute(
            f"CREATE TRIGGER IF NOT EXISTS messages_age_{suffix} AFTER {operation} ON messages "
            f"{condition} "
            "BEGIN UPDATE messages SET retention_at=strftime('%Y-%m-%dT%H:%M:%fZ',NEW.sent_at) "
            "WHERE id=NEW.id; END"
        )
