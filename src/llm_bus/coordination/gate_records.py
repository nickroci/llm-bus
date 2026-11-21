"""Merge gate records: the worker submits, the coordinator accepts or rejects.

Acceptance sends the worker an ordinary merge approval bound to the record id, so the
receiving bus reverifies it at every exposure and hides it once the record leaves
``accepted``. Records and notices grant nothing by themselves.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from llm_bus.coordination import delegation, delivery, run_actions, runs
from llm_bus.coordination.authority import authorize
from llm_bus.coordination.models import Approval, Direction
from llm_bus.errors import BusError
from llm_bus.mailbox.writes import send_in_transaction

if TYPE_CHECKING:
    from collections.abc import Sequence
    from sqlite3 import Connection

    from llm_bus.mailbox.models import Message

LIFETIME = timedelta(hours=24)
MAX_EVIDENCE = 8
MAX_CHECKS = 32
MAX_TEXT = 512
REJECTED = "rejected"
Result = tuple[dict[str, object], "list[Message]"]


def _texts(values: Sequence[str], name: str, limit: int) -> list[str]:
    listed = list(values)
    if not 0 < len(listed) <= limit:
        raise BusError(f"Gate record needs 1 to {limit} {name}")
    for value in listed:
        if not value or len(value) > MAX_TEXT or not value.isprintable():
            raise BusError(f"Invalid gate record {name} entry")
    return listed


def _enrolled_role(db: Connection, record: dict[str, object], address: str) -> str | None:
    row = cast(
        "tuple[str] | None",
        db.execute(
            "SELECT role FROM coordination_enrollments WHERE scope=? AND project_id=? "
            "AND address=? AND generation=?",
            (record["scope"], record["project_id"], address, record["generation"]),
        ).fetchone(),
    )
    return None if row is None else row[0]


def _grant_id(db: Connection, record: dict[str, object], address: str) -> str | None:
    row = cast(
        "tuple[str] | None",
        db.execute(
            "SELECT grant_id FROM coordination_grants WHERE scope=? AND project_id=? "
            "AND recipient=? AND generation=? AND revoked_at IS NULL ORDER BY rowid DESC LIMIT 1",
            (record["scope"], record["project_id"], address, record["generation"]),
        ).fetchone(),
    )
    return None if row is None else row[0]


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def submit(  # noqa: PLR0913 - explicit gate fields
    db: Connection,
    record: dict[str, object],
    caller: str,
    *,
    ticket: str,
    branch: str,
    base: str,
    pr: int,
    head_sha: str,
    evidence: Sequence[str],
    checks: Sequence[str],
    now: datetime,
    run_id: str | None = None,
) -> Result:
    """The claiming worker records its review verdict; the coordinator still must accept."""
    # pylint: disable=too-many-arguments,too-many-locals
    if caller == record["coordinator"] or _enrolled_role(db, record, caller) != "worker":
        raise BusError("Only the worker that claims the branch submits gate records")
    expires_at = _stamp(now + LIFETIME)
    Approval(ticket, branch, base, expires_at, pr, head_sha).validate("approve-merge")
    evidence_list = _texts(evidence, "review evidence references", MAX_EVIDENCE)
    check_list = _texts(checks, "required checks", MAX_CHECKS)
    claimed = cast(
        "tuple[int] | None",
        db.execute(
            "SELECT 1 FROM coordination_claims WHERE address=? AND scope=? AND project_id=? "
            "AND ticket=? AND branch=?",
            (caller, record["scope"], record["project_id"], ticket, branch),
        ).fetchone(),
    )
    if claimed is None:
        raise BusError("Gate record not submitted: wrong_ticket")
    grant_id = _grant_id(db, record, caller)
    granted = None if grant_id is None else delegation.effective(db, record, grant_id, now=now)
    merge_delegated = not (
        grant_id is None
        or granted is None
        or not granted.auto_merge
        or not any(base in granted.targets(gate) for gate in delegation.MERGE_GATES)
    )
    if run_id is not None:
        runs.require_active(db, record, str(record["coordinator"]), run_id=run_id, ticket=ticket)
        if (
            grant_id is None
            or not authorize(
                db,
                sender=str(record["coordinator"]),
                recipient=caller,
                scope=str(record["scope"]),
                direction=Direction(
                    1, str(record["project_id"]), str(record["generation"]), grant_id, "clarify"
                ),
                now=now,
            ).verified
        ):
            raise BusError("Gate observation requires current worker consent")
    elif not merge_delegated:
        raise BusError("Gate record not submitted: gate_not_delegated")
    # The branch is the submitter's own claim, so no other worker can supersede this one.
    key = (record["scope"], record["project_id"], record["generation"], pr, branch)
    superseded = [
        item[0]
        for item in cast(
            "list[tuple[str]]",
            db.execute(
                "SELECT record_id FROM coordination_gate_records WHERE scope=? AND project_id=? "
                "AND generation=? AND pr=? AND branch=? AND status IN ('submitted','accepted')",
                key,
            ).fetchall(),
        )
    ]
    for old in superseded:
        db.execute(
            "UPDATE coordination_gate_records SET status='superseded' WHERE record_id=?", (old,)
        )
    record_id = uuid4().hex
    db.execute(
        "INSERT INTO coordination_gate_records VALUES (?,?,?,?,?,?,?,?,?,?,'approved',?,?,?,?,"
        "'submitted',NULL,NULL,?)",
        (
            record_id,
            record["scope"],
            record["project_id"],
            record["generation"],
            grant_id,
            ticket,
            branch,
            base,
            pr,
            head_sha,
            json.dumps(evidence_list),
            json.dumps(check_list),
            caller,
            _stamp(now),
            expires_at,
        ),
    )
    run_actions.bind_gate(db, record, caller, ticket, record_id, run_id=run_id)
    notice = send_in_transaction(
        db,
        caller,
        str(record["coordinator"]),
        f"Gate record {record_id} submitted for PR {pr} head {head_sha}. "
        "Verify live, then run coordination gate-accept.",
        cross_folder=True,
    )
    return (
        {
            "record_id": record_id,
            "status": "submitted",
            "expires_at": expires_at,
            "superseded": superseded,
        },
        [notice],
    )


def _load(
    db: Connection, record: dict[str, object], caller: str, record_id: str, verb: str
) -> dict[str, object]:
    """Coordinator-only lookup of a record of the current generation."""

    def refuse(reason: str) -> BusError:
        return BusError(f"Gate record not {verb}: {reason}")

    if caller != record["coordinator"] or _enrolled_role(db, record, caller) != "coordinator":
        raise refuse("not_coordinator")
    cursor = db.execute(
        "SELECT * FROM coordination_gate_records WHERE record_id=? AND scope=? AND project_id=?",
        (record_id, record["scope"], record["project_id"]),
    )
    found = cast("tuple[object, ...] | None", cursor.fetchone())
    if found is None:
        raise refuse("no_record")
    row = dict(zip((column[0] for column in cursor.description), found, strict=True))
    if row["generation"] != record["generation"]:
        raise refuse("stale_generation")
    return row


def _decide(
    db: Connection, row: dict[str, object], status: str, caller: str, now: datetime
) -> None:
    db.execute(
        "UPDATE coordination_gate_records SET status=?,decided_by=?,decided_at=? WHERE record_id=?",
        (status, caller, _stamp(now), row["record_id"]),
    )


def accept(  # noqa: PLR0913 - explicit acceptance inputs
    db: Connection,
    record: dict[str, object],
    caller: str,
    *,
    record_id: str,
    head_sha: str,
    admin: bool,
    now: datetime,
    run_id: str | None = None,
    repository: str | None = None,
) -> Result:
    """Mark the record accepted and send the worker an ordinary record-bound merge approval."""
    # pylint: disable=too-many-arguments,too-many-locals
    row = _load(db, record, caller, record_id, "accepted")
    if run_id is not None:
        run_actions.require_gate(
            db, record, str(row["submitted_by"]), str(row["ticket"]), record_id, run_id=run_id
        )
    reason = None
    if row["status"] != "submitted":
        reason = "not_submitted"
    elif now >= datetime.fromisoformat(str(row["expires_at"])):
        reason = "expired"
    elif head_sha != row["head_sha"]:
        reason = "sha_mismatch"
    else:
        granted = delegation.effective(db, record, str(row["grant_id"]), now=now)
        if granted is None or not granted.auto_merge:
            reason = "gate_not_delegated"
    if reason is not None:
        raise BusError(f"Gate record not accepted: {reason}")
    _decide(db, row, "accepted", caller, now)
    operation = "approve-merge-admin" if admin else "approve-merge"
    approval = Approval(
        str(row["ticket"]),
        str(row["branch"]),
        str(row["base"]),
        _stamp(now + LIFETIME),
        cast("int", row["pr"]),
        str(row["head_sha"]),
        record_id=record_id,
        repository=repository,
    )
    recipient = str(row["submitted_by"])
    direction = delivery.outgoing(
        db,
        sender=caller,
        scope=str(record["scope"]),
        label=str(record["label"]),
        recipient=recipient,
        operation=operation,
        approval=approval,
        run_id=run_id,
    )
    message = send_in_transaction(
        db,
        caller,
        recipient,
        f"Approval {operation.removeprefix('approve-')}: gate record {record_id}, "
        f"ticket {approval.ticket}, branch {approval.branch} into {approval.base}, "
        f"PR {approval.pr}, head {approval.head_sha}. "
        "Wait for required CI on that SHA, verify live, then merge.",
        cross_folder=True,
        direction=direction,
        approval=approval,
    )
    return (
        {
            "record_id": record_id,
            "status": "accepted",
            "message_id": message["id"],
            "operation": operation,
            "expires_at": approval.expires_at,
        },
        [message],
    )


def reject(
    db: Connection, record: dict[str, object], caller: str, *, record_id: str, now: datetime
) -> Result:
    """Withdraw a submitted or accepted record; its sent approval stops verifying."""
    row = _load(db, record, caller, record_id, REJECTED)
    if row["status"] not in {"submitted", "accepted"}:
        raise BusError(f"Gate record not {REJECTED}: not_submitted")
    _decide(db, row, REJECTED, caller, now)
    notice = send_in_transaction(
        db,
        caller,
        str(row["submitted_by"]),
        f"Gate record {record_id} was rejected; do not merge on its approval.",
        cross_folder=True,
    )
    return {"record_id": record_id, "status": REJECTED}, [notice]
