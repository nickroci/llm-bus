"""Outbox ownership."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from llm_bus.coordination.delivery import persist, require_coordinator
from llm_bus.coordination.stalls import observe_message
from llm_bus.errors import BusError
from llm_bus.hosts.sessions import require_main_agent
from llm_bus.mailbox.models import MAX_MESSAGE_BYTES, address
from llm_bus.remote.models import Envelope
from llm_bus.routing import routing_scope

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Connection

    from llm_bus.coordination.models import Approval, Availability, Direction
    from llm_bus.mailbox.models import SenderContext

if TYPE_CHECKING:
    from llm_bus.remote.store import RemoteStore


def proxy(db: Connection, peer: str, repo_id: str) -> None:
    # Remote peers are history identities, never native roster entries or wake endpoints.
    db.execute(
        "INSERT OR IGNORE INTO agents(address,kind,project,scope,last_seen,ended_at) "
        "VALUES (?, 'remote', ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (peer, f"remote:{repo_id}", f"remote:{repo_id}"),
    )
    db.execute("INSERT OR IGNORE INTO remote_proxies VALUES (?)", (peer,))


def enqueue(  # noqa: PLR0913 - additive public API preserves optional keywords
    self: RemoteStore,
    sender: SenderContext,
    recipient: str,
    repo_id: str,
    body: str,
    *,
    scope: str | None = None,
    reply_to: int | None = None,
    direction: Direction | None = None,
    approval: Approval | None = None,
) -> dict[str, object]:
    # pylint: disable=too-many-arguments
    with self.store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        return enqueue_in_transaction(
            self,
            db,
            sender=sender,
            recipient=recipient,
            repo_id=repo_id,
            body=body,
            scope=scope,
            reply_to=reply_to,
            direction=direction,
            approval=approval,
        )


def remote_approval(direction: Direction | None, approval: Approval) -> dict[str, object]:
    """Wire form of an approval; gate records stay local so a record-bound one is refused."""
    if direction is None or approval.record_id is not None:
        raise BusError("Remote approval needs a gate direction and no gate record")
    approval.validate(direction.operation)
    return approval.wire()


def enqueue_in_transaction(  # noqa: PLR0913, PLR0917 - shared transaction writer
    self: RemoteStore,
    db: Connection,
    sender: SenderContext,
    recipient: str,
    repo_id: str,
    body: str,
    *,
    scope: str | None = None,
    reply_to: int | None = None,
    direction: Direction | None = None,
    availability: Availability | None = None,
    approval: Approval | None = None,
) -> dict[str, object]:
    """Caller owns the immediate transaction; public enqueue shares this writer."""
    # pylint: disable=too-many-arguments,too-many-locals,too-many-positional-arguments
    if not body.strip() or len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise BusError("Message body must contain 1-65536 UTF-8 bytes")
    native = address(sender.kind, sender.session_id)
    require_main_agent(native)
    scope = routing_scope(sender.project) if scope is None else scope
    envelope = Envelope(
        id=str(uuid4()), sender=native, recipient=recipient, repo_id=repo_id, body=body
    )
    if availability is not None:
        availability.validate()
        if direction is not None or reply_to is not None:
            raise BusError("Availability cannot carry direction or reply metadata")
        envelope["availability"] = asdict(availability)
    if direction is not None:
        require_coordinator(db, native, scope, direction)
        envelope["coordination"] = asdict(direction)
    if approval is not None:
        envelope["approval"] = remote_approval(direction, approval)
    self.store.validate_reply(db, reply_to, (native, scope), (recipient, f"remote:{repo_id}"))
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
        validate_parent_envelope(self, parent[1], native, recipient, repo_id)
        envelope["reply_to"] = parent[0]
    db.execute(
        "INSERT INTO agents(address,kind,project,scope,last_seen,ended_at) "
        "VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),NULL) "
        "ON CONFLICT(address) DO UPDATE SET project=excluded.project,scope=excluded.scope, "
        "last_seen=excluded.last_seen,ended_at=NULL",
        (native, sender.kind, sender.project, scope),
    )
    proxy(db, recipient, repo_id)
    cursor = db.execute(
        "INSERT INTO messages(sender,recipient,body,sender_cwd,"
        "sender_scope,recipient_scope,reply_to) "
        "VALUES (?,?,?,?,?,?,?)",
        (native, recipient, body, sender.project, scope, f"remote:{repo_id}", reply_to),
    )
    local_id = cursor.lastrowid
    if local_id is None:
        raise RuntimeError("SQLite did not return an outgoing message ID")
    if direction is not None:
        persist(db, local_id, direction)
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


def validate_parent_envelope(
    self: RemoteStore, payload: str, sender: str, recipient: str, repo: str
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


def outgoing(self: RemoteStore, *, after: int = 0) -> list[tuple[Envelope, str]]:
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


def outgoing_pages(self: RemoteStore) -> Iterator[list[tuple[Envelope, str]]]:
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


def source_activity(self: RemoteStore, remote_id: str) -> str:
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


def source_scope(self: RemoteStore, remote_id: str) -> str | None:
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


def statuses(self: RemoteStore) -> list[dict[str, object]]:
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


def set_state(self: RemoteStore, remote_id: str, state: str) -> None:
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
            if acknowledged is None:
                local_id = cast(
                    "tuple[int]",
                    db.execute(
                        "SELECT local_id FROM remote_outbox WHERE id=?", (remote_id,)
                    ).fetchone(),
                )[0]
                observe_message(db, local_id, now=datetime.now(UTC))
