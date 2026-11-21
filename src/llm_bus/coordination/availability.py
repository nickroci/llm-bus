"""Durable informational local arrivals; these records confer no authority."""

from datetime import UTC
from sqlite3 import Connection
from typing import cast

from llm_bus.coordination.models import Availability, timestamp, uuid4_string
from llm_bus.errors import BusError
from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.store import Store
from llm_bus.mailbox.writes import send_in_transaction
from llm_bus.policy.config import NATIVE, load_config
from llm_bus.policy.fields import IDENTIFIER
from llm_bus.remote.outbox import enqueue_in_transaction

ADDRESS_PARTS = 3


def record_remote(  # noqa: PLR0913 - existing join transaction and exact identity
    db: Connection,
    mailbox: Store,
    record: dict[str, object],
    worker: str,
    grant_id: str,
    *,
    cwd: str,
) -> dict[str, object]:
    """Queue the owner-bound arrival in the caller's join transaction."""
    # pylint: disable=too-many-arguments,too-many-locals,import-outside-toplevel,clean-code-commented-out-code
    # Deferred import avoids the remote admission-to-availability import cycle.
    from llm_bus.remote.store import RemoteStore  # noqa: PLC0415  # pylint: disable=cyclic-import

    config = load_config()
    repo = config.repository(cwd) if config is not None else None
    recipient = str(record["coordinator"])
    parts = recipient.split("/")
    if config is None or repo is None or repo.scope != record["scope"]:
        raise BusError("Remote sending is disabled for this repository")
    if (
        len(parts) != ADDRESS_PARTS
        or parts[0] == config.machine_id
        or parts[1] != repo.repo_id
        or not IDENTIFIER.fullmatch(parts[0])
        or not NATIVE.fullmatch(parts[2])
    ):
        raise BusError("Remote coordinator must belong to another machine in the same repository")
    peer = config.peer(repo.repo_id, parts[0])
    if peer is None or not peer.send_messages:
        raise BusError("Local capability policy: message sending is disabled for this peer")
    previous = cast(
        "tuple[int, str, str] | None",
        db.execute(
            "SELECT a.message_id,COALESCE(o.id,a.remote_id),COALESCE(o.state,'unavailable') "
            "FROM coordination_local_availability a "
            "LEFT JOIN remote_outbox o ON o.local_id=a.message_id WHERE a.grant_id=?",
            (grant_id,),
        ).fetchone(),
    )
    if previous is not None:
        return {"id": previous[0], "remote_id": previous[1], "status": previous[2]}
    kind, session_id = worker.split(":", 1)
    result = enqueue_in_transaction(
        RemoteStore(mailbox, config.connection, db=db),
        db,
        SenderContext(kind, session_id, cwd),
        recipient,
        repo.repo_id,
        f"Worker {worker} joined coordination project {record['label']}. "
        "Availability is informational; recheck current authority before assignments.",
        scope=repo.scope,
        availability=Availability(
            1, str(record["project_id"]), str(record["generation"]), grant_id, "joined"
        ),
    )
    db.execute(
        "INSERT INTO coordination_local_availability(grant_id,message_id,remote_id) VALUES (?,?,?)",
        (grant_id, result["id"], result["remote_id"]),
    )
    return result


def accept(  # noqa: PLR0913 - explicit admission interface
    db: Connection,
    *,
    sender: str,
    recipient: str,
    scope: str,
    event: Availability,
    message_id: int,
    sent_at: str,
    event_id: str,
) -> bool:
    """Observe a remote worker inside policy-approved inbox admission only."""
    # pylint: disable=too-many-arguments,too-many-locals
    event.validate()
    observed_at = timestamp(sent_at)
    uuid4_string(event_id)
    parts = sender.split("/")
    if (
        len(parts) != ADDRESS_PARTS
        or not all(IDENTIFIER.fullmatch(part) for part in parts[:2])
        or not NATIVE.fullmatch(parts[2])
        or not NATIVE.fullmatch(recipient)
    ):
        return False
    binding = cast(
        "tuple[int] | None",
        db.execute(
            "SELECT 1 FROM coordination_projects p JOIN coordination_enrollments e "
            "ON e.scope=p.scope AND e.project_id=p.project_id AND e.generation=p.generation "
            "WHERE p.scope=? AND p.project_id=? AND p.generation=? AND p.coordinator=? "
            "AND p.revoked_at IS NULL AND e.address=? AND e.role='coordinator'",
            (scope, event.project_id, event.generation, recipient, recipient),
        ).fetchone(),
    )
    if binding is None:
        return False
    if (
        db.execute(
            "SELECT 1 FROM coordination_availability_events WHERE sender=? AND grant_id=?",
            (sender, event.grant_id),
        ).fetchone()
        is not None
    ):
        return False
    prior = cast(
        "list[tuple[str, str, str]]",
        db.execute(
            "SELECT sent_at,event_id,grant_id FROM coordination_availability_events "
            "WHERE sender=? AND recipient=? AND scope=? AND project_id=? AND generation=? "
            "ORDER BY sent_at DESC,event_id DESC LIMIT 1",
            (sender, recipient, scope, event.project_id, event.generation),
        ).fetchall(),
    )
    if any((observed_at, event_id) <= (timestamp(row[0]), row[1]) for row in prior):
        return False
    db.execute(
        "INSERT INTO coordination_availability_events "
        "(message_id,grant_id,sender,sent_at,event_id,recipient,scope,project_id,generation) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            message_id,
            event.grant_id,
            sender,
            observed_at.astimezone(UTC).isoformat(timespec="microseconds"),
            event_id,
            recipient,
            scope,
            event.project_id,
            event.generation,
        ),
    )
    db.execute(
        "INSERT OR REPLACE INTO coordination_targets VALUES (?,?,?,?,?)",
        (scope, event.project_id, sender, event.grant_id, event.generation),
    )
    return True


def record_local(
    db: Connection, record: dict[str, object], worker: str, grant_id: str
) -> int | None:
    """Caller owns the validated join transaction, including notice deduplication."""
    if (
        db.execute(
            "SELECT 1 FROM coordination_local_availability WHERE grant_id=?", (grant_id,)
        ).fetchone()
        is not None
    ):
        return None
    message = send_in_transaction(
        db,
        worker,
        str(record["coordinator"]),
        f"Worker {worker} joined coordination project {record['label']}. "
        "Availability is informational; recheck current authority before assignments.",
    )
    db.execute(
        "INSERT INTO coordination_local_availability(grant_id,message_id) VALUES (?,?)",
        (grant_id, message["id"]),
    )
    return message["id"]
