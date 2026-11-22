"""Discovery store ownership."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from llm_bus.mailbox.agents import expire_idle_agents

if TYPE_CHECKING:
    from llm_bus.remote.models import Peer
    from llm_bus.remote.store import RemoteStore


SESSION_ROWS = """
    WITH roster AS (
        SELECT a.address, a.project, a.last_seen, a.ended_at, a.scope,
               CASE WHEN a.ended_at IS NULL THEN l.label END AS label,
               p.revision, p.payload, repo.value AS repo_id
        FROM agents a
        LEFT JOIN agent_labels l ON l.address = a.address
        LEFT JOIN session_publications p ON p.address = a.address
        LEFT JOIN json_each(:repositories) repo ON repo.key = a.scope
        WHERE a.address NOT IN (SELECT address FROM remote_proxies)
    ), revisions AS (
        SELECT *, revision IS NULL
               OR json_extract(payload, '$.project') IS NOT project
               OR json_extract(payload, '$.last_seen') IS NOT last_seen
               OR json_extract(payload, '$.ended_at') IS NOT ended_at
               OR json_extract(payload, '$.scope') IS NOT scope
               OR json_extract(payload, '$.label') IS NOT label AS changed
        FROM roster
    )
    SELECT r.address, r.project, r.last_seen, r.ended_at, r.scope, r.label,
           r.revision, r.changed
    FROM revisions r
    LEFT JOIN remote_publications sent ON sent.address = r.repo_id || '/' || r.address
    WHERE :repositories IS NULL OR r.changed OR (
        r.repo_id IS NOT NULL AND (
            json_extract(sent.payload, '$.revision') IS NOT r.revision
            OR (r.ended_at IS NULL AND substr(r.address, 1, 6) = 'codex:')
        )
    )
"""


def sessions(
    self: RemoteStore, repositories: dict[str, str] | None
) -> list[dict[str, str | int | None]]:
    """Keep all revisions current and select publications still needed by the bridge.

    Active Codex sessions can change role independently of the saved mailbox revision.
    """
    with self.store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        expire_idle_agents(db)
        rows = cast(
            "list[tuple[str, str, str, str | None, str, str | None, int | None, int]]",
            db.execute(
                SESSION_ROWS,
                {"repositories": None if repositories is None else json.dumps(repositories)},
            ).fetchall(),
        )
        result: list[dict[str, str | int | None]] = []
        for (
            address,
            project,
            last_seen,
            ended_at,
            scope,
            label,
            prior_revision,
            changed,
        ) in rows:
            session: dict[str, str | int | None] = {
                "native_address": address,
                "project": project,
                "last_seen": last_seen,
                "ended_at": ended_at,
                "scope": scope,
                "label": label,
            }
            revision = 1 if prior_revision is None else prior_revision
            if changed:
                if prior_revision is not None:
                    revision = prior_revision + 1
                db.execute(
                    "INSERT INTO session_publications(address,revision,payload) VALUES (?,?,?) "
                    "ON CONFLICT(address) DO UPDATE SET "
                    "revision=excluded.revision,payload=excluded.payload",
                    (address, revision, json.dumps(session, sort_keys=True)),
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
