"""Remote transport state alongside the native SQLite mailbox."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypedDict, cast
from uuid import uuid4

from llm_bus_store import MAX_MESSAGE_BYTES, BusError, Store, address, routing_scope

if TYPE_CHECKING:
    from sqlite3 import Connection

    from llm_bus_store import SenderContext


class Envelope(TypedDict):
    id: str
    sender: str
    recipient: str
    repo_id: str
    body: str


class Peer(TypedDict):
    address: str
    machine_id: str
    repo_id: str
    native_address: str
    last_seen: str
    ended: int


class Delivery(Envelope):
    sequence: int
    sent_at: str


TABLES = (
    (
        "CREATE TABLE IF NOT EXISTS remote_publications "
        "(address TEXT PRIMARY KEY, payload TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS remote_binding (id INTEGER PRIMARY KEY CHECK(id=1), "
        "connection TEXT NOT NULL)"
    ),
    ("CREATE TABLE IF NOT EXISTS remote_proxies (address TEXT PRIMARY KEY)"),
    (
        "CREATE TABLE IF NOT EXISTS remote_peers (address TEXT PRIMARY KEY, repo_id TEXT NOT "
        "NULL, payload TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS remote_outbox (id TEXT PRIMARY KEY, local_id INTEGER NOT "
        "NULL REFERENCES messages(id), envelope TEXT NOT NULL, state TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS remote_imports (id TEXT PRIMARY KEY, local_id INTEGER NOT "
        "NULL REFERENCES messages(id), envelope TEXT NOT NULL, ack_synced INTEGER NOT NULL "
        "DEFAULT 0, wake_claimed INTEGER NOT NULL DEFAULT 0)"
    ),
)


class RemoteStore:
    """Atomic UUID deduplication and durable outbox; native message IDs stay integers."""

    store: Store

    def __init__(self, store: Store, connection: str) -> None:
        self.store = store
        with store.transaction() as db:
            for statement in TABLES:
                db.execute(statement)
            db.execute("INSERT OR IGNORE INTO remote_binding VALUES (1, ?)", (connection,))
            row = cast(
                "tuple[str]",
                db.execute("SELECT connection FROM remote_binding WHERE id=1").fetchone(),
            )
            if row[0] != connection:
                raise BusError(
                    "This mailbox is bound to another relay/machine; use another LLM_BUS_DB"
                )

    def sessions(self) -> list[dict[str, str | None]]:
        self.store.all_agents()  # Apply the native inactivity rule before exporting activity.
        with self.store.transaction() as db:
            rows = cast(
                "list[tuple[str, str, str, str | None]]",
                db.execute(
                    "SELECT address, project, last_seen, ended_at FROM agents "
                    "WHERE address NOT IN (SELECT address FROM remote_proxies)"
                ).fetchall(),
            )
        return [
            {"native_address": row[0], "project": row[1], "last_seen": row[2], "ended_at": row[3]}
            for row in rows
        ]

    def published(self, key: str, payload: str | None = None) -> bool:
        with self.store.transaction() as db:
            row = cast(
                "tuple[str] | None",
                db.execute(
                    "SELECT payload FROM remote_publications WHERE address=?", (key,)
                ).fetchone(),
            )
        return row is not None and (payload is None or row[0] == payload)

    def record_publication(self, key: str, payload: str) -> None:
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO remote_publications VALUES (?,?) "
                "ON CONFLICT(address) DO UPDATE SET payload=excluded.payload",
                (key, payload),
            )

    def cache(self, repo_id: str, peers: list[Peer]) -> None:
        with self.store.transaction() as db:
            db.execute("DELETE FROM remote_peers WHERE repo_id=?", (repo_id,))
            db.executemany(
                "INSERT INTO remote_peers VALUES (?, ?, ?)",
                [(peer["address"], repo_id, json.dumps(peer)) for peer in peers],
            )

    def peers(self) -> list[Peer]:
        with self.store.transaction() as db:
            rows = cast(
                "list[tuple[str]]", db.execute("SELECT payload FROM remote_peers").fetchall()
            )
        return [cast("Peer", json.loads(row[0])) for row in rows]

    @staticmethod
    def _proxy(db: Connection, peer: str, repo_id: str) -> None:
        # Remote peers are history identities, never native roster entries or wake endpoints.
        db.execute(
            "INSERT OR IGNORE INTO agents(address,kind,project,scope,last_seen,ended_at) "
            "VALUES (?, 'remote', ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (peer, f"remote:{repo_id}", f"remote:{repo_id}"),
        )
        db.execute("INSERT OR IGNORE INTO remote_proxies VALUES (?)", (peer,))

    def enqueue(
        self, sender: SenderContext, recipient: str, repo_id: str, body: str
    ) -> dict[str, object]:
        if not body.strip() or len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise BusError("Message body must contain 1-65536 UTF-8 bytes")
        native = address(sender.kind, sender.session_id)
        scope = routing_scope(sender.project)
        envelope = Envelope(
            id=str(uuid4()), sender=native, recipient=recipient, repo_id=repo_id, body=body
        )
        with self.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO agents(address,kind,project,scope,last_seen,ended_at) "
                "VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),NULL) "
                "ON CONFLICT(address) DO UPDATE SET project=excluded.project,scope=excluded.scope, "
                "last_seen=excluded.last_seen,ended_at=NULL",
                (native, sender.kind, sender.project, scope),
            )
            self._proxy(db, recipient, repo_id)
            cursor = db.execute(
                "INSERT INTO messages(sender,recipient,body,sender_cwd,"
                "sender_scope,recipient_scope) "
                "VALUES (?,?,?,?,?,?)",
                (native, recipient, body, sender.project, scope, f"remote:{repo_id}"),
            )
            local_id = cursor.lastrowid
            db.execute(
                "INSERT INTO remote_outbox VALUES (?,?,?,'queued_local')",
                (envelope["id"], local_id, json.dumps(envelope)),
            )
        return {
            "id": local_id,
            "remote_id": envelope["id"],
            "status": "queued_local",
            "sender": native,
            "recipient": recipient,
            "wake": {"status": "unsupported", "via": "remote bridge", "reason": "delivery pending"},  # pylint: disable=clean-code-business-policy-literal
        }

    def outgoing(self) -> list[tuple[Envelope, str]]:
        with self.store.transaction() as db:
            rows = cast(
                "list[tuple[str, str]]",
                db.execute(
                    "SELECT envelope,state FROM remote_outbox "
                    "WHERE state NOT IN ('acknowledged','expired') ORDER BY local_id"
                ).fetchall(),
            )
        return [(cast("Envelope", json.loads(payload)), state) for payload, state in rows]

    def source_activity(self, remote_id: str) -> str:
        with self.store.transaction() as db:
            row = cast(
                "tuple[str]",
                db.execute(
                    "SELECT m.sent_at FROM messages m JOIN remote_outbox o ON o.local_id=m.id "
                    "WHERE o.id=?",
                    (remote_id,),
                ).fetchone(),
            )
        return row[0]

    def statuses(self) -> list[dict[str, object]]:
        with self.store.transaction() as db:
            rows = cast(
                "list[tuple[int, str, str, str]]",
                db.execute(
                    "SELECT o.local_id,o.id,m.recipient,o.state FROM remote_outbox o "
                    "JOIN messages m ON m.id=o.local_id ORDER BY o.local_id DESC LIMIT 100"
                ).fetchall(),
            )
        return [
            {"id": local_id, "remote_id": remote_id, "recipient": recipient, "status": state}
            for local_id, remote_id, recipient, state in rows
        ]

    def set_state(self, remote_id: str, state: str) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE remote_outbox SET state=? WHERE id=?", (state, remote_id))
            if state == "acknowledged":
                db.execute(
                    "UPDATE messages SET acknowledged_at=COALESCE(acknowledged_at, "
                    "strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                    "WHERE id=(SELECT local_id FROM remote_outbox WHERE id=?)",
                    (remote_id,),
                )

    def import_message(self, item: Delivery, scope: str) -> int | None:
        native = item["recipient"].split("/", 2)[2]
        payload = json.dumps(item, sort_keys=True)
        with self.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = cast(
                "tuple[int, str] | None",
                db.execute(
                    "SELECT local_id,envelope FROM remote_imports WHERE id=?", (item["id"],)
                ).fetchone(),
            )
            if prior is not None:
                if prior[1] != payload:
                    raise BusError("Remote message ID was reused with different content")
                return prior[0]
            recipient = cast(
                "tuple[str, str] | None",
                db.execute(
                    "SELECT project,scope FROM agents WHERE address=? AND address NOT IN "
                    "(SELECT address FROM remote_proxies)",
                    (native,),
                ).fetchone(),
            )
            if recipient is None or recipient[1] != scope:
                return None
            self._proxy(db, item["sender"], item["repo_id"])
            cursor = db.execute(
                "INSERT INTO messages(sender,recipient,body,recipient_cwd,"
                "sender_scope,recipient_scope,sent_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    item["sender"],
                    native,
                    item["body"],
                    recipient[0],
                    f"remote:{item['repo_id']}",
                    scope,
                    item["sent_at"],
                ),
            )
            local_id = cursor.lastrowid
            if local_id is None:
                raise RuntimeError("SQLite did not return an imported message ID")
            db.execute(
                "INSERT INTO remote_imports(id,local_id,envelope) VALUES (?,?,?)",
                (item["id"], local_id, payload),
            )
            return local_id

    def claim_wake(self, remote_id: str) -> bool:
        with self.store.transaction() as db:
            return (
                db.execute(
                    "UPDATE remote_imports SET wake_claimed=1 WHERE id=? AND wake_claimed=0",
                    (remote_id,),
                ).rowcount
                == 1
            )

    def acknowledgements(self) -> list[tuple[str, str]]:
        with self.store.transaction() as db:
            rows = cast(
                "list[tuple[str, str]]",
                db.execute(
                    "SELECT r.id,r.envelope FROM remote_imports r "
                    "JOIN messages m ON m.id=r.local_id "
                    "WHERE r.ack_synced=0 AND m.acknowledged_at IS NOT NULL"
                ).fetchall(),
            )
        return [
            (remote_id, cast("Delivery", json.loads(payload))["repo_id"])
            for remote_id, payload in rows
        ]

    def mark_acknowledged(self, remote_id: str) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE remote_imports SET ack_synced=1 WHERE id=?", (remote_id,))
