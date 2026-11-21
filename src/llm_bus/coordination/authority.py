"""One receiver-side predicate for all direction exposure consumers."""

import json
from datetime import datetime
from sqlite3 import Connection
from typing import cast

from llm_bus.coordination import delegation, run_actions, runs
from llm_bus.coordination.models import (
    GATE_OPERATIONS,
    OPERATIONS,
    Approval,
    AuthorityDecision,
    Direction,
    timestamp,
)
from llm_bus.errors import BusError
from llm_bus.mailbox.agents import expire_idle_agents
from llm_bus.mailbox.models import is_local_address
from llm_bus.policy.config import optional_config

REMOTE_PARTS = 3


def project_execution_allowed(  # noqa: PLR0911 - fail-closed authority checks
    db: Connection, record: dict[str, object], address: str, *, scope: str, now: datetime
) -> bool:
    """Fresh caller-bound local consent; stored history and remote identities grant none."""
    # pylint: disable=clean-code-commented-out-code,too-many-return-statements
    expire_idle_agents(db)
    if not is_local_address(address):
        return False
    kind = address.partition(":")[0]
    row = cast(
        "tuple[str] | None",
        db.execute(
            "SELECT e.role FROM coordination_enrollments e "
            "JOIN coordination_projects p ON p.scope=e.scope AND p.project_id=e.project_id "
            "JOIN agents a ON a.address=e.address "
            "WHERE e.address=? AND e.scope=? AND e.project_id=? AND e.generation=? "
            "AND p.generation=e.generation AND p.coordinator=? AND p.revoked_at IS NULL "
            "AND a.scope=e.scope AND a.kind=? AND a.ended_at IS NULL",
            (
                address,
                scope,
                record["project_id"],
                record["generation"],
                record["coordinator"],
                kind,
            ),
        ).fetchone(),
    )
    if row is None or scope != record["scope"] or "/" in str(record["coordinator"]):
        return False
    try:
        if row[0] == "coordinator":
            project = delegation.project_delegation(db, record)
            return address == record["coordinator"] and bool(project and project.project_execution)
        if row[0] != "worker":
            return False
        grants = cast(
            "list[tuple[str]]",
            db.execute(
                "SELECT grant_id FROM coordination_grants WHERE scope=? AND project_id=? "
                "AND recipient=? AND generation=? AND coordinator=?",
                (scope, record["project_id"], address, record["generation"], record["coordinator"]),
            ).fetchall(),
        )
        for (grant_id,) in grants:
            held = delegation.effective(db, record, grant_id, now=now)
            if held is None or not held.project_execution:
                continue
            if any(
                authorize(
                    db,
                    sender=str(record["coordinator"]),
                    recipient=address,
                    scope=scope,
                    direction=Direction(
                        1, str(record["project_id"]), str(record["generation"]), grant_id, operation
                    ),
                    now=now,
                ).verified
                for operation in OPERATIONS
            ):
                return True
    except (BusError, ValueError, TypeError):
        return False
    return False


def authorize(  # noqa: PLR0913, C901, PLR0911, PLR0912 - explicit denial precedence
    db: Connection,
    *,
    sender: str,
    recipient: str,
    scope: str,
    direction: Direction,
    now: datetime,
    approval: Approval | None = None,
) -> AuthorityDecision:
    """Check exact local grant and enrollment; caller owns admission transaction."""
    # pylint: disable=clean-code-commented-out-code,clean-code-redundant-comment
    # pylint: disable=too-many-arguments,too-many-return-statements
    # pylint: disable=too-many-branches,too-many-locals

    def deny(reason: str) -> AuthorityDecision:
        return AuthorityDecision(verified=False, reason=reason)

    expire_idle_agents(db)
    try:
        direction.validate()
    except (BusError, AttributeError, TypeError):
        return deny("invalid_direction")
    project = cast(
        "tuple[str, str, str | None] | None",
        db.execute(
            "SELECT generation,coordinator,revoked_at FROM coordination_projects "
            "WHERE scope=? AND project_id=?",
            (scope, direction.project_id),
        ).fetchone(),
    )
    if project is None:
        exists = cast(
            "tuple[int] | None",
            db.execute(
                "SELECT 1 FROM coordination_projects WHERE project_id=?", (direction.project_id,)
            ).fetchone(),
        )
        return deny("wrong_scope" if exists else "unknown_project")
    generation, coordinator, revoked_at = project
    if revoked_at is not None:
        return deny("revoked")
    if generation != direction.generation:
        return deny("stale_generation")
    if coordinator != sender:
        return deny("wrong_coordinator")
    consent = cast(
        "tuple[str, str | None, str | None, str | None] | None",
        db.execute(
            "SELECT c.address,c.ended_at,a.scope,a.ended_at FROM coordination_session_consents c "
            "LEFT JOIN agents a ON a.address=c.address WHERE c.grant_id=?",
            (direction.grant_id,),
        ).fetchone(),
    )
    if consent is not None:
        owner, ended, current_scope, session_ended = consent
        if owner != recipient or ended is not None or session_ended is not None:
            return deny("revoked")
        if current_scope != scope:
            return deny("out_of_scope")
    enrolled = cast(
        "tuple[str] | None",
        db.execute(
            "SELECT generation FROM coordination_enrollments WHERE scope=? AND project_id=? "
            "AND address=?",
            (scope, direction.project_id, recipient),
        ).fetchone(),
    )
    if enrolled is None:
        return deny("not_enrolled")
    if enrolled[0] != direction.generation:
        return deny("stale_generation")
    grant = cast(
        "tuple[str, str, str, str, str | None, str | None] | None",
        db.execute(
            "SELECT grant_id,generation,coordinator,operations_json,expires_at,revoked_at "
            "FROM coordination_grants WHERE scope=? AND project_id=? "
            "AND recipient=? AND grant_id=?",
            (scope, direction.project_id, recipient, direction.grant_id),
        ).fetchone(),
    )
    if grant is None:
        return deny("invalid_direction")
    grant_id, generation, coordinator, operations_json, expires_at, revoked_at = grant
    if revoked_at is not None:
        return deny("revoked")
    if generation != direction.generation:
        return deny("stale_generation")
    if coordinator != sender:
        return deny("wrong_coordinator")
    try:
        if expires_at is not None and timestamp(expires_at) <= now:
            return deny("expired")
        operations = cast("object", json.loads(operations_json))
        if not isinstance(operations, list):
            return deny("invalid_direction")
        allowed = cast("list[object]", operations)
        if not all(isinstance(item, str) and item in OPERATIONS for item in allowed):
            return deny("invalid_direction")
        if direction.run_id is not None:
            record: dict[str, object] = {
                "scope": scope,
                "project_id": direction.project_id,
                "generation": generation,
            }
            if reason := _run_reason(
                db,
                record,
                sender=sender,
                recipient=recipient,
                direction=direction,
                approval=approval,
                now=now,
            ):
                return deny(reason)
        if direction.operation in GATE_OPERATIONS:
            return _gate_decision(
                db,
                {"scope": scope, "project_id": direction.project_id, "generation": generation},
                coordinator=sender,
                recipient=recipient,
                direction=direction,
                approval=approval,
                now=now,
            )
        if direction.operation not in allowed:
            return deny("operation_denied")
    except (BusError, ValueError, TypeError):
        return deny("invalid_direction")
    return AuthorityDecision(verified=True, reason="verified", grant_id=grant_id)


def _run_reason(  # noqa: PLR0913, PLR0911, C901 - explicit fail-closed action bindings
    db: Connection,
    record: dict[str, object],
    *,
    sender: str,
    recipient: str,
    direction: Direction,
    approval: Approval | None,
    now: datetime,
) -> str | None:
    """Run scope supplements, and never replaces, the ordinary authority predicate."""
    # pylint: disable=clean-code-commented-out-code,too-many-arguments,too-many-return-statements
    permission: str | None = None
    if direction.operation in GATE_OPERATIONS:
        if approval is None:
            return "invalid_direction"
        permission = f"{GATE_OPERATIONS[direction.operation]}:{approval.base}"
        if (approval.ticket, approval.branch) != (direction.ticket, direction.branch):
            return "wrong_ticket"
        if direction.operation == "approve-merge-admin":
            return "unattended_admin_denied"
        if direction.operation == "approve-merge" and approval.record_id is None:
            return "independent_review_required"
        if direction.operation == "approve-merge" and approval.repository is None:
            return "repository_binding_required"
    try:
        run_actions.require(
            db, record, sender, recipient, direction, permission=permission, now=now
        )
        if direction.operation == "approve-merge" and approval is not None:
            runs.require_active(
                db, record, sender, run_id=direction.run_id, permission="auto-merge", now=now
            )
            held = delegation.effective(db, record, direction.grant_id, now=now)
            if held is None or not held.auto_merge:
                return "gate_not_delegated"
            submitter = cast(
                "tuple[str, str] | None",
                db.execute(
                    "SELECT submitted_by,grant_id FROM coordination_gate_records WHERE record_id=?",
                    (approval.record_id,),
                ).fetchone(),
            )
            if submitter != (recipient, direction.grant_id):
                return "record_wrong_worker"
            run_actions.require_gate(
                db,
                record,
                recipient,
                approval.ticket,
                str(approval.record_id),
                run_id=direction.run_id,
            )
    except BusError:
        return "unattended_authority_denied"
    return None


def _record_accepted(db: Connection, record: dict[str, object], approval: Approval) -> bool:
    """A record-bound approval holds only while its gate record is accepted and unchanged."""
    row = cast(
        "tuple[str, str, str, str, str, str, str, int, str] | None",
        db.execute(
            "SELECT scope,project_id,generation,status,ticket,branch,base,pr,head_sha "
            "FROM coordination_gate_records WHERE record_id=?",
            (approval.record_id,),
        ).fetchone(),
    )
    return row == (
        record["scope"],
        record["project_id"],
        record["generation"],
        "accepted",
        approval.ticket,
        approval.branch,
        approval.base,
        approval.pr,
        approval.head_sha,
    )


def _peer_gates(record: dict[str, object], coordinator: str) -> tuple[str, ...]:
    """Gates this machine's owner lets a remote coordinator use; no config or peer allows none."""
    config = optional_config()
    parts = coordinator.split("/")
    repo = None if config is None else config.repository_scope(str(record["scope"]))
    if repo is None or len(parts) != REMOTE_PARTS or parts[1] != repo.repo_id:
        return ()
    peer = next((item for item in repo.peers if item.machine_id == parts[0]), None)
    # A peer blocked or monitor-only now holds no cap, so an admitted approval stops verifying.
    if peer is None or peer.receive_messages != "inbox":
        return ()
    return peer.accept_delegated_gates


def _delegation_reason(
    project: delegation.Delegation | None,
    granted: delegation.Delegation | None,
    gate: str,
    approval: Approval,
    now: datetime,
) -> str | None:
    """The one ordered gate, target and expiry check, shared by receiver and sender."""
    if project is None or granted is None or not granted.targets(gate):
        return "gate_not_delegated"
    if approval.base not in granted.targets(gate):
        return "target_denied"
    # The work branch is checked against the whole project set, never a narrower binding or cap.
    if approval.branch == approval.base or approval.branch in project.all_targets():
        return "target_denied"
    if timestamp(approval.expires_at) <= now:
        return "expired"
    return None


def sender_gate_reason(
    db: Connection, *, scope: str, direction: Direction, approval: Approval, now: datetime
) -> str | None:
    """Coordinator-machine check against its own owner's project delegation; None allows."""
    gate = GATE_OPERATIONS.get(direction.operation)
    row = cast(
        "tuple[str, str | None] | None",
        db.execute(
            "SELECT generation,revoked_at FROM coordination_projects "
            "WHERE scope=? AND project_id=?",
            (scope, direction.project_id),
        ).fetchone(),
    )
    if gate is None:
        return "invalid_direction"
    if row is None or row[1] is not None or row[0] != direction.generation:
        return "gate_not_delegated"
    record: dict[str, object] = {
        "scope": scope,
        "project_id": direction.project_id,
        "generation": direction.generation,
    }
    project = delegation.project_delegation(db, record)
    return _delegation_reason(project, project, gate, approval, now)


def _gate_decision(  # noqa: PLR0911, PLR0913 - explicit denial precedence
    db: Connection,
    record: dict[str, object],
    *,
    coordinator: str,
    recipient: str,
    direction: Direction,
    approval: Approval | None,
    now: datetime,
) -> AuthorityDecision:
    """Gate operations rest on delegation, target limits, expiry and the worker's own claim."""
    # pylint: disable=clean-code-commented-out-code,clean-code-redundant-comment
    # pylint: disable=too-many-return-statements,too-many-arguments

    def deny(reason: str) -> AuthorityDecision:
        return AuthorityDecision(verified=False, reason=reason)

    try:
        if approval is None:
            return deny("invalid_direction")
        approval.validate(direction.operation)
    except BusError:
        return deny("invalid_direction")
    remote = "/" in coordinator
    if approval.record_id is not None:
        # Gate records stay local: a record-bound approval never verifies from a remote machine.
        if remote:
            return deny("invalid_direction")
        if not _record_accepted(db, record, approval):
            return deny("record_withdrawn")
    project = delegation.project_delegation(db, record)
    granted = delegation.effective(db, record, direction.grant_id, now=now)
    if remote and granted is not None:
        granted = granted.only_gates(_peer_gates(record, coordinator))
    reason = _delegation_reason(
        project, granted, GATE_OPERATIONS[direction.operation], approval, now
    )
    if reason is not None:
        return deny(reason)
    claim = cast(
        "tuple[int] | None",
        db.execute(
            "SELECT 1 FROM coordination_claims WHERE address=? AND scope=? AND project_id=? "
            "AND ticket=? AND branch=?",
            (recipient, record["scope"], record["project_id"], approval.ticket, approval.branch),
        ).fetchone(),
    )
    if claim is None:
        return deny("wrong_ticket")
    return AuthorityDecision(verified=True, reason="verified", grant_id=direction.grant_id)
