"""Discovery store ownership."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from llm_bus.mailbox.agents import expire_idle_agents

if TYPE_CHECKING:
    from llm_bus.remote.models import Peer
    from llm_bus.remote.store import RemoteStore


def sessions(self: RemoteStore) -> list[dict[str, str | int | None]]:
    with self.store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        expire_idle_agents(db)
        rows = cast(
            "list[tuple[str, str, str, str | None, str, str | None, int | None, str | None]]",
            db.execute(
                "SELECT a.address, a.project, a.last_seen, a.ended_at, a.scope, l.label, "
                "p.revision, p.payload "
                "FROM agents a LEFT JOIN agent_labels l ON l.address=a.address "
                "LEFT JOIN session_publications p ON p.address=a.address "
                "WHERE a.address NOT IN (SELECT address FROM remote_proxies)"
            ).fetchall(),
        )
        result: list[dict[str, str | int | None]] = []
        for row in rows:
            address, project, last_seen, ended_at, scope, label, prior_revision, prior_payload = row
            session: dict[str, str | int | None] = {
                "native_address": address,
                "project": project,
                "last_seen": last_seen,
                "ended_at": ended_at,
                "scope": scope,
                "label": label if ended_at is None else None,
            }
            payload = json.dumps(session, sort_keys=True)
            revision = 1 if prior_revision is None else prior_revision
            if payload != prior_payload:
                if prior_revision is not None:
                    revision = prior_revision + 1
                db.execute(
                    "INSERT INTO session_publications(address,revision,payload) VALUES (?,?,?) "
                    "ON CONFLICT(address) DO UPDATE SET "
                    "revision=excluded.revision,payload=excluded.payload",
                    (address, revision, payload),
                )
            session["revision"] = revision
            result.append(session)
    return result


def publication_payloads(self: RemoteStore) -> dict[str, str]:
    with self.store.transaction() as db:
        return dict(db.execute("SELECT address,payload FROM remote_publications"))


def published(self: RemoteStore, key: str) -> bool:
    with self.store.transaction() as db:
        return (
            db.execute("SELECT 1 FROM remote_publications WHERE address=?", (key,)).fetchone()
            is not None
        )


def record_publication(self: RemoteStore, key: str, payload: str) -> None:
    with self.store.transaction() as db:
        db.execute(
            "INSERT INTO remote_publications VALUES (?,?) "
            "ON CONFLICT(address) DO UPDATE SET payload=excluded.payload",
            (key, payload),
        )


def cache(self: RemoteStore, repo_id: str, items: list[Peer]) -> None:
    with self.store.transaction() as db:
        db.execute("DELETE FROM remote_peers WHERE repo_id=?", (repo_id,))
        db.executemany(
            "INSERT INTO remote_peers VALUES (?, ?, ?)",
            [(peer["address"], repo_id, json.dumps(peer)) for peer in items],
        )


def peers(self: RemoteStore) -> list[Peer]:
    with self.store.transaction() as db:
        rows = cast("list[tuple[str]]", db.execute("SELECT payload FROM remote_peers").fetchall())
    return [cast("Peer", json.loads(row[0])) for row in rows]
