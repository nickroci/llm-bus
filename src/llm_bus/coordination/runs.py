"""Local unattended run lifetime. Callers own the mailbox transaction.

Run authority supplements existing delegation and consent; it never replaces them.
Every consumer must call ``require_active`` in its action transaction, including
after waits. Stored status alone is history, never proof of current authority.
"""

from __future__ import annotations

# pylint: disable=clean-code-commented-out-code
import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from llm_bus.coordination import amendments, delegation, store
from llm_bus.coordination.models import ticket_text, timestamp
from llm_bus.errors import BusError
from llm_bus.mailbox.agents import expire_idle_agents
from llm_bus.mailbox.models import is_local_address

if TYPE_CHECKING:
    from sqlite3 import Connection

MAX_TICKETS = 128
MAX_TEXT = 4096
CONTROL_BOUNDARY = 32
ACTIVE = "active"
NO_ACTIVE_RUN = "Unattended authority denied: no active run"
TICKET_OUTSIDE_SCOPE = "Unattended authority denied: ticket outside approved scope"
PERMISSION_OUTSIDE_SCOPE = "Unattended authority denied: permission outside approved scope"
NO_VERIFIED_OWNER_TURN = (
    "Unattended activation needs a verified owner turn. A recognized wake or an unverified "
    "prompt cannot activate a run. Retry after a direct owner "
    "prompt, or use owner terminal setup: `llm-bus coordinate PROJECT --setup` for "
    "a new project or `llm-bus coordination unattended PROJECT --setup` for an "
    "existing project."
)
COLUMNS = (
    "run_id,scope,project_id,generation,coordinator,objective_revision,objective,"
    "tickets_json,completion,expires_at,permissions_json,started_at,ended_at,end_reason"
)


def create_tables(db: Connection) -> None:
    """Add history without granting authority to existing projects."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS coordination_runs ("
        "run_id TEXT PRIMARY KEY,scope TEXT NOT NULL,project_id TEXT NOT NULL,"
        "generation TEXT NOT NULL,coordinator TEXT NOT NULL,objective_revision TEXT NOT NULL,"
        "objective TEXT NOT NULL,tickets_json TEXT NOT NULL,completion TEXT NOT NULL,"
        "expires_at TEXT NOT NULL,permissions_json TEXT NOT NULL,started_at TEXT NOT NULL,"
        "ended_at TEXT,end_reason TEXT)"
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS coordination_one_open_run "
        "ON coordination_runs(scope,project_id) WHERE ended_at IS NULL"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS coordination_run_proofs "
        "(run_id TEXT PRIMARY KEY,kind TEXT NOT NULL)"
    )


def _project(db: Connection, record: dict[str, object]) -> dict[str, object]:
    """Refresh by immutable repository/project key, never trust a cached generation."""
    cursor = db.execute(
        "SELECT * FROM coordination_projects WHERE scope=? AND project_id=?",
        (record["scope"], record["project_id"]),
    )
    row = cast("tuple[object, ...] | None", cursor.fetchone())
    if row is None:
        raise BusError("Unattended authority denied: unknown project")
    return dict(zip((column[0] for column in cursor.description), row, strict=True))


def objective_revision(db: Connection, record: dict[str, object]) -> str:
    """Digest includes amendment identity, so even a repeated objective ends the run."""
    history = amendments.history(db, record)
    return revision_digest(
        str(record["objective"]), [(row["amendment_id"], row["sha256"]) for row in history]
    )


def revision_digest(objective: str, changes: list[tuple[object, object]]) -> str:
    """Same exact revision for initial setup preview and persisted project history."""
    revision = [objective, changes]
    return hashlib.sha256(json.dumps(revision, separators=(",", ":")).encode()).hexdigest()


def require_owner_origin(db: Connection, caller: str | None, *, terminal: bool = False) -> None:
    """Procedural same-user check: wakes deny; unknown host context needs owner terminal."""
    row = (
        None
        if caller is None
        else db.execute(
            "SELECT turn FROM coordination_owner_turns WHERE address=?", (caller,)
        ).fetchone()
    )
    if (not terminal and row is None) or (row is not None and row[0] <= 0):
        raise BusError(NO_VERIFIED_OWNER_TURN)


def _local_coordinator(db: Connection, record: dict[str, object], *, enrolled: bool) -> bool:
    expire_idle_agents(db)
    address = str(record["coordinator"])
    if not is_local_address(address):
        return False
    active = cast(
        "tuple[int] | None",
        db.execute(
            "SELECT 1 FROM agents WHERE address=? AND scope=? AND ended_at IS NULL",
            (address, record["scope"]),
        ).fetchone(),
    )
    if active is None:
        return False
    return (
        not enrolled
        or db.execute(
            "SELECT 1 FROM coordination_enrollments WHERE scope=? AND project_id=? "
            "AND address=? AND role='coordinator' AND generation=?",
            (record["scope"], record["project_id"], address, record["generation"]),
        ).fetchone()
        is not None
    )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        raise BusError(f"Unattended {name} must be nonempty text (at most 4096 characters)")
    if any(ord(char) < CONTROL_BOUNDARY for char in value):
        raise BusError(f"Invalid unattended {name}")
    return value


def scope_selection(
    db: Connection, record: dict[str, object], args: dict[str, object], *, now: datetime
) -> dict[str, object]:
    """Shared preview/activation validator; permissions can only narrow project delegation."""
    objective = _text(args.get("objective"), "objective")
    if objective != amendments.effective_objective(db, record):
        raise BusError("Unattended objective must match the current objective revision")
    tickets = args.get("ticket")
    if not isinstance(tickets, list):
        raise BusError("Unattended scope needs 1 to 128 explicit tickets")
    listed = cast("list[object]", tickets)
    if not 1 <= len(listed) <= MAX_TICKETS:
        raise BusError("Unattended scope needs 1 to 128 explicit tickets")
    selected = [ticket_text(ticket) for ticket in listed]
    if len(selected) != len(set(selected)) or any(
        not ticket.strip() or ticket != ticket.strip() or any(char in ticket for char in "*?[]")
        for ticket in selected
    ):
        raise BusError("Unattended tickets must be unique and finite; no wildcards")
    expires_at = _text(args.get("expires_at"), "expiry")
    if timestamp(expires_at) <= now:
        raise BusError("Unattended expiry must be in the future")
    project = delegation.project_delegation(db, record)
    permissions = args.get("permission")
    if project is None or not isinstance(permissions, list) or not permissions:
        raise BusError("Unattended run needs explicit delegated permissions")
    if any(not isinstance(item, str) for item in cast("list[object]", permissions)):
        raise BusError("Invalid unattended permission")
    chosen = project.narrow(cast("list[str]", permissions))
    proof = args.get("completion_proof") or "reviewed-pr"
    if proof not in {"reviewed-pr", "merged-pr"}:
        raise BusError("Unsupported unattended completion proof")
    if proof == "merged-pr" and not any(item.startswith("merge:") for item in chosen.items()):
        raise BusError("Merged-PR completion requires a selected merge permission")
    return {
        "objective": objective,
        "objective_revision": objective_revision(db, record),
        "tickets": selected,
        "completion": _text(args.get("completion"), "completion condition"),
        "completion_proof": proof,
        "expires_at": timestamp(expires_at).isoformat(),
        "permissions": chosen.wire(),
    }


def _rows(db: Connection, record: dict[str, object]) -> list[dict[str, object]]:
    cursor = db.execute(
        f"SELECT {COLUMNS} FROM coordination_runs "  # noqa: S608 - fixed columns
        "WHERE scope=? AND project_id=? ORDER BY rowid",
        (record["scope"], record["project_id"]),
    )
    return [
        dict(zip((c[0] for c in cursor.description), row, strict=True))
        for row in cast("list[tuple[object, ...]]", cursor.fetchall())
    ]


def _end_reason(
    db: Connection, record: dict[str, object], run: dict[str, object], now: datetime
) -> str | None:
    if record["revoked_at"] is not None:
        return "project_revoked"
    if (run["generation"], run["coordinator"]) != (record["generation"], record["coordinator"]):
        return "generation_changed"
    if run["objective_revision"] != objective_revision(db, record):
        return "objective_changed"
    if timestamp(str(run["expires_at"])) <= now:
        return "expired"
    project = delegation.project_delegation(db, record)
    if project is None or not project.covers(
        delegation.Delegation.load(str(run["permissions_json"]))
    ):
        return "permissions_changed"
    return None


def reconcile(db: Connection, record: dict[str, object], *, now: datetime) -> None:
    """Persist terminal lifetime reasons; safe to call on every status/resume."""
    current = _project(db, record)
    for run in _rows(db, current):
        if run["ended_at"] is None and (reason := _end_reason(db, current, run, now)):
            db.execute(
                "UPDATE coordination_runs SET ended_at=?,end_reason=? "
                "WHERE run_id=? AND ended_at IS NULL",
                (now.isoformat(), reason, run["run_id"]),
            )


def _public(run: dict[str, object]) -> dict[str, object]:
    result = dict(run)
    result["tickets"] = json.loads(str(result.pop("tickets_json")))
    result["permissions"] = json.loads(str(result.pop("permissions_json")))
    result[ACTIVE] = result["ended_at"] is None
    return result


def activate(  # noqa: PLR0913 - explicit run and owner context
    db: Connection,
    record: dict[str, object],
    args: dict[str, object],
    caller: str | None,
    *,
    terminal: bool = False,
    now: datetime | None = None,
) -> dict[str, object]:
    """Explicit owner activation/renewal; the caller holds BEGIN IMMEDIATE."""
    # pylint: disable=too-many-arguments
    now = now or datetime.now(UTC)
    create_tables(db)
    record = _project(db, record)
    require_owner_origin(db, caller, terminal=terminal)
    if (
        not _local_coordinator(db, record, enrolled=not terminal)
        or record["revoked_at"] is not None
    ):
        raise BusError("Unattended runs require an unrevoked local coordinator")
    if caller is not None and caller != record["coordinator"]:
        raise BusError("Only the project's coordinator may activate unattended authority")
    if store.worker_of_generation(db, record, str(record["coordinator"])):
        raise BusError("A project worker cannot activate unattended authority")
    if args.get("confirm") is not True:
        raise BusError("Review unattended scope and explicitly confirm activation")
    chosen = scope_selection(db, record, args, now=now)
    reconcile(db, record, now=now)
    db.execute(
        "UPDATE coordination_runs SET ended_at=?,end_reason='superseded' "
        "WHERE scope=? AND project_id=? AND ended_at IS NULL",
        (now.isoformat(), record["scope"], record["project_id"]),
    )
    run_id = str(uuid4())
    db.execute(
        "INSERT INTO coordination_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
        (
            run_id,
            record["scope"],
            record["project_id"],
            record["generation"],
            record["coordinator"],
            chosen["objective_revision"],
            chosen["objective"],
            json.dumps(chosen["tickets"]),
            chosen["completion"],
            chosen["expires_at"],
            json.dumps(chosen["permissions"]),
            now.isoformat(),
        ),
    )
    db.execute(
        "INSERT INTO coordination_run_proofs VALUES (?,?)", (run_id, chosen["completion_proof"])
    )
    return _public(_rows(db, record)[-1])


def require_active(  # noqa: PLR0913 - explicit action-time bindings
    db: Connection,
    record: dict[str, object],
    caller: str,
    *,
    run_id: str | None = None,
    ticket: str | None = None,
    permission: str | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Authoritative coordinator read for T2/T3; denies stale cached project/run bindings.

    Worker actions must additionally verify their existing grant, consent, exact claim,
    actor and action binding at consumption. This API grants no worker authority.
    """
    # pylint: disable=too-many-arguments
    now = now or datetime.now(UTC)
    current = _project(db, record)
    if caller != current["coordinator"] or not _local_coordinator(db, current, enrolled=True):
        raise BusError("Unattended authority denied: not the enrolled coordinator")
    if "/" in caller or current["generation"] != record["generation"]:
        raise BusError("Unattended authority denied: stale or remote coordinator")
    rows = _rows(db, current)
    run = next((row for row in reversed(rows) if row["ended_at"] is None), None)
    if run is None or _end_reason(db, current, run, now) is not None:
        raise BusError(NO_ACTIVE_RUN)
    if run_id is not None and run_id != run["run_id"]:
        raise BusError("Unattended authority denied: wrong run")
    result = _public(run)
    if ticket is not None and ticket not in cast("list[str]", result["tickets"]):
        raise BusError(TICKET_OUTSIDE_SCOPE)
    permitted = delegation.Delegation.load(str(run["permissions_json"]))
    if permission is not None and permission not in permitted.items():
        raise BusError(PERMISSION_OUTSIDE_SCOPE)
    return result


def require_history(
    db: Connection, record: dict[str, object], caller: str, run_id: str, ticket: str
) -> None:
    """Permit status-only observation of an original run, never new execution."""
    current = _project(db, record)
    if caller != current["coordinator"] or not _local_coordinator(db, current, enrolled=True):
        raise BusError("Run history denied: not the enrolled coordinator")
    run = next((row for row in _rows(db, current) if row["run_id"] == run_id), None)
    if run is None or (run["generation"], run["coordinator"]) != (current["generation"], caller):
        raise BusError("Run history denied: wrong project, generation or coordinator")
    if ticket not in cast("list[str]", _public(run)["tickets"]):
        raise BusError(TICKET_OUTSIDE_SCOPE)


def finish(
    db: Connection, record: dict[str, object], caller: str, run_id: str, *, reason: str
) -> dict[str, object]:
    """T4 completion transition or explicit revocation; no renewal or permission expansion."""
    if reason not in {"completed", "revoked"}:
        raise BusError("Invalid unattended end reason")
    current = _project(db, record)
    if caller != current["coordinator"] or not _local_coordinator(db, current, enrolled=True):
        raise BusError("Only the enrolled coordinator may end an unattended run")
    runs = [run for run in _rows(db, current) if run["run_id"] == run_id]
    if not runs:
        raise BusError("Unknown unattended run")
    if reason == "completed":
        require_active(db, current, caller, run_id=run_id)
    db.execute(
        "UPDATE coordination_runs SET ended_at=?,end_reason=? WHERE run_id=? AND ended_at IS NULL",
        (datetime.now(UTC).isoformat(), reason, run_id),
    )
    return _public(next(run for run in _rows(db, current) if run["run_id"] == run_id))


def status(db: Connection, record: dict[str, object], caller: str) -> dict[str, object]:
    """Only the current enrolled coordinator receives scope and history."""
    current = _project(db, record)
    if caller != current["coordinator"] or not _local_coordinator(db, current, enrolled=True):
        return {ACTIVE: False}
    reconcile(db, current, now=datetime.now(UTC))
    history = [_public(run) for run in _rows(db, current)]
    create_tables(db)
    # Deferred import keeps lifetime checks available to recovery's action validators.
    # pylint: disable=import-outside-toplevel,cyclic-import
    from llm_bus.coordination.recovery import summary  # noqa: PLC0415 - lifetime/recovery cycle

    for run in history:
        proof = cast(
            "tuple[str] | None",
            db.execute(
                "SELECT kind FROM coordination_run_proofs WHERE run_id=?", (run["run_id"],)
            ).fetchone(),
        )
        run["completion_proof"] = "reviewed-pr" if proof is None else str(proof[0])
        run["summary"] = summary(db, current, run)
    active = next((run for run in reversed(history) if run[ACTIVE]), None)
    return {ACTIVE: active is not None, "run": active, "history": history}
