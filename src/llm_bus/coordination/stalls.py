"""Local, durable communication clocks for currently authorized open directions.

Inspection is pure. Writers call ``observe`` after admitted evidence, inside their
transaction; recovery calls ``reconcile``. Neither path creates notices or wakes.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from llm_bus.coordination import store, work_state
from llm_bus.coordination.models import timestamp
from llm_bus.errors import BusError
from llm_bus.hosts.sessions import is_subagent
from llm_bus.locks import notices
from llm_bus.locks.timer import TIMER_POLL_SECONDS, TIMER_START_SECONDS, spawn_checked
from llm_bus.mailbox.agents import SESSION_IDLE_HOURS
from llm_bus.policy.config import optional_config

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from sqlite3 import Connection

    from llm_bus.mailbox.models import Message
    from llm_bus.mailbox.store import Store

DEFAULT_THRESHOLD_SECONDS = 1800
MIN_THRESHOLD_SECONDS = 60
MAX_THRESHOLD_SECONDS = 86400
OPERATIONS = ("assign", "review")
REMOTE_ADDRESS_PARTS = 3
OBSERVATION_PENDING = "observation_pending"
MINUTE_SECONDS = 60
HOUR_SECONDS = 3600
TIMER_LEASE_SECONDS = TIMER_POLL_SECONDS + 2 * TIMER_START_SECONDS


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
        "lease_until TEXT NOT NULL, due_at TEXT NOT NULL, PRIMARY KEY(scope,project_id,generation))"
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


def require_coordinator(
    db: Connection, project: dict[str, object], address: str, *, now: datetime | None = None
) -> None:
    """Check current local enrollment without lazily expiring or updating sessions."""
    observed_at = datetime.now(UTC) if now is None else now
    session = cast(
        "tuple[str] | None",
        db.execute(
            "SELECT a.last_seen FROM coordination_projects p JOIN coordination_enrollments e "
            "ON e.scope=p.scope AND e.project_id=p.project_id AND e.generation=p.generation "
            "JOIN agents a ON a.address=e.address "
            "WHERE p.scope=? AND p.project_id=? AND p.generation=? AND p.revoked_at IS NULL "
            "AND p.coordinator=? AND e.address=? AND e.role='coordinator' "
            "AND a.scope=p.scope AND a.ended_at IS NULL",
            (*_binding(project), address, address),
        ).fetchone(),
    )
    if (
        "/" in address
        or session is None
        or timestamp(session[0]) <= observed_at - timedelta(hours=SESSION_IDLE_HOURS)
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
        "tuple[str, str | None, str] | None",
        db.execute(
            "SELECT g.operations_json,g.expires_at,a.last_seen FROM coordination_grants g "
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
    operations_json, expires_at, last_seen = grant
    operations = cast("object", json.loads(operations_json))
    return (
        isinstance(operations, list)
        and row.operation in operations
        and (expires_at is None or timestamp(expires_at) > now)
        and timestamp(last_seen) > now - timedelta(hours=SESSION_IDLE_HOURS)
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
        require_coordinator(db, project, address, now=now)
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
            "AND NOT EXISTS(SELECT 1 FROM coordination_availability_events a "
            "WHERE a.message_id=m.id) "
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
    revision = cast("int", report["revision"]) if report else 0
    identity = json.dumps([row.acked, revision, replies[-1][0] if replies else None])
    remote = row.outbox_id is not None
    if (remote and (revision > 0 or bool(replies))) or any(reply[2] for reply in replies):
        return Evidence(identity, None, "remote_observed")
    clocks = [cast("str", row.acked)]
    if report is not None and not remote:
        clocks.append(cast("str", report["reported_at"]))
    clocks.extend(reply[1] for reply in replies)
    return Evidence(identity, max(clocks, key=timestamp), "remote_observed" if remote else "local")


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
                    evidence.at or now.isoformat(),
                    evidence.observation,
                ),
            )


def observe_message(db: Connection, message_id: int, *, now: datetime) -> None:
    """Observe admitted evidence for projects in its local participant scopes."""
    bindings = cast(
        "list[tuple[str, str]]",
        db.execute(
            "SELECT scope,label FROM coordination_projects WHERE scope IN "
            "(SELECT sender_scope FROM messages WHERE id=? UNION "
            "SELECT recipient_scope FROM messages WHERE id=?)",
            (message_id, message_id),
        ).fetchall(),
    )
    for scope, label in bindings:
        observe(db, store.project(db, scope, label), message_id, now=now)


def projection(  # pylint: disable=too-many-locals
    db: Connection,
    project: dict[str, object],
    address: str,
    *,
    now: datetime,
    threshold_seconds: int | None = None,
) -> dict[str, object]:
    """Read all eligible clocks, including future deadlines, without changing state."""
    require_coordinator(db, project, address, now=now)
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


def _items(db: Connection, record: dict[str, object], now: datetime) -> list[dict[str, object]]:
    if is_subagent(str(record["coordinator"])):
        return []
    reconcile(db, record, now=now)
    try:
        result = projection(db, record, str(record["coordinator"]), now=now)
    except BusError:
        return []
    return [
        item for item in cast("list[dict[str, object]]", result["items"]) if not item["notified"]
    ]


def apply_due(
    mailbox: Store, db: Connection, record: dict[str, object], *, now: datetime
) -> list[Message]:
    """Persist informational self-notices and epoch markers in caller's write transaction."""
    messages: list[Message] = []
    for item in _items(db, record, now):
        if timestamp(str(item["due_at"])) > now:
            continue
        coordinator = str(record["coordinator"])
        body = (
            f"llm-bus coordination silence: project {record['label']!r}, "
            f"direction #{item['message_id']} to {item['recipient']}; "
            f"last recorded communication {item['last_communication_at']}, "
            f"interval {threshold(db, record)} seconds. "
            f"Inspect with `llm-bus coordination stalls {record['label']}` "
            "(pass the project label as one literal argument). "
            "Silence is informational, not proof of failure or progress. "
            "Recheck current authority and evidence before deciding any action; "
            "this notice does not approve, retry, cancel, reassign or resume work."
        )
        sent = mailbox.send_in_transaction(db, coordinator, coordinator, body, cross_folder=True)
        db.execute(
            "UPDATE coordination_stall_state SET notified_epoch=?,notice_message_id=? "
            "WHERE message_id=?",
            (item["epoch"], sent["id"], item["message_id"]),
        )
        messages.append(sent)
    return messages


def _release(db: Connection, record: dict[str, object], token: str) -> None:
    db.execute(
        "DELETE FROM coordination_stall_timers "
        "WHERE scope=? AND project_id=? AND generation=? AND token=?",
        (*_binding(record), token),
    )


def _claim(db: Connection, record: dict[str, object], now: datetime) -> str | None:
    pending = _items(db, record, now)
    if not pending:
        db.execute(
            "DELETE FROM coordination_stall_timers WHERE scope=? AND project_id=? AND generation=?",
            _binding(record),
        )
        return None
    due_at = str(pending[0]["due_at"])
    claim = cast(
        "tuple[str, str] | None",
        db.execute(
            "SELECT lease_until,due_at FROM coordination_stall_timers "
            "WHERE scope=? AND project_id=? AND generation=?",
            _binding(record),
        ).fetchone(),
    )
    if claim and timestamp(claim[0]) > now and timestamp(claim[1]) <= timestamp(due_at):
        return None
    token = uuid4().hex
    db.execute(
        "INSERT INTO coordination_stall_timers VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(scope,project_id,generation) DO UPDATE SET "
        "token=excluded.token,lease_until=excluded.lease_until,due_at=excluded.due_at",
        (
            *_binding(record),
            token,
            (now + timedelta(seconds=TIMER_LEASE_SECONDS)).isoformat(),
            due_at,
        ),
    )
    return token


def schedule(mailbox: Store, record: dict[str, object], *, now: datetime | None = None) -> None:
    """Arrange deadlines after commit; bookkeeping itself never delivers a wake."""
    moment = datetime.now(UTC) if now is None else now
    with mailbox.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        token = _claim(db, record, moment)
    if token is None:
        return
    started = spawn_checked(
        "coordination",
        "stall-timer",
        str(record["project_id"]),
        "--scope",
        str(record["scope"]),
        "--generation",
        str(record["generation"]),
        "--token",
        token,
        database=mailbox.path,
    )
    if not started:
        with mailbox.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            _release(db, record, token)


def schedule_messages(mailbox: Store, message_ids: list[int]) -> None:
    """Rearm locally owned projects affected by committed message evidence."""
    records: dict[tuple[object, object, object], dict[str, object]] = {}
    with mailbox.transaction() as db:
        for message_id in message_ids:
            bindings = cast(
                "list[tuple[str, str]]",
                db.execute(
                    "SELECT scope,label FROM coordination_projects WHERE scope IN "
                    "(SELECT sender_scope FROM messages WHERE id=? UNION "
                    "SELECT recipient_scope FROM messages WHERE id=?)",
                    (message_id, message_id),
                ).fetchall(),
            )
            for scope, label in bindings:
                record = store.project(db, scope, label)
                records[_binding(record)] = record
    for record in records.values():
        schedule(mailbox, record)


def schedule_remote(mailbox: Store, remote_id: str) -> None:
    with mailbox.transaction() as db:
        row = cast(
            "tuple[int] | None",
            db.execute("SELECT local_id FROM remote_outbox WHERE id=?", (remote_id,)).fetchone(),
        )
    if row:
        schedule_messages(mailbox, [row[0]])


def apply_for_coordinator(mailbox: Store, address: str, *, now: datetime | None = None) -> None:
    """Recover due notices and missing timers only for the current main coordinator."""
    if is_subagent(address):
        return
    moment = datetime.now(UTC) if now is None else now
    with mailbox.transaction() as db:
        bindings = cast(
            "list[tuple[str, str]]",
            db.execute(
                "SELECT p.scope,p.label FROM coordination_projects p "
                "JOIN coordination_enrollments e "
                "ON e.scope=p.scope AND e.project_id=p.project_id AND e.generation=p.generation "
                "WHERE p.coordinator=? AND e.address=? AND e.role='coordinator'",
                (address, address),
            ).fetchall(),
        )
    for scope, label in bindings:
        with mailbox.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            record = store.project(db, scope, label)
            try:
                require_coordinator(db, record, address, now=moment)
            except BusError:
                continue
            messages = apply_due(mailbox, db, record, now=moment)
        notices.deliver(mailbox, messages)
        schedule(mailbox, record, now=moment)


def run_timer(  # noqa: PLR0913 - exact timer binding plus injectable clock/sleep
    mailbox: Store,
    scope: str,
    project_id: str,
    generation: str,
    token: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Check ownership/lifecycle at bounded intervals, persist due notices, then exit when idle."""
    # pylint: disable=too-many-arguments
    while True:
        moment = clock()
        with mailbox.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            row = cast(
                "tuple[str, str] | None",
                db.execute(
                    "SELECT p.label,t.lease_until FROM coordination_stall_timers t "
                    "JOIN coordination_projects p ON p.scope=t.scope AND p.project_id=t.project_id "
                    "AND p.generation=t.generation WHERE t.scope=? AND t.project_id=? "
                    "AND t.generation=? AND t.token=?",
                    (scope, project_id, generation, token),
                ).fetchone(),
            )
            if row is None:
                db.execute(
                    "DELETE FROM coordination_stall_timers WHERE scope=? AND project_id=? "
                    "AND generation=? AND token=?",
                    (scope, project_id, generation, token),
                )
                return
            record = store.project(db, scope, row[0])
            if timestamp(row[1]) <= moment:
                _release(db, record, token)
                return
            messages = apply_due(mailbox, db, record, now=moment)
            pending = _items(db, record, moment)
            if pending:
                remaining = (timestamp(str(pending[0]["due_at"])) - moment).total_seconds()
                db.execute(
                    "UPDATE coordination_stall_timers SET lease_until=?,due_at=? "
                    "WHERE scope=? AND project_id=? AND generation=? AND token=?",
                    (
                        (moment + timedelta(seconds=TIMER_LEASE_SECONDS)).isoformat(),
                        pending[0]["due_at"],
                        *_binding(record),
                        token,
                    ),
                )
            else:
                remaining = None
                _release(db, record, token)
        notices.deliver(mailbox, messages)
        if remaining is None:
            return
        sleep(min(max(0, remaining), TIMER_POLL_SECONDS))
