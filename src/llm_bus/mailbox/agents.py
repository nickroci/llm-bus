from __future__ import annotations

from typing import TYPE_CHECKING, cast

from llm_bus.coordination.store import end_session_consents
from llm_bus.hosts.kinds import ENDPOINT_KINDS
from llm_bus.hosts.sessions import is_subagent
from llm_bus.mailbox import labels
from llm_bus.mailbox.models import (
    Agent,
    address,
)
from llm_bus.routing import routing_scope

if TYPE_CHECKING:
    import sqlite3

    from llm_bus.hosts.processes import HostProcess

SESSION_IDLE_HOURS = 24


def expire_idle_agents(db: sqlite3.Connection) -> None:
    """Close sessions idle for a day so their messages stop waking and their locks lapse."""
    db.execute(
        "UPDATE agents SET ended_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
        "WHERE ended_at IS NULL AND last_seen <= "
        "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
        (f"-{SESSION_IDLE_HOURS} hours",),
    )
    db.execute(
        "DELETE FROM agent_labels WHERE address IN "
        "(SELECT address FROM agents WHERE ended_at IS NOT NULL)"
    )
    for (ended_address,) in cast(
        "list[tuple[str]]",
        db.execute(
            "SELECT DISTINCT c.address FROM coordination_session_consents c JOIN agents a "
            "ON a.address=c.address WHERE a.ended_at IS NOT NULL AND c.ended_at IS NULL"
        ).fetchall(),
    ):
        end_session_consents(db, str(ended_address))


def register(  # noqa: PLR0913 - explicit optional registration metadata
    db: sqlite3.Connection,
    kind: str,
    session_id: str,
    project: str,
    *,
    messaging_socket: str | None = None,
    label: str | None = None,
    host: HostProcess | None = None,
) -> str:
    """Upsert session address without deleting its pending messages.

    A host process found by the caller replaces the recorded one, so a resumed session is
    checked against its new process. Without one, the previous record stays.
    """
    # pylint: disable=too-many-arguments
    scope = routing_scope(project)
    agent_address = upsert_agent(db, kind, session_id, project, scope)
    if label is not None:
        labels.claim(db, scope, agent_address, label)
    db.execute(
        "UPDATE agents SET messaging_socket = ? WHERE address = ?",
        (messaging_socket if kind in ENDPOINT_KINDS else None, agent_address),
    )
    if host is not None:
        db.execute(
            "UPDATE agents SET host_pid = ?, host_started = ? WHERE address = ?",
            (host.pid, host.started, agent_address),
        )
    return agent_address


def host_session(db: sqlite3.Connection, kind: str, host: HostProcess) -> str | None:
    """The live session a host hook registered for this exact host process, newest first."""
    row = cast(
        "tuple[str] | None",
        db.execute(
            "SELECT address FROM agents WHERE kind = ? AND host_pid = ? AND host_started = ? "
            "AND ended_at IS NULL ORDER BY last_seen DESC LIMIT 1",
            (kind, host.pid, host.started),
        ).fetchone(),
    )
    return row[0] if row is not None else None


def read_messaging_socket(db: sqlite3.Connection, agent_address: str) -> str | None:
    """Read the native endpoint reported by the recipient's own hook."""
    row = cast(
        "tuple[str | None] | None",
        db.execute(
            "SELECT messaging_socket FROM agents WHERE address = ? AND ended_at IS NULL",
            (agent_address,),
        ).fetchone(),
    )
    return row[0] if row is not None else None


def upsert_agent(
    db: sqlite3.Connection, kind: str, session_id: str, project: str, scope: str
) -> str:
    """Record the session's current scope without ending its join consent.

    Hooks can register from a folder outside the repository, or after a git timeout,
    while the session is still the joined worker. Authorization refuses directions
    until the session is back in the project scope.
    """
    agent_address = address(kind, session_id)
    expire_idle_agents(db)
    db.execute("DELETE FROM agent_labels WHERE address=? AND scope<>?", (agent_address, scope))
    db.execute(
        "INSERT INTO agents(address, kind, project, scope, last_seen, ended_at) "
        "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), NULL) "
        "ON CONFLICT(address) DO UPDATE SET project = excluded.project, "
        "scope = excluded.scope, "
        "last_seen = excluded.last_seen, ended_at = NULL",
        (agent_address, kind, project, scope),
    )
    return agent_address


def end_session(db: sqlite3.Connection, kind: str, session_id: str) -> None:
    """Mark a registered session closed without consuming its pending messages."""
    end_agent(db, address(kind, session_id))


def end_agent(db: sqlite3.Connection, agent_address: str) -> None:
    end_session_consents(db, agent_address)
    db.execute("DELETE FROM agent_labels WHERE address=?", (agent_address,))
    db.execute(
        "UPDATE agents SET ended_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
        "messaging_socket = NULL "
        "WHERE address = ?",
        (agent_address,),
    )


def agents(db: sqlite3.Connection, project: str | None = None) -> list[Agent]:
    """List sessions active within the last day; presence is not guaranteed."""
    clauses = ["ended_at IS NULL"]
    parameters: list[str] = []
    if project is not None:
        clauses.append("scope = ?")
        parameters.append(routing_scope(project))
    return [
        agent
        for agent in _read_agents(db, clauses, parameters)
        if not is_subagent(agent["address"])
    ]


def all_agents(db: sqlite3.Connection) -> list[Agent]:
    """List all registered sessions, including ended ones."""
    return _read_agents(db, [], [])


def _read_agents(db: sqlite3.Connection, clauses: list[str], parameters: list[str]) -> list[Agent]:
    expire_idle_agents(db)
    query = (
        "SELECT address, kind, project, last_seen, ended_at, "
        "(SELECT label FROM agent_labels WHERE agent_labels.address=agents.address) "
        "FROM agents"
    )
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    rows = cast(
        "list[tuple[str, str, str, str, str | None, str | None]]",
        db.execute(query + " ORDER BY last_seen DESC", parameters).fetchall(),
    )
    return [
        Agent(
            address=row[0],
            kind=row[1],
            project=row[2],
            last_seen=row[3],
            ended_at=row[4],
            label=row[5],
        )
        for row in rows
    ]


def is_ended(db: sqlite3.Connection, agent_address: str) -> bool:
    """Whether a session ended or has been idle for a day."""
    expire_idle_agents(db)
    row = cast(
        "tuple[str | None] | None",
        db.execute("SELECT ended_at FROM agents WHERE address = ?", (agent_address,)).fetchone(),
    )
    return row is not None and row[0] is not None


def guidance_needed(db: sqlite3.Connection, recipient: str, version: int, *, reset: bool) -> bool:
    """Remember only guidance format, never permissions or peer policy."""
    row = cast(
        "tuple[int] | None",
        db.execute("SELECT version FROM hook_guidance WHERE address=?", (recipient,)).fetchone(),
    )
    db.execute(
        "INSERT INTO hook_guidance(address,version) VALUES (?,?) "
        "ON CONFLICT(address) DO UPDATE SET version=excluded.version",
        (recipient, version),
    )
    return reset or row is None or row[0] != version
