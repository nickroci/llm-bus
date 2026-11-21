"""Single-operation consent records; never reads a secret or invokes a provider.

Callers hold the mailbox IMMEDIATE transaction. Reservation is irreversible: an
interruption requires provider reconciliation and fresh consent for another attempt.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from sqlite3 import IntegrityError
from typing import TYPE_CHECKING, cast

from llm_bus.coordination import delegation, run_actions, runs, secret_policy
from llm_bus.coordination.authority import authorize, project_execution_allowed
from llm_bus.coordination.models import Direction, timestamp, uuid4_string
from llm_bus.errors import BusError

if TYPE_CHECKING:
    from sqlite3 import Connection

FIELDS = frozenset(
    {
        "version",
        "receipt_id",
        "owner",
        "repository",
        "project",
        "run",
        "generation",
        "ticket",
        "actor_machine",
        "actor_session",
        "branch",
        "claimed_at",
        "provider",
        "account",
        "provider_project",
        "target",
        "secret",
        "operation",
        "source_ref",
        "plan_sha256",
        "expires_at",
        "grant_id",
    }
)
IDENTIFIER = re.compile(r"[A-Za-z0-9/][A-Za-z0-9_.:/@#+=-]{0,511}\Z")
STATUSES = frozenset({"succeeded", "denied", "not_submitted", "unknown", "fresh_approval_required"})
DENIED = secret_policy.DENIED
MAX_PLAN_BYTES = 32768


def canonical(value: dict[str, str]) -> bytes:
    """The owner signs UTF-8 JSON with sorted keys and no whitespace or trailing LF."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def parse(value: object) -> dict[str, str]:  # noqa: C901 - closed signed payload validation
    if not isinstance(value, dict):
        raise BusError(DENIED)
    fields = cast("dict[str, object]", value)
    if frozenset(fields) != FIELDS:
        raise BusError(DENIED)
    if not all(isinstance(item, str) for item in fields.values()):
        raise BusError(DENIED)
    payload = cast("dict[str, str]", fields)
    for name, item in payload.items():
        if name not in {"claimed_at", "expires_at", "source_ref"} and not IDENTIFIER.fullmatch(
            item
        ):
            raise BusError(DENIED)
    if payload["version"] != "1" or payload["operation"] not in {"create", "replace", "delete"}:
        raise BusError(DENIED)
    for name in ("receipt_id", "project", "run", "generation", "grant_id"):
        uuid4_string(payload[name])
    if not re.fullmatch(r"[a-f0-9]{64}", payload["plan_sha256"]):
        raise BusError(DENIED)
    source = payload["source_ref"]
    if payload["operation"] == "delete":
        if source:
            raise BusError(DENIED)
    elif not IDENTIFIER.fullmatch(source) or "#" not in source:
        raise BusError(DENIED)
    timestamp(payload["claimed_at"])
    timestamp(payload["expires_at"])
    return payload


def create_table(db: Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS coordination_dev_secrets ("
        "receipt_id TEXT PRIMARY KEY,scope TEXT NOT NULL,project_id TEXT NOT NULL,"
        "payload TEXT NOT NULL,signature TEXT NOT NULL,status TEXT NOT NULL,"
        "reserved_at TEXT NOT NULL,checked_at TEXT,ended_at TEXT)"
    )


def _current(  # noqa: PLR0913,PLR0917 - exact authenticated consent context
    db: Connection,
    record: dict[str, object],
    actor: str,
    payload: dict[str, str],
    plan: bytes,
    signature: str,
) -> None:
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    now = datetime.now(UTC)
    if (
        payload["repository"] != record["scope"]
        or payload["project"] != record["project_id"]
        or payload["generation"] != record["generation"]
        or payload["actor_session"] != actor
    ):
        raise BusError(DENIED)
    if (
        hashlib.sha256(plan).hexdigest() != payload["plan_sha256"]
        or timestamp(payload["expires_at"]) <= now
    ):
        raise BusError(DENIED)
    secret_policy.verify(payload, canonical(payload), signature)
    now = datetime.now(UTC)
    if timestamp(payload["expires_at"]) <= now:
        raise BusError(DENIED)
    active = runs.require_active(
        db,
        record,
        str(record["coordinator"]),
        run_id=payload["run"],
        ticket=payload["ticket"],
        permission="project-execution",
        now=now,
    )
    if timestamp(payload["expires_at"]) > timestamp(str(active["expires_at"])):
        raise BusError(DENIED)
    if not project_execution_allowed(db, record, actor, scope=str(record["scope"]), now=now):
        raise BusError(DENIED)
    held = delegation.effective(db, record, payload["grant_id"], now=now)
    if held is None or not held.project_execution:
        raise BusError(DENIED)
    direction = Direction(
        1,
        payload["project"],
        payload["generation"],
        payload["grant_id"],
        "assign",
        payload["run"],
        payload["ticket"],
        payload["branch"],
        payload["claimed_at"],
    )
    if not authorize(
        db,
        sender=str(record["coordinator"]),
        recipient=actor,
        scope=str(record["scope"]),
        direction=direction,
        now=now,
    ).verified:
        raise BusError(DENIED)
    if run_actions.claim(db, record, actor, payload["ticket"]) != (
        payload["branch"],
        payload["claimed_at"],
    ):
        raise BusError(DENIED)


def reserve(  # noqa: PLR0913,PLR0917 - exact authenticated consent context
    db: Connection,
    record: dict[str, object],
    actor: str,
    payload: dict[str, str],
    plan: bytes,
    signature: str,
) -> dict[str, object]:
    """Reserve before returning a record-bound action. Duplicate IDs never issue twice."""
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    payload = parse(payload)
    _current(db, record, actor, payload, plan, signature)
    create_table(db)
    try:
        db.execute(
            "INSERT INTO coordination_dev_secrets VALUES (?,?,?,?,?,?,?,NULL,NULL)",
            (
                payload["receipt_id"],
                record["scope"],
                record["project_id"],
                canonical(payload).decode(),
                signature,
                "reserved",
                datetime.now(UTC).isoformat(),
            ),
        )
    except IntegrityError as exc:
        raise BusError(
            "Development secret receipt already reserved; reconcile before fresh approval"
        ) from exc
    return {"receipt_id": payload["receipt_id"], "status": "reserved", "action": payload}


def _stored(
    db: Connection,
    record: dict[str, object],
    actor: str,
    receipt_id: str,
) -> tuple[dict[str, str], str, str]:
    create_table(db)
    row = cast(
        "tuple[str, str, str] | None",
        db.execute(
            "SELECT payload,signature,status FROM coordination_dev_secrets "
            "WHERE receipt_id=? AND scope=? AND project_id=?",
            (receipt_id, record["scope"], record["project_id"]),
        ).fetchone(),
    )
    if row is None:
        raise BusError(DENIED)
    payload = parse(cast("object", json.loads(row[0])))
    if payload["actor_session"] != actor:
        raise BusError(DENIED)
    return payload, row[1], row[2]


def check(
    db: Connection,
    record: dict[str, object],
    actor: str,
    receipt_id: str,
    plan: bytes,
) -> dict[str, object]:
    """One action-time recheck. A crash after this point must never auto-repeat."""
    payload, signature, status = _stored(db, record, actor, receipt_id)
    if status != "reserved":
        raise BusError(
            "Development secret action already checked; reconcile and obtain fresh approval"
        )
    _current(db, record, actor, payload, plan, signature)
    db.execute(
        "UPDATE coordination_dev_secrets SET status='unknown',checked_at=? WHERE receipt_id=?",
        (datetime.now(UTC).isoformat(), receipt_id),
    )
    return {"receipt_id": receipt_id, "status": "ready", "action": payload}


def outcome(
    db: Connection,
    record: dict[str, object],
    actor: str,
    receipt_id: str,
    status: str,
) -> dict[str, object]:
    """Retain only bounded untrusted provider status; it never grants retry authority."""
    _, _, previous = _stored(db, record, actor, receipt_id)
    if status not in STATUSES or previous not in {"reserved", "unknown"}:
        raise BusError(DENIED)
    if previous == "reserved" and status == "succeeded":
        raise BusError(DENIED)
    db.execute(
        "UPDATE coordination_dev_secrets SET status=?,ended_at=? WHERE receipt_id=?",
        (status, datetime.now(UTC).isoformat(), receipt_id),
    )
    # This bounded wire field is an unconditional refusal, never a retry permission.
    return {"receipt_id": receipt_id, "status": status, "retry_allowed": False}  # pylint: disable=clean-code-business-policy-literal
