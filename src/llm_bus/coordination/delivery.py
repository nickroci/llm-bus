"""Typed direction intent and current receiver authority at every exposure."""

import json
from datetime import UTC, datetime
from sqlite3 import Connection
from typing import cast

from llm_bus.coordination import run_actions
from llm_bus.coordination.authority import authorize, sender_gate_reason
from llm_bus.coordination.models import GATE_OPERATIONS, Approval, Direction
from llm_bus.coordination.store import project
from llm_bus.errors import BusError


def require_coordinator(db: Connection, sender: str, scope: str, direction: Direction) -> None:
    """Sender enrollment expresses intent; it cannot grant receiver authority."""
    direction.validate()
    if (
        db.execute(
            "SELECT 1 FROM coordination_projects p JOIN coordination_enrollments e "
            "ON e.scope=p.scope AND e.project_id=p.project_id "
            "WHERE p.scope=? AND p.project_id=? AND p.generation=? AND p.coordinator=? "
            "AND p.revoked_at IS NULL AND e.address=? AND e.role='coordinator' "
            "AND e.generation=p.generation",
            (scope, direction.project_id, direction.generation, sender, sender),
        ).fetchone()
        is None
    ):
        raise BusError("Direction requires current coordinator enrollment")


def outgoing(  # noqa: PLR0913, C901 - exact routing identity and binding checks
    db: Connection,
    *,
    sender: str,
    scope: str,
    label: str,
    recipient: str,
    operation: str,
    approval: Approval | None = None,
    run_id: str | None = None,
    ticket: str | None = None,
) -> Direction:
    """Resolve recipient-issued routing reference without treating it as a grant."""
    # pylint: disable=too-many-arguments
    if operation in GATE_OPERATIONS:
        if approval is None:
            raise BusError("Gate operations require an approval")
        approval.validate(operation)
    elif approval is not None:
        raise BusError("Only gate operations carry an approval")
    record = project(db, scope, label)
    target = cast(
        "tuple[str] | None",
        db.execute(
            "SELECT grant_id FROM coordination_targets WHERE scope=? AND project_id=? "
            "AND recipient=? AND generation=?",
            (scope, record["project_id"], recipient, record["generation"]),
        ).fetchone(),
    )
    if target is None:
        raise BusError("Missing current coordination target reference")
    direction = Direction(
        1, str(record["project_id"]), str(record["generation"]), target[0], operation
    )
    require_coordinator(db, sender, scope, direction)
    if run_id is not None:
        bound_ticket = approval.ticket if approval is not None else ticket
        if bound_ticket is None or "/" in recipient:
            raise BusError("Unattended directions require a local worker and explicit ticket")
        direction = run_actions.bind(
            db, record, recipient, direction, run_id=run_id, ticket=bound_ticket
        )
        decision = authorize(
            db,
            sender=sender,
            recipient=recipient,
            scope=scope,
            direction=direction,
            approval=approval,
            now=datetime.now(UTC),
        )
        if not decision.verified:
            raise BusError(f"Direction denied: {decision.reason}")
    elif ticket is not None:
        raise BusError("Direction ticket requires --run-id")
    if approval is not None and "/" in recipient:
        # Sender and receiver must both allow: this machine's owner delegation is checked first.
        reason = sender_gate_reason(
            db, scope=scope, direction=direction, approval=approval, now=datetime.now(UTC)
        )
        if reason is not None:
            raise BusError(f"Direction denied: {reason}")
    return direction


def persist(
    db: Connection, message_id: int, direction: Direction, approval: Approval | None = None
) -> None:
    """Write immutable strict metadata in the owning admission transaction."""
    direction.validate()
    db.execute(
        "INSERT INTO coordination_messages VALUES (?,?)",
        (message_id, json.dumps(direction.wire())),
    )
    if approval is not None:
        db.execute(
            "INSERT INTO coordination_approvals VALUES (?,?)",
            (message_id, json.dumps(approval.wire())),
        )


def _stored_approval(db: Connection, message_id: int, operation: str) -> Approval | None:
    row = cast(
        "tuple[str] | None",
        db.execute(
            "SELECT approval_json FROM coordination_approvals WHERE message_id=?", (message_id,)
        ).fetchone(),
    )
    if row is None:
        return None
    try:
        return Approval.parse(cast("object", json.loads(row[0])), operation)
    except (BusError, ValueError):
        return None


def verification(db: Connection, message_id: int) -> dict[str, object] | None:
    """Return fresh authority, or None for an ordinary historical message."""
    row = cast(
        "tuple[str, str, str, str | None, str | None] | None",
        db.execute(
            "SELECT c.metadata_json,m.sender,m.recipient,m.recipient_scope,a.scope "
            "FROM coordination_messages c JOIN messages m ON m.id=c.message_id "
            "LEFT JOIN agents a ON a.address=m.recipient WHERE m.id=?",
            (message_id,),
        ).fetchone(),
    )
    if row is None:
        return None
    metadata, sender, recipient, stored_scope, current_scope = row
    try:
        direction = Direction.parse(cast("object", json.loads(metadata)))
    except (BusError, ValueError, TypeError):
        return {"verified": False, "reason": "invalid_direction"}
    if current_scope is None or stored_scope != current_scope:
        return {**direction.wire(), "verified": False, "reason": "wrong_scope"}
    gate = direction.operation in GATE_OPERATIONS
    approval = _stored_approval(db, message_id, direction.operation) if gate else None
    decision = authorize(
        db,
        sender=sender,
        recipient=recipient,
        scope=current_scope,
        direction=direction,
        now=datetime.now(UTC),
        approval=approval,
    )
    result: dict[str, object] = {
        **direction.wire(),
        "verified": decision.verified,
        "reason": decision.reason,
    }
    if approval is not None:
        result["approval"] = approval.wire()
    return result


def exposure_allowed(db: Connection, message_id: int) -> bool:
    """Ordinary messages keep their existing policy; directions require current authority."""
    result = verification(db, message_id)
    return result is None or result["verified"] is True
