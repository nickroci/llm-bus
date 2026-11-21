"""Exact unattended claim bindings shared by decisions, directions and secret receipts."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from llm_bus.coordination import runs
from llm_bus.coordination.models import Direction
from llm_bus.errors import BusError

if TYPE_CHECKING:
    from datetime import datetime
    from sqlite3 import Connection


def claim(db: Connection, record: dict[str, object], actor: str, ticket: str) -> tuple[str, str]:
    """A claim's timestamp distinguishes even reacquisition by the same worker."""
    row = cast(
        "tuple[str, str] | None",
        db.execute(
            "SELECT c.branch,c.claimed_at FROM coordination_claims c "
            "JOIN coordination_enrollments e ON e.scope=c.scope AND e.project_id=c.project_id "
            "AND e.address=c.address JOIN agents a ON a.address=e.address "
            "WHERE c.scope=? AND c.project_id=? AND c.address=? AND c.ticket=? "
            "AND e.generation=? AND e.role='worker' AND a.scope=c.scope AND a.ended_at IS NULL",
            (record["scope"], record["project_id"], actor, ticket, record["generation"]),
        ).fetchone(),
    )
    if row is None or "/" in actor:
        raise BusError("Unattended authority denied: no current local worker claim")
    return row


def require(  # noqa: PLR0913 - exact authenticated action context
    db: Connection,
    record: dict[str, object],
    coordinator: str,
    actor: str,
    direction: Direction,
    *,
    permission: str | None,
    now: datetime,
) -> None:
    """Check run and exact claim; callers must also check grant, consent and operation."""
    # pylint: disable=too-many-arguments
    if direction.run_id is None or direction.ticket is None:
        raise BusError("Unattended authority denied: missing action binding")
    runs.require_active(
        db,
        record,
        coordinator,
        run_id=direction.run_id,
        ticket=direction.ticket,
        permission=permission,
        now=now,
    )
    if claim(db, record, actor, direction.ticket) != (direction.branch, direction.claimed_at):
        raise BusError("Unattended authority denied: stale claim")


def bind_gate(  # noqa: PLR0913 - exact record and claim bindings
    db: Connection,
    record: dict[str, object],
    actor: str,
    ticket: str,
    record_id: str,
    *,
    run_id: str | None = None,
) -> None:
    """Snapshot an exact claim when its worker submits gate evidence."""
    # pylint: disable=too-many-arguments
    found = cast(
        "tuple[str, str] | None",
        db.execute(
            "SELECT branch,claimed_at FROM coordination_claims WHERE address=? AND scope=? "
            "AND project_id=? AND ticket=?",
            (actor, record["scope"], record["project_id"], ticket),
        ).fetchone(),
    )
    if found is None:
        raise BusError("Gate snapshot denied: missing current claim")
    branch, claimed_at = found
    _gate_table(db)
    db.execute(
        "INSERT INTO coordination_gate_claims VALUES (?,?,?,?)",
        (record_id, branch, claimed_at, run_id),
    )


def _gate_table(db: Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS coordination_gate_claims ("
        "record_id TEXT PRIMARY KEY,branch TEXT NOT NULL,claimed_at TEXT NOT NULL,run_id TEXT)"
    )


def require_gate(  # noqa: PLR0913 - exact record and claim bindings
    db: Connection,
    record: dict[str, object],
    actor: str,
    ticket: str,
    record_id: str,
    *,
    run_id: str | None = None,
) -> tuple[str, str]:
    """Unattended gates cannot follow claim reacquisition or a different run binding."""
    # pylint: disable=too-many-arguments
    _gate_table(db)
    found = cast(
        "tuple[str, str, str | None] | None",
        db.execute(
            "SELECT branch,claimed_at,run_id FROM coordination_gate_claims WHERE record_id=?",
            (record_id,),
        ).fetchone(),
    )
    current = claim(db, record, actor, ticket)
    if found is None:
        raise BusError("Unattended gate denied: missing claim binding")
    branch, claimed_at, bound_run = found
    if current != (branch, claimed_at) or (bound_run is not None and bound_run != run_id):
        raise BusError("Unattended gate denied: stale claim or run")
    return current


def bind(  # noqa: PLR0913 - exact action binding
    db: Connection,
    record: dict[str, object],
    actor: str,
    direction: Direction,
    *,
    run_id: str,
    ticket: str,
) -> Direction:
    # pylint: disable=too-many-arguments
    branch, claimed_at = claim(db, record, actor, ticket)
    return Direction(
        direction.version,
        direction.project_id,
        direction.generation,
        direction.grant_id,
        direction.operation,
        run_id,
        ticket,
        branch,
        claimed_at,
    )
