"""Admission ownership."""

# pylint: disable=clean-code-commented-out-code

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from llm_bus.coordination.authority import authorize
from llm_bus.coordination.availability import accept
from llm_bus.coordination.delivery import persist
from llm_bus.coordination.models import (
    GATE_OPERATIONS,
    MAX_LIFETIME,
    Availability,
    Direction,
    timestamp,
)
from llm_bus.coordination.stalls import observe_message
from llm_bus.errors import BusError
from llm_bus.hosts.sessions import is_subagent
from llm_bus.mailbox import writes
from llm_bus.mailbox.eligibility import DELIVERY_HOURS, cutoff
from llm_bus.mailbox.retention_schema import allocate_id, normalize_age
from llm_bus.policy.inbound import receive_mode
from llm_bus.remote import outbox
from llm_bus.remote.models import Delivery, validate_reply_uuid
from llm_bus.remote.protocol import parse_approval

if TYPE_CHECKING:
    from sqlite3 import Connection

    from llm_bus.coordination.models import Approval


if TYPE_CHECKING:
    from llm_bus.remote.store import RemoteStore


def incoming_parent(
    self: RemoteStore, db: Connection, item: Delivery, scope: str, now: datetime
) -> int | None:
    parent_uuid = item.get("reply_to")
    if parent_uuid is None:
        return None
    validate_reply_uuid(parent_uuid)
    parent = cast(
        "tuple[int, str, str] | None",
        db.execute(
            "SELECT r.local_id,r.envelope,m.retention_at FROM remote_imports r "
            "JOIN messages m ON m.id=r.local_id WHERE r.id=? UNION ALL "
            "SELECT o.local_id,o.envelope,m.retention_at FROM remote_outbox o "
            "JOIN messages m ON m.id=o.local_id WHERE o.id=?",
            (parent_uuid, parent_uuid),
        ).fetchone(),
    )
    if parent is None:
        return None
    local_id, payload, age = parent
    native = item["recipient"].split("/", 2)[2]
    outbox.validate_parent_envelope(self, payload, item["sender"], native, item["repo_id"])
    writes.validate_reply(
        db,
        local_id,
        (item["sender"], f"remote:{item['repo_id']}"),
        (native, scope),
        accepted_remote=True,
    )
    return local_id if age > cutoff(now) else None


def _screen(  # noqa: PLR0913 - captured instant belongs to the admission operation
    db: Connection,
    item: Delivery,
    recipient: tuple[str, str],
    direction: Direction | None,
    *,
    inbox: bool,
    now: datetime,
) -> tuple[bool, Approval | None]:
    """Parse the approval and, for inbox delivery, apply the lifetime cap and receiver authority.

    An approval rides only with a gate direction; a missing one is left to ``authorize``.
    """
    # pylint: disable=too-many-arguments
    approval = None
    if "approval" in item:
        if direction is None or direction.operation not in GATE_OPERATIONS:
            return False, None
        try:
            approval = parse_approval(item["approval"], direction.operation)
        except BusError:
            return False, None
    if direction is None or not inbox:
        return True, approval
    # A peer cannot widen the local approval lifetime cap.
    if approval is not None and timestamp(approval.expires_at) > now + MAX_LIFETIME:
        return False, None
    decision = authorize(
        db,
        sender=item["sender"],
        recipient=recipient[0],
        scope=recipient[1],
        direction=direction,
        now=now,
        approval=approval,
    )
    return decision.verified, approval


def admit_message(  # noqa: C901, PLR0911, PLR0912 - explicit transport and authority denials
    self: RemoteStore, item: Delivery, scope: str
) -> tuple[str, int | None, bool] | None:
    """Commit exactly one delivery class under the current machine policy."""
    # pylint: disable=too-many-locals,too-many-return-statements,too-many-branches
    native = item["recipient"].split("/", 2)[2]
    payload = json.dumps(item, sort_keys=True)
    with self.store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        now = datetime.now(UTC)
        age = normalize_age(item["sent_at"])
        if age <= cutoff(now):
            return None
        recipient = cast(
            "tuple[str, str] | None",
            db.execute(
                "SELECT project,scope FROM agents WHERE address=? AND address NOT IN "
                "(SELECT address FROM remote_proxies)",
                (native,),
            ).fetchone(),
        )
        if recipient is None or is_subagent(native):
            return None
        mode = receive_mode(item, scope, recipient[1], self.connection)
        if mode == "deny":
            return None
        if "coordination" in item and "availability" in item:
            raise BusError("Conflicting remote message classes")
        event = Availability.parse(item["availability"]) if "availability" in item else None
        direction = Direction.parse(item["coordination"]) if "coordination" in item else None
        admit, approval = _screen(
            db, item, (native, scope), direction, inbox=mode == "inbox", now=now
        )
        if not admit:
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
        if db.execute("SELECT 1 FROM remote_replay_guard WHERE id=?", (item["id"],)).fetchone():
            return None
        if age <= cutoff(now, hours=DELIVERY_HOURS):
            return None
        parent_id = incoming_parent(self, db, item, scope, now)
        # UUID knowledge grants no authority: policy and typed authority were checked above.
        if (
            "reply_to" in item
            and parent_id is None
            and not db.execute(
                "SELECT 1 FROM remote_replay_guard WHERE id=? UNION ALL "
                "SELECT 1 FROM remote_imports WHERE id=? UNION ALL "
                "SELECT 1 FROM remote_outbox WHERE id=?",
                (item["reply_to"], item["reply_to"], item["reply_to"]),
            ).fetchone()
        ):
            return None
        if mode == "monitor":
            db.execute(
                "INSERT INTO remote_monitor(monitor_id,id,envelope,scope,connection,retention_at) "
                "VALUES (?,?,?,?,?,?)",
                (allocate_id(db, "monitor"), item["id"], payload, scope, self.connection, age),
            )
            return "monitor", None, True
        db.execute("SAVEPOINT availability_admission")
        outbox.proxy(db, item["sender"], item["repo_id"])
        local_id = allocate_id(db, "message")
        db.execute(
            "INSERT INTO messages(id,sender,recipient,body,recipient_cwd,"
            "sender_scope,recipient_scope,sent_at,reply_to,retention_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                local_id,
                item["sender"],
                native,
                item["body"],
                recipient[0],
                f"remote:{item['repo_id']}",
                scope,
                item["sent_at"],
                parent_id,
                age,
            ),
        )
        if event is not None and not accept(
            db,
            sender=item["sender"],
            recipient=native,
            scope=scope,
            event=event,
            message_id=local_id,
            sent_at=item["sent_at"],
            event_id=item["id"],
        ):
            db.execute("ROLLBACK TO availability_admission")
            db.execute("RELEASE availability_admission")
            return None
        if direction is not None:
            persist(db, local_id, direction, approval)
        db.execute(
            "INSERT INTO remote_imports(id,local_id,envelope) VALUES (?,?,?)",
            (item["id"], local_id, payload),
        )
        self.store.request_dispatch(db, native)
        if direction is None and event is None and parent_id is not None:
            observe_message(db, local_id, now=now)
        db.execute("RELEASE availability_admission")
        return "inbox", local_id, True


def acknowledgements(self: RemoteStore) -> list[tuple[str, str]]:
    with self.store.transaction() as db:
        rows = cast(
            "list[tuple[str, str]]",
            db.execute(
                "SELECT r.id,r.envelope FROM remote_imports r "
                "JOIN messages m ON m.id=r.local_id "
                "WHERE r.ack_synced=0 AND m.acknowledged_at IS NOT NULL AND m.retention_at>?",
                (cutoff(),),
            ).fetchall(),
        )
    return [
        (remote_id, cast("Delivery", json.loads(payload))["repo_id"]) for remote_id, payload in rows
    ]


def permits_ack(self: RemoteStore, remote_id: str) -> bool:
    with self.store.transaction() as db:
        row = cast(
            "tuple[int] | None",
            db.execute("SELECT local_id FROM remote_imports WHERE id=?", (remote_id,)).fetchone(),
        )
    return row is not None and self.store.pending_allowed(row[0])


def mark_acknowledged(self: RemoteStore, remote_id: str) -> None:
    with self.store.transaction() as db:
        db.execute("UPDATE remote_imports SET ack_synced=1 WHERE id=?", (remote_id,))
