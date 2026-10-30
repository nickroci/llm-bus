"""Remote transport state alongside the native SQLite mailbox."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast
from uuid import UUID, uuid4

from llm_bus_policy import receive_mode
from llm_bus_store import (
    MAX_BATCH,
    MAX_MESSAGE_BYTES,
    SYNC_ERRORS,
    SYNC_READ_SECONDS,
    BusError,
    Store,
    address,
    routing_scope,
    synchronization_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Connection

    from llm_bus_store import SenderContext

REPLY_UUID_VERSION = 4
SUSPEND_POLLING_QUERY = (
    "UPDATE remote_sync_work SET polling_suspended_at=COALESCE(polling_suspended_at,?) "
    "WHERE kind=? AND key=?"
)
NEXT_SYNC_QUERY = (
    "SELECT MIN(next_attempt_at) FROM (SELECT next_attempt_at FROM remote_sync_work "
    "WHERE next_attempt_at>? AND polling_suspended_at IS NULL UNION ALL "
    "SELECT next_attempt_at FROM remote_peer_sync WHERE next_attempt_at>?)"
)


def validate_reply_uuid(value: str) -> None:
    """Accept canonical UUIDv4 parent identities only."""
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as error:
        raise BusError("Invalid remote reply UUID") from error
    if parsed.version != REPLY_UUID_VERSION or str(parsed) != value:
        raise BusError("Invalid remote reply UUID")


class Envelope(TypedDict):
    id: str
    sender: str
    recipient: str
    repo_id: str
    body: str
    reply_to: NotRequired[str]


class Peer(TypedDict):
    address: str
    machine_id: str
    repo_id: str
    native_address: str
    last_seen: str
    ended: int
    synchronization: NotRequired[dict[str, object]]


class Delivery(Envelope):
    sequence: int
    sent_at: str


class RemoteStore:
    """Atomic UUID deduplication and durable outbox; native message IDs stay integers."""

    # This facade owns mailbox-bound transport state and its durable synchronization metadata.
    # pylint: disable=too-many-public-methods

    store: Store
    connection: str

    RETRY_DELAYS: tuple[int, ...] = (5, 10, 20, 40, 80, 160, 300)
    READ_SECONDS: int = SYNC_READ_SECONDS
    PEER_SECONDS: int = 60
    HORIZON_DAYS: int = 30
    SYNC_ERRORS: frozenset[str | None] = SYNC_ERRORS | {None}

    def sync_snapshot(self, kind: str, key: str) -> dict[str, object]:
        with self.store.transaction() as db:
            return synchronization_snapshot(db, kind, key)

    def work_due(self, kind: str, key: str, now: datetime, *, force: bool = False) -> bool:  # pylint: disable=clean-code-boolean-flag-argument
        snapshot = self.sync_snapshot(kind, key)
        accepted = cast("str | None", snapshot["first_accepted_at"])
        if (
            kind == "status"
            and accepted
            and now >= datetime.fromisoformat(accepted) + timedelta(days=self.HORIZON_DAYS)
        ):
            with self.store.transaction() as db:
                db.execute(
                    SUSPEND_POLLING_QUERY,
                    (now.isoformat(), kind, key),
                )
            return force
        due = cast("str | None", snapshot["next_attempt_at"])
        return force or due is None or now >= datetime.fromisoformat(due)

    def record_sync_result(
        self, kind: str, key: str, now: datetime, *, error_code: str | None
    ) -> None:
        self.sync_snapshot(kind, key)
        if error_code not in self.SYNC_ERRORS:
            raise BusError("Invalid synchronization error code")
        with self.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            row = cast(
                "tuple[int] | None",
                db.execute(
                    "SELECT attempts FROM remote_sync_work WHERE kind=? AND key=?", (kind, key)
                ).fetchone(),
            )
            attempts = (row[0] if row else 0) + 1 if error_code else 0
            delay = (
                self.RETRY_DELAYS[min(attempts - 1, len(self.RETRY_DELAYS) - 1)]
                if error_code
                else (self.READ_SECONDS if kind in {"status", "report_read"} else 0)
            )
            db.execute(
                "INSERT INTO remote_sync_work(kind,key,attempts,next_attempt_at,last_attempt_at,"
                "last_success_at,error_code) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(kind,key) DO UPDATE SET "
                "attempts=excluded.attempts,next_attempt_at=excluded.next_attempt_at,"
                "last_attempt_at=excluded.last_attempt_at,last_success_at="
                "COALESCE(excluded.last_success_at,remote_sync_work.last_success_at),"
                "error_code=excluded.error_code",
                (
                    kind,
                    key,
                    attempts,
                    (now + timedelta(seconds=delay)).isoformat(),
                    now.isoformat(),
                    None if error_code else now.isoformat(),
                    error_code,
                ),
            )

    def observe_acceptance(self, key: str, now: datetime) -> None:
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO remote_sync_work(kind,key,first_accepted_at) VALUES ('status',?,?) "
                "ON CONFLICT(kind,key) DO UPDATE SET first_accepted_at="
                "COALESCE(remote_sync_work.first_accepted_at,excluded.first_accepted_at)",
                (key, now.isoformat()),
            )

    def peer_snapshot(self, repo_id: str, now: datetime) -> dict[str, object]:
        columns: tuple[str, ...] = (
            "last_attempt_at",
            "last_success_at",
            "next_attempt_at",
            "error_code",
        )
        with self.store.transaction() as db:
            row = cast(
                "tuple[str | None, ...] | None",
                db.execute(
                    "SELECT last_attempt_at,last_success_at,next_attempt_at,error_code "
                    "FROM remote_peer_sync WHERE repo_id=?",
                    (repo_id,),
                ).fetchone(),
            )
        result: dict[str, object] = (
            dict(zip(columns, row, strict=True))
            if row
            else dict.fromkeys(columns, cast("object", None))
        )
        success = cast("str | None", result["last_success_at"])
        result["stale"] = (
            success is None
            or result["error_code"] is not None
            or now >= datetime.fromisoformat(success) + timedelta(seconds=self.PEER_SECONDS)
        )
        return result

    def record_peer_result(self, repo_id: str, now: datetime, *, error_code: str | None) -> None:
        if error_code not in self.SYNC_ERRORS:
            raise BusError("Invalid synchronization error code")
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO remote_peer_sync VALUES (?,?,?,?,?) "
                "ON CONFLICT(repo_id) DO UPDATE SET "
                "last_attempt_at=excluded.last_attempt_at,last_success_at="
                "COALESCE(excluded.last_success_at,remote_peer_sync.last_success_at),"
                "next_attempt_at=excluded.next_attempt_at,error_code=excluded.error_code",
                (
                    repo_id,
                    now.isoformat(),
                    None if error_code else now.isoformat(),
                    (now + timedelta(seconds=self.PEER_SECONDS)).isoformat(),
                    error_code,
                ),
            )

    def sleep_seconds(self, now: datetime) -> float:
        with self.store.transaction() as db:
            row = cast(
                "tuple[str | None]",
                db.execute(
                    NEXT_SYNC_QUERY,
                    (now.isoformat(), now.isoformat()),
                ).fetchone(),
            )
        delay = (datetime.fromisoformat(row[0]) - now).total_seconds() if row[0] else 5
        return max(1, min(30, delay))

    def __init__(self, store: Store, connection: str) -> None:
        self.store = store
        self.connection = connection
        with store.transaction() as db:
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
                "list[tuple[str, str, str, str | None, str]]",
                db.execute(
                    "SELECT address, project, last_seen, ended_at, scope FROM agents "
                    "WHERE address NOT IN (SELECT address FROM remote_proxies)"
                ).fetchall(),
            )
        return [
            {
                "native_address": row[0],
                "project": row[1],
                "last_seen": row[2],
                "ended_at": row[3],
                "scope": row[4],
            }
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

    def enqueue(  # noqa: PLR0913 - additive public API preserves optional keywords
        self,
        sender: SenderContext,
        recipient: str,
        repo_id: str,
        body: str,
        *,
        scope: str | None = None,
        reply_to: int | None = None,
    ) -> dict[str, object]:
        # Preserve the existing scope keyword and add an optional public parent ID.
        # pylint: disable=too-many-arguments
        if not body.strip() or len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise BusError("Message body must contain 1-65536 UTF-8 bytes")
        native = address(sender.kind, sender.session_id)
        scope = routing_scope(sender.project) if scope is None else scope
        envelope = Envelope(
            id=str(uuid4()), sender=native, recipient=recipient, repo_id=repo_id, body=body
        )
        with self.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store.validate_reply(
                db, reply_to, (native, scope), (recipient, f"remote:{repo_id}")
            )
            if reply_to is not None:
                parent = cast(
                    "tuple[str, str] | None",
                    db.execute(
                        "SELECT id,envelope FROM remote_outbox WHERE local_id=? UNION ALL "
                        "SELECT id,envelope FROM remote_imports WHERE local_id=?",
                        (reply_to, reply_to),
                    ).fetchone(),
                )
                if parent is None:
                    raise BusError("Remote reply requires a remote parent")
                self._validate_parent_envelope(parent[1], native, recipient, repo_id)
                envelope["reply_to"] = parent[0]
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
                "sender_scope,recipient_scope,reply_to) "
                "VALUES (?,?,?,?,?,?,?)",
                (native, recipient, body, sender.project, scope, f"remote:{repo_id}", reply_to),
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
            "reply_to": reply_to,
            "wake": {"status": "unsupported", "via": "remote bridge", "reason": "delivery pending"},  # pylint: disable=clean-code-business-policy-literal
        }

    def _validate_parent_envelope(
        self, payload: str, sender: str, recipient: str, repo: str
    ) -> None:
        """Normalize only this connection's native participant; UUID metadata grants no trust."""
        parent = cast("dict[str, object]", json.loads(payload))
        machine = self.connection.rsplit("|", 1)[1]

        def qualified(value: str) -> str:
            return value if "/" in value else f"{machine}/{repo}/{value}"

        parent_sender, parent_recipient = parent.get("sender"), parent.get("recipient")
        if (
            parent.get("repo_id") != repo
            or not isinstance(parent_sender, str)
            or not isinstance(parent_recipient, str)
            or sorted((qualified(parent_sender), qualified(parent_recipient)))
            != sorted((qualified(sender), qualified(recipient)))
        ):
            raise BusError("Reply participants or repository do not match remote parent")

    def _incoming_parent(self, db: Connection, item: Delivery, scope: str) -> int | None:
        parent_uuid = item.get("reply_to")
        if parent_uuid is None:
            return None
        validate_reply_uuid(parent_uuid)
        parent = cast(
            "tuple[int, str] | None",
            db.execute(
                "SELECT local_id,envelope FROM remote_imports WHERE id=? UNION ALL "
                "SELECT local_id,envelope FROM remote_outbox WHERE id=?",
                (parent_uuid, parent_uuid),
            ).fetchone(),
        )
        if parent is None:
            return None
        native = item["recipient"].split("/", 2)[2]
        self._validate_parent_envelope(parent[1], item["sender"], native, item["repo_id"])
        self.store.validate_reply(
            db, parent[0], (item["sender"], f"remote:{item['repo_id']}"), (native, scope)
        )
        return parent[0]

    def outgoing(self, *, after: int = 0) -> list[tuple[Envelope, str]]:
        with self.store.transaction() as db:
            rows = cast(
                "list[tuple[str, str]]",
                db.execute(
                    "SELECT envelope,state FROM remote_outbox "
                    "WHERE state NOT IN ('acknowledged','expired') AND local_id>? "
                    "ORDER BY local_id LIMIT 100",
                    (after,),
                ).fetchall(),
            )
        return [(cast("Envelope", json.loads(payload)), state) for payload, state in rows]

    def outgoing_pages(self) -> Iterator[list[tuple[Envelope, str]]]:
        after = 0
        while page := self.outgoing(after=after):
            with self.store.transaction() as db:
                after = cast(
                    "tuple[int]",
                    db.execute(
                        "SELECT local_id FROM remote_outbox WHERE id=?", (page[-1][0]["id"],)
                    ).fetchone(),
                )[0]
            yield page

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

    def source_scope(self, remote_id: str) -> str | None:
        with self.store.transaction() as db:
            row = cast(
                "tuple[str | None]",
                db.execute(
                    "SELECT m.sender_scope FROM messages m JOIN remote_outbox o ON o.local_id=m.id "
                    "WHERE o.id=?",
                    (remote_id,),
                ).fetchone(),
            )
        return row[0]

    def statuses(self) -> list[dict[str, object]]:
        with self.store.transaction() as db:
            rows = cast(
                "list[tuple[int]]",
                db.execute(
                    "SELECT o.local_id FROM remote_outbox o "
                    "JOIN messages m ON m.id=o.local_id ORDER BY o.local_id DESC LIMIT 100"
                ).fetchall(),
            )
        result = [self.store.message_status(local_id) for (local_id,) in rows]
        for row in result:
            key = cast("str", row["remote_id"])
            row["status"] = cast("dict[str, object]", row["transport"])["status"]
            # Keep the earlier remote-status scheduling keys alongside the shared projection.
            sync = cast("dict[str, object]", row["synchronization"])
            legacy = self.sync_snapshot("status", key)
            sync.update({name: legacy[name] for name in ("attempts", "first_accepted_at")})
            row["upload_synchronization"] = self.sync_snapshot("upload", key)
            row["report_synchronization"] = self.sync_snapshot("report_read", key)
        return result

    def set_state(self, remote_id: str, state: str) -> None:
        progress = ("queued_local", "accepted_remote", "expired", "delivered_local", "acknowledged")
        if state not in progress:
            raise BusError("Invalid remote message state")
        with self.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            row = cast(
                "tuple[str, str | None] | None",
                db.execute(
                    "SELECT o.state,m.acknowledged_at FROM remote_outbox o JOIN messages m "
                    "ON m.id=o.local_id WHERE o.id=?",
                    (remote_id,),
                ).fetchone(),
            )
            if row is None:
                raise BusError("Unknown remote message ID")
            stored, acknowledged = row
            if stored not in progress:
                raise BusError("Invalid stored remote message state")
            merged = (
                "acknowledged"
                if acknowledged is not None
                else progress[max(progress.index(stored), progress.index(state))]
            )
            db.execute("UPDATE remote_outbox SET state=? WHERE id=?", (merged, remote_id))
            if merged == "acknowledged":
                db.execute(
                    "UPDATE messages SET acknowledged_at=COALESCE(acknowledged_at, "
                    "strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                    "WHERE id=(SELECT local_id FROM remote_outbox WHERE id=?)",
                    (remote_id,),
                )

    def admit_message(self, item: Delivery, scope: str) -> tuple[str, int | None, bool] | None:
        """Commit exactly one delivery class under the current machine policy."""
        native = item["recipient"].split("/", 2)[2]
        payload = json.dumps(item, sort_keys=True)
        with self.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            recipient = cast(
                "tuple[str, str] | None",
                db.execute(
                    "SELECT project,scope FROM agents WHERE address=? AND address NOT IN "
                    "(SELECT address FROM remote_proxies)",
                    (native,),
                ).fetchone(),
            )
            if recipient is None:
                return None
            mode = receive_mode(item, scope, recipient[1], self.connection)
            if mode == "deny":
                return None
            prior = cast(
                "tuple[str, int | None, str] | None",
                db.execute(
                    "SELECT 'inbox',local_id,envelope FROM remote_imports WHERE id=? "
                    "UNION ALL SELECT 'monitor',NULL,envelope FROM remote_monitor WHERE id=? "
                    "UNION ALL SELECT 'outbox',local_id,envelope FROM remote_outbox WHERE id=?",
                    (item["id"], item["id"], item["id"]),
                ).fetchone(),
            )
            if prior is not None:
                prior_mode, prior_id, prior_payload = prior
                if prior_mode == "outbox" or prior_payload != payload:
                    raise BusError("Remote message ID was reused with different content")
                return prior_mode, prior_id, False
            parent_id = self._incoming_parent(db, item, scope)
            if "reply_to" in item and parent_id is None:
                return None
            if mode == "monitor":
                db.execute(
                    "INSERT INTO remote_monitor(id,envelope,scope,connection) VALUES (?,?,?,?)",
                    (item["id"], payload, scope, self.connection),
                )
                return "monitor", None, True
            self._proxy(db, item["sender"], item["repo_id"])
            cursor = db.execute(
                "INSERT INTO messages(sender,recipient,body,recipient_cwd,"
                "sender_scope,recipient_scope,sent_at,reply_to) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    item["sender"],
                    native,
                    item["body"],
                    recipient[0],
                    f"remote:{item['repo_id']}",
                    scope,
                    item["sent_at"],
                    parent_id,
                ),
            )
            local_id = cursor.lastrowid
            if local_id is None:
                raise RuntimeError("SQLite did not return an imported message ID")
            db.execute(
                "INSERT INTO remote_imports(id,local_id,envelope) VALUES (?,?,?)",
                (item["id"], local_id, payload),
            )
            self.store.request_dispatch(db, native)
            return "inbox", local_id, True

    @staticmethod
    def monitor_history(
        store: Store, *, repository: str | None, before: int | None = None, limit: int = 100
    ) -> list[tuple[int, str, str, Delivery]]:
        """Read retained monitoring without requiring current configuration or a host identity."""
        if not 1 <= limit <= MAX_BATCH:
            raise BusError("Monitor limit must be between 1 and 100")
        if before is not None and before < 1:
            raise BusError("Monitor cursor must be a positive ID")
        clauses: list[str] = []
        parameters: list[str | int] = []
        if repository is not None:
            clauses.append("scope=?")
            parameters.append(routing_scope(repository))
        if before is not None:
            clauses.append("monitor_id<?")
            parameters.append(before)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(limit)
        with store.transaction() as db:
            rows = cast(
                "list[tuple[int, str, str, str]]",
                db.execute(
                    "SELECT monitor_id,scope,connection,envelope FROM remote_monitor"  # noqa: S608
                    + where
                    + " ORDER BY monitor_id DESC LIMIT ?",
                    parameters,
                ).fetchall(),
            )
        return [
            (key, scope, connection, cast("Delivery", json.loads(payload)))
            for key, scope, connection, payload in rows
        ]

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

    def permits_ack(self, remote_id: str) -> bool:
        with self.store.transaction() as db:
            row = cast(
                "tuple[int] | None",
                db.execute(
                    "SELECT local_id FROM remote_imports WHERE id=?", (remote_id,)
                ).fetchone(),
            )
        return row is not None and self.store.pending_allowed(row[0])

    def mark_acknowledged(self, remote_id: str) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE remote_imports SET ack_synced=1 WHERE id=?", (remote_id,))
