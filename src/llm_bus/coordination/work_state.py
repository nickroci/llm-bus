"""Recipient-recorded work state for local coordination directions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple, cast

from llm_bus.errors import BusError
from llm_bus.reports.lifecycle import TERMINAL, TRANSITION_CONFLICT, transition_conflicts
from llm_bus.reports.protocol import MAX_REVISION, parse_report, validate_status

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Connection

WORK_OPERATIONS = ("assign", "review", "reprioritize")
MAX_DIRECTIONS = 100
FILTERS = ("all", "attention", "open")
NOT_RECIPIENT = "Report requires a received local coordination direction or an imported message"
UNACKNOWLEDGED = "Acknowledge the direction before reporting its state"
TABLE = "coordination_work_reports"


def create_tables(db: Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS coordination_work_reports ("
        "message_id INTEGER PRIMARY KEY REFERENCES messages(id), "
        "revision INTEGER NOT NULL CHECK(revision>0), status TEXT NOT NULL, "
        "reason TEXT, reported_at TEXT NOT NULL)"
    )


def imported(db: Connection, message_id: int) -> bool:
    return (
        db.execute("SELECT 1 FROM remote_imports WHERE local_id=?", (message_id,)).fetchone()
        is not None
    )


def snapshot(db: Connection, message_id: int) -> dict[str, object] | None:
    row = cast(
        "tuple[int, str, str | None, str] | None",
        db.execute(
            "SELECT revision,status,reason,reported_at FROM coordination_work_reports "
            "WHERE message_id=?",
            (message_id,),
        ).fetchone(),
    )
    if row is None:
        return None
    status, reason = validate_status(row[1], row[2])
    return {
        "message_id": message_id,
        "revision": row[0],
        "status": status,
        "reason": reason,
        "reported_at": row[3],
    }


def record(
    db: Connection, message_id: int, recipient: str, status: object, reason: object
) -> dict[str, object]:
    """Record state; the caller already holds a write transaction."""
    # pylint: disable=import-outside-toplevel,cyclic-import,clean-code-commented-out-code
    selected, why = validate_status(status, reason)
    row = cast(
        "tuple[str, str | None] | None",
        db.execute(
            "SELECT m.recipient,m.acknowledged_at FROM coordination_messages c "
            "JOIN messages m ON m.id=c.message_id WHERE c.message_id=? "
            "AND NOT EXISTS (SELECT 1 FROM remote_imports WHERE local_id=m.id) "
            "AND NOT EXISTS (SELECT 1 FROM remote_outbox WHERE local_id=m.id)",
            (message_id,),
        ).fetchone(),
    )
    if row is None or row[0] != recipient:
        raise BusError(NOT_RECIPIENT)
    if row[1] is None:
        raise BusError(UNACKNOWLEDGED)
    prior = snapshot(db, message_id)
    if prior is not None and prior["status"] == selected and prior["reason"] == why:
        return prior
    if transition_conflicts(cast("str | None", prior["status"]) if prior else None, selected):
        raise BusError(TRANSITION_CONFLICT)
    revision = cast("int", prior["revision"]) + 1 if prior else 1
    if revision > MAX_REVISION:
        raise BusError("Work report revision limit reached")
    db.execute(
        "INSERT INTO coordination_work_reports VALUES (?,?,?,?,"
        "strftime('%Y-%m-%dT%H:%M:%fZ','now')) ON CONFLICT(message_id) DO UPDATE SET "
        "revision=excluded.revision,status=excluded.status,reason=excluded.reason,"
        "reported_at=excluded.reported_at",
        (message_id, revision, selected, why),
    )
    from llm_bus.coordination.stalls import observe_message  # noqa: PLC0415

    observe_message(db, message_id, now=datetime.now(UTC))
    return cast("dict[str, object]", snapshot(db, message_id))


def work_report(
    db: Connection, tables: set[str], message_id: int, *, remote_id: str | None, imported: bool
) -> object:
    """Read-only work state; tolerates tables missing from older schemas."""
    # pylint: disable=redefined-outer-name  # `imported` parameter shadows the module function
    if remote_id is not None and not imported:
        row = (
            cast(
                "tuple[str] | None",
                db.execute(
                    "SELECT payload FROM remote_reports WHERE id=?", (remote_id,)
                ).fetchone(),
            )
            if "remote_reports" in tables
            else None
        )
        return parse_report(cast("object", json.loads(row[0])), remote_id) if row else None
    if imported or TABLE not in tables:
        return None
    return snapshot(db, message_id)


class DirectionRow(NamedTuple):
    message_id: int
    operation: str
    grant: str
    sender: str
    recipient: str
    sent_at: str
    acked: str | None
    outbox_id: str | None
    import_id: str | None


def table_names(db: Connection) -> set[str]:
    return {
        row[0]
        for row in cast(
            "list[tuple[str]]",
            db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
        )
    }


def _direction_rows(
    db: Connection, record: dict[str, object], viewer: str, *, coordinator: bool
) -> list[DirectionRow]:
    # pylint: disable=redefined-outer-name  # `record` parameter shadows the module function
    found = cast(
        "list[tuple[int, str, str, str, str, str, str | None, str | None, str | None]]",
        db.execute(
            "SELECT m.id,json_extract(c.metadata_json,'$.operation'),"
            "json_extract(c.metadata_json,'$.grant_id'),m.sender,m.recipient,"
            "m.sent_at,m.acknowledged_at,o.id,i.id "
            "FROM coordination_messages c JOIN messages m ON m.id=c.message_id "
            "LEFT JOIN remote_outbox o ON o.local_id=m.id "
            "LEFT JOIN remote_imports i ON i.local_id=m.id "
            "WHERE json_extract(c.metadata_json,'$.project_id')=? "
            "AND json_extract(c.metadata_json,'$.generation')=? "
            "AND ((? AND m.sender=?) OR m.recipient=?) ORDER BY m.id DESC",
            (record["project_id"], record["generation"], coordinator, viewer, viewer),
        ).fetchall(),
    )
    return [DirectionRow(*row) for row in found]


def _is_open(
    acked: str | None, operation: str, *, imported_here: bool, cancelled: bool, status: object
) -> bool:
    return (
        acked is not None
        and operation in WORK_OPERATIONS
        and not imported_here
        and not cancelled
        and status not in TERMINAL
    )


def _state_changed_at(row: DirectionRow, report: dict[str, object] | None) -> str | None:
    if report is None:
        return row.acked
    if row.outbox_id is None:
        return cast("str | None", report["reported_at"])
    # A relay report has no local report time; a revision-0 snapshot is no report yet.
    return row.acked if report["status"] is None else None


def _item(
    row: DirectionRow,
    report: dict[str, object] | None,
) -> dict[str, object]:
    return {
        "message_id": row.message_id,
        "recipient": row.recipient,
        "operation": row.operation,
        "sent_at": row.sent_at,
        "acknowledged_at": row.acked,
        "remote_id": row.outbox_id or row.import_id,
        "work_report": report,
        "state_changed_at": _state_changed_at(row, report),
    }


def direction_states(
    db: Connection, project: dict[str, object], viewer: str, *, coordinator: bool
) -> Iterator[tuple[DirectionRow, dict[str, object] | None, bool, bool]]:
    """Uncapped lifecycle traversal shared by historical listing and stall detection."""
    tables = table_names(db)
    cancels: set[tuple[str, str, str]] = set()
    for row in _direction_rows(db, project, viewer, coordinator=coordinator):
        key = (row.grant, row.sender, row.recipient)
        if row.operation == "cancel" and row.acked is not None:
            cancels.add(key)
        # Descending IDs: every cancel collected so far has a greater ID than this row.
        cancelled = row.operation in WORK_OPERATIONS and key in cancels
        report = cast(
            "dict[str, object] | None",
            work_report(
                db,
                tables,
                row.message_id,
                remote_id=row.outbox_id,
                imported=row.import_id is not None,
            ),
        )
        is_open = _is_open(
            row.acked,
            row.operation,
            imported_here=row.import_id is not None,
            cancelled=cancelled,
            status=None if report is None else report["status"],
        )
        yield row, report, is_open, cancelled


def listing(
    db: Connection,
    record: dict[str, object],
    viewer: str,
    *,
    coordinator: bool,
    only: str = "all",
) -> list[dict[str, object]]:
    """Directions the viewer sent (coordinator) or received, newest first, with work state."""
    # pylint: disable=redefined-outer-name  # `record` parameter shadows the module function
    if only not in FILTERS:
        raise ValueError(f"only must be one of {FILTERS}")
    items: list[dict[str, object]] = []
    for row, report, is_open, cancelled in direction_states(
        db, record, viewer, coordinator=coordinator
    ):
        if only == "all":
            wanted = True
        elif only == "open":
            wanted = is_open
        else:
            wanted = is_open or (row.acked is None and not cancelled)
        if wanted:
            items.append(_item(row, report))
    return items[:MAX_DIRECTIONS]
