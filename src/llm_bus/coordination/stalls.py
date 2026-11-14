"""Local, durable communication clocks for currently authorized open directions.

Inspection is pure. Writers call ``observe`` after admitted evidence, inside their
transaction; recovery calls ``reconcile``. Neither path creates notices or wakes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from llm_bus.coordination import work_state
from llm_bus.coordination.models import timestamp
from llm_bus.errors import BusError
from llm_bus.policy.config import optional_config

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Connection

DEFAULT_THRESHOLD_SECONDS = 1800
MIN_THRESHOLD_SECONDS = 60
MAX_THRESHOLD_SECONDS = 86400
OPERATIONS = ("assign", "review")
REMOTE_ADDRESS_PARTS = 3
OBSERVATION_PENDING = "observation_pending"
MINUTE_SECONDS = 60
HOUR_SECONDS = 3600


def create_tables(db: Connection) -> None:
    """Add only local bookkeeping; migration never observes or launches work."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS coordination_stall_settings (scope TEXT NOT NULL, "
        "project_id TEXT NOT NULL, generation TEXT NOT NULL, threshold_seconds INTEGER NOT NULL "
        "CHECK(threshold_seconds BETWEEN 60 AND 86400), PRIMARY KEY(scope,project_id,generation))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS coordination_stall_state (message_id INTEGER PRIMARY KEY "
        "REFERENCES messages(id), scope TEXT NOT NULL, project_id TEXT NOT NULL, "
        "generation TEXT NOT NULL, evidence TEXT NOT NULL, last_communication_at TEXT NOT NULL, "
        "observation TEXT NOT NULL CHECK(observation IN ('local','remote_observed')), "
        "epoch INTEGER NOT NULL CHECK(epoch>0), notified_epoch INTEGER, "
        "notice_message_id INTEGER REFERENCES messages(id))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS coordination_stall_timers (scope TEXT NOT NULL, "
        "project_id TEXT NOT NULL, generation TEXT NOT NULL, token TEXT NOT NULL, "
        "lease_until TEXT NOT NULL, PRIMARY KEY(scope,project_id,generation))"
    )


def parse_threshold(value: str) -> int:
    matched = re.fullmatch(r"([0-9]+)([mh])", value)
    if matched is None:
        raise BusError("Threshold requires integer m or h units, from 1m through 24h")
    amount, unit = matched.groups()
    seconds = int(amount) * (MINUTE_SECONDS if unit == "m" else HOUR_SECONDS)
    if not MIN_THRESHOLD_SECONDS <= seconds <= MAX_THRESHOLD_SECONDS:
        raise BusError("Threshold must be from 1m through 24h")
    return seconds


def _binding(project: dict[str, object]) -> tuple[object, object, object]:
    return project["scope"], project["project_id"], project["generation"]


def require_coordinator(db: Connection, project: dict[str, object], address: str) -> None:
    """Check current local enrollment without lazily expiring or updating sessions."""
    if (
        "/" in address
        or db.execute(
            "SELECT 1 FROM coordination_projects p JOIN coordination_enrollments e "
            "ON e.scope=p.scope AND e.project_id=p.project_id AND e.generation=p.generation "
            "JOIN agents a ON a.address=e.address "
            "WHERE p.scope=? AND p.project_id=? AND p.generation=? AND p.revoked_at IS NULL "
            "AND p.coordinator=? AND e.address=? AND e.role='coordinator' "
            "AND a.scope=p.scope AND a.ended_at IS NULL",
            (*_binding(project), address, address),
        ).fetchone()
        is None
    ):
        raise BusError("Stall inspection requires current local coordinator enrollment")


def threshold(db: Connection, project: dict[str, object]) -> int:
    row = cast(
        "tuple[int] | None",
        db.execute(
            "SELECT threshold_seconds FROM coordination_stall_settings "
            "WHERE scope=? AND project_id=? AND generation=?",
            _binding(project),
        ).fetchone(),
    )
    return DEFAULT_THRESHOLD_SECONDS if row is None else row[0]


def configure(
    db: Connection, project: dict[str, object], address: str, seconds: int
) -> dict[str, object]:
    require_coordinator(db, project, address)
    if isinstance(seconds, bool) or not MIN_THRESHOLD_SECONDS <= seconds <= MAX_THRESHOLD_SECONDS:
        raise BusError("Threshold must be from 1m through 24h")
    db.execute(
        "INSERT INTO coordination_stall_settings VALUES (?,?,?,?) "
        "ON CONFLICT(scope,project_id,generation) DO UPDATE SET "
        "threshold_seconds=excluded.threshold_seconds",
        (*_binding(project), seconds),
    )
    return {
        "project_id": project["project_id"],
        "generation": project["generation"],
        "threshold_seconds": seconds,
    }


def _local_grant(
    db: Connection, project: dict[str, object], row: work_state.DirectionRow, now: datetime
) -> bool:
    grant = cast(
        "tuple[str, str | None] | None",
        db.execute(
            "SELECT g.operations_json,g.expires_at FROM coordination_grants g "
            "JOIN coordination_enrollments e ON e.scope=g.scope AND e.project_id=g.project_id "
            "AND e.address=g.recipient AND e.generation=g.generation "
            "JOIN agents a ON a.address=g.recipient "
            "LEFT JOIN coordination_session_consents c ON c.grant_id=g.grant_id "
            "JOIN messages m ON m.id=? "
            "WHERE g.scope=? AND g.project_id=? AND g.generation=? "
            "AND g.grant_id=? AND g.coordinator=? AND g.recipient=? AND g.revoked_at IS NULL "
            "AND c.ended_at IS NULL AND (c.address IS NULL OR c.address=g.recipient) "
            "AND a.ended_at IS NULL AND a.scope=g.scope AND m.recipient_scope=g.scope "
            "AND m.sender_scope=g.scope",
            (row.message_id, *_binding(project), row.grant, row.sender, row.recipient),
        ).fetchone(),
    )
    if grant is None:
        return False
    operations = cast("object", json.loads(grant[0]))
    return (
        isinstance(operations, list)
        and row.operation in operations
        and (grant[1] is None or timestamp(grant[1]) > now)
    )


def _remote_target(
    db: Connection, project: dict[str, object], row: work_state.DirectionRow
) -> bool:
    config = optional_config()
    parts = row.recipient.split("/")
    if config is None or len(parts) != REMOTE_ADDRESS_PARTS:
        return False
    repo = config.repository_scope(str(project["scope"]))
    peer = config.peer(parts[1], parts[0])
    if repo is None or repo.repo_id != parts[1] or peer is None or not peer.send_messages:
        return False
    return (
        db.execute(
            "SELECT 1 FROM coordination_targets t JOIN messages m ON m.id=? "
            "WHERE t.scope=? AND t.project_id=? AND t.generation=? AND t.recipient=? "
            "AND t.grant_id=? AND m.sender_scope=t.scope",
            (row.message_id, *_binding(project), row.recipient, row.grant),
        ).fetchone()
        is not None
    )


def eligible(
    db: Connection, project: dict[str, object], *, now: datetime
) -> Iterator[tuple[work_state.DirectionRow, dict[str, object] | None]]:
    """Uncapped current sender projection; targets never assert remote receiver consent."""
    address = str(project["coordinator"])
    try:
        require_coordinator(db, project, address)
    except BusError:
        return
    for row, report, is_open, _cancelled in work_state.direction_states(
        db, project, address, coordinator=True
    ):
        if not is_open or row.operation not in OPERATIONS or row.sender != address:
            continue
        permitted = (
            _remote_target(db, project, row)
            if row.outbox_id is not None
            else _local_grant(db, project, row, now)
        )
        if permitted:
            yield row, report


def _replies(db: Connection, row: work_state.DirectionRow) -> list[tuple[int, str, bool]]:
    """Only ordinary descendants with exact participants and reversed/forward scopes count."""
    found = cast(
        "list[tuple[int, str, int]]",
        db.execute(
            "WITH RECURSIVE thread(id) AS (SELECT ? UNION "
            "SELECT m.id FROM messages m JOIN thread t ON m.reply_to=t.id "
            "JOIN messages root ON root.id=? "
            "WHERE ((m.sender=root.sender AND m.recipient=root.recipient "
            "AND m.sender_scope IS root.sender_scope "
            "AND m.recipient_scope IS root.recipient_scope) OR "
            "(m.sender=root.recipient AND m.recipient=root.sender "
            "AND m.sender_scope IS root.recipient_scope "
            "AND m.recipient_scope IS root.sender_scope))) "
            "SELECT m.id,m.sent_at,EXISTS(SELECT 1 FROM remote_imports i WHERE i.local_id=m.id) "
            "FROM thread t JOIN messages m ON m.id=t.id "
            "WHERE m.id<>? AND m.sender=? AND m.recipient=? "
            "AND NOT EXISTS(SELECT 1 FROM coordination_messages c WHERE c.message_id=m.id) "
            "ORDER BY m.id",
            (row.message_id, row.message_id, row.message_id, row.recipient, row.sender),
        ).fetchall(),
    )
    return [(message_id, sent_at, bool(remote)) for message_id, sent_at, remote in found]


@dataclass(frozen=True)
class Evidence:
    identity: str
    at: str | None
    observation: str


def _evidence(
    db: Connection, row: work_state.DirectionRow, report: dict[str, object] | None
) -> Evidence:
    replies = _replies(db, row)
    revision = report["revision"] if report else 0
    identity = json.dumps([row.acked, revision, replies[-1][0] if replies else None])
    remote = row.outbox_id is not None or any(reply[2] for reply in replies)
    if remote:
        return Evidence(identity, None, "remote_observed")
    clocks = [cast("str", row.acked)]
    if report is not None:
        clocks.append(cast("str", report["reported_at"]))
    clocks.extend(reply[1] for reply in replies)
    return Evidence(identity, max(clocks, key=timestamp), "local")


def _state(
    db: Connection, project: dict[str, object], message_id: int
) -> tuple[str, str, str, int, int | None] | None:
    return cast(
        "tuple[str, str, str, int, int | None] | None",
        db.execute(
            "SELECT evidence,last_communication_at,observation,epoch,notified_epoch "
            "FROM coordination_stall_state WHERE message_id=? "
            "AND scope=? AND project_id=? AND generation=?",
            (message_id, *_binding(project)),
        ).fetchone(),
    )


def _save(db: Connection, project: dict[str, object], message_id: int, evidence: Evidence) -> None:
    db.execute(
        "INSERT INTO coordination_stall_state "
        "(message_id,scope,project_id,generation,evidence,last_communication_at,observation,epoch) "
        "VALUES (?,?,?,?,?,?,?,1) ON CONFLICT(message_id) DO UPDATE SET "
        "evidence=excluded.evidence,last_communication_at=excluded.last_communication_at,"
        "observation=excluded.observation,epoch=coordination_stall_state.epoch+1 "
        "WHERE coordination_stall_state.evidence<>excluded.evidence",
        (message_id, *_binding(project), evidence.identity, evidence.at, evidence.observation),
    )


def reconcile(db: Connection, project: dict[str, object], *, now: datetime) -> None:
    """Repair derived clocks in caller's transaction; old remote evidence starts now."""
    for row, report in eligible(db, project, now=now):
        evidence = _evidence(db, row, report)
        if evidence.at is None:
            evidence = Evidence(evidence.identity, now.isoformat(), evidence.observation)
        _save(db, project, row.message_id, evidence)


def observe(db: Connection, project: dict[str, object], message_id: int, *, now: datetime) -> None:
    """Capture admitted acknowledgment/report/reply evidence at local acceptance time.

    ``message_id`` is the direction ID or an admitted ordinary worker reply ID.
    Call only after admission and source writes, in the same transaction.
    Identical source identities never advance an epoch.
    """
    for row, report in eligible(db, project, now=now):
        if row.message_id == message_id or any(
            reply[0] == message_id for reply in _replies(db, row)
        ):
            evidence = _evidence(db, row, report)
            _save(
                db,
                project,
                row.message_id,
                Evidence(
                    evidence.identity,
                    now.isoformat(),
                    evidence.observation,
                ),
            )


def projection(  # pylint: disable=too-many-locals
    db: Connection,
    project: dict[str, object],
    address: str,
    *,
    now: datetime,
    threshold_seconds: int | None = None,
) -> dict[str, object]:
    """Read all eligible clocks, including future deadlines, without changing state."""
    require_coordinator(db, project, address)
    seconds = threshold(db, project) if threshold_seconds is None else threshold_seconds
    if isinstance(seconds, bool) or not MIN_THRESHOLD_SECONDS <= seconds <= MAX_THRESHOLD_SECONDS:
        raise BusError("Threshold must be from 1m through 24h")
    items: list[dict[str, object]] = []
    for row, report in eligible(db, project, now=now):
        evidence = _evidence(db, row, report)
        state = _state(db, project, row.message_id)
        current = state is not None and state[0] == evidence.identity
        at = state[1] if current and state else evidence.at
        observation = state[2] if current and state else evidence.observation
        epoch = state[3] + (0 if current else 1) if state else 1
        notified = bool(current and state and state[4] == epoch)
        due = timestamp(at) + timedelta(seconds=seconds) if at else None
        items.append(
            {
                "message_id": row.message_id,
                "recipient": row.recipient,
                "operation": row.operation,
                "last_communication_at": at,
                "due_at": due.isoformat() if due else None,
                "silence_seconds": max(0, int((now - timestamp(at)).total_seconds()))
                if at
                else None,
                "work_status": report["status"] if report else None,
                "epoch": epoch,
                "notified": notified,
                "observation": observation if at else OBSERVATION_PENDING,
            }
        )
    items.sort(
        key=lambda item: (
            item["due_at"] is None,
            timestamp(cast("str", item["due_at"])) if item["due_at"] else now,
            cast("int", item["message_id"]),
        )
    )
    return {
        "project_id": project["project_id"],
        "generation": project["generation"],
        "threshold_seconds": seconds,
        "observed_at": now.isoformat(),
        "items": items,
    }


def inspect(
    db: Connection,
    project: dict[str, object],
    address: str,
    *,
    now: datetime,
    threshold_seconds: int | None = None,
) -> dict[str, object]:
    """Read only due or unknown-baseline directions, ordered by deadline and ID."""
    result = projection(db, project, address, now=now, threshold_seconds=threshold_seconds)
    result["items"] = [
        item
        for item in cast("list[dict[str, object]]", result["items"])
        if item["due_at"] is None or timestamp(cast("str", item["due_at"])) <= now
    ]
    return result
