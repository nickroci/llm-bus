"""Assertions for silence-clock isolation at remote admission boundaries."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from llm_bus.coordination import stalls
from llm_bus.coordination.store import project
from llm_bus.errors import BusError
from llm_bus.remote.models import Delivery
from llm_bus.remote.store import RemoteStore
from llm_bus.reports.store import Reports

if TYPE_CHECKING:
    from llm_bus.coordination.models import Direction
    from llm_bus.mailbox.store import Store
    from llm_bus.policy.config import Config
    from llm_bus.remote.models import Envelope


def clock_state(store: Store, message_id: int) -> tuple[str, int]:
    with store.transaction() as db:
        return cast(
            "tuple[str, int]",
            db.execute(
                "SELECT last_communication_at,epoch FROM coordination_stall_state WHERE message_id=?",
                (message_id,),
            ).fetchone(),
        )


def assert_linked_availability_preserves_silence(
    store: Store,
    config: Config,
    envelope: Envelope,
    direction: Direction,
    *,
    already_notified: bool,
) -> None:
    now = datetime.now(UTC)
    acknowledged = (now - timedelta(hours=1)).isoformat()
    with store.transaction() as db:
        message_id = cast(
            "tuple[int]",
            db.execute(
                "SELECT local_id FROM remote_outbox WHERE id=?", (envelope["id"],)
            ).fetchone(),
        )[0]
        db.execute("UPDATE messages SET acknowledged_at=? WHERE id=?", (acknowledged, message_id))
        db.execute("DELETE FROM coordination_stall_state WHERE message_id=?", (message_id,))
        record = project(db, config.repositories[0].scope, "demo")
        stalls.reconcile(db, record, now=now)
        if already_notified:
            assert len(stalls.apply_due(store, db, record, now=now)) == 1
        before = stalls.inspect(db, record, envelope["sender"], now=now)["items"]
        assert len(cast("list[object]", before)) == 1
    event = Delivery(
        id=str(uuid4()),
        sequence=1,
        sender=envelope["recipient"],
        recipient=f"{config.machine_id}/{envelope['repo_id']}/{envelope['sender']}",
        repo_id=envelope["repo_id"],
        body="joined",
        sent_at=now.isoformat(),
        reply_to=envelope["id"],
        availability={
            "version": 1,
            "project_id": direction.project_id,
            "generation": direction.generation,
            "grant_id": direction.grant_id,
            "kind": "joined",
        },
    )
    result = RemoteStore(store, config.connection).admit_message(
        event, config.repositories[0].scope
    )
    assert result is not None
    assert result[0] == "inbox"
    assert result[2]
    assert clock_state(store, message_id) == (acknowledged, 1)
    with store.transaction() as db:
        assert stalls.inspect(db, record, envelope["sender"], now=now)["items"] == before
        stalls.reconcile(db, record, now=now)
        assert len(stalls.apply_due(store, db, record, now=now)) == (0 if already_notified else 1)
        stalls.reconcile(db, record, now=now + timedelta(minutes=1))
        assert stalls.apply_due(store, db, record, now=now + timedelta(minutes=1)) == []
    assert clock_state(store, message_id) == (acknowledged, 1)


def assert_rejected_observations_isolated(
    store: Store,
    config: Config,
    message_id: int,
    envelope: Envelope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = clock_state(store, message_id)
    value = {
        "message_id": envelope["id"],
        "revision": 2,
        "status": "proceeding",
        "reason": "started",
    }
    reports = Reports(store, config.connection)
    forged = envelope.copy()
    forged["recipient"] = "b/xyz/claude:other"
    with pytest.raises(BusError, match="binding"):
        reports.receive(forged, value, config)
    with pytest.raises(BusError):
        reports.receive(envelope, {**value, "extra": "untrusted text"}, config)
    assert clock_state(store, message_id) == before
    repository = config.repositories[0]
    monitor = replace(
        config,
        repositories=(
            replace(repository, peers=(replace(repository.peers[0], receive_messages="monitor"),)),
        ),
    )
    reply = Delivery(
        id=str(uuid4()),
        sequence=1,
        sender=envelope["recipient"],
        recipient=f"{config.machine_id}/{envelope['repo_id']}/{envelope['sender']}",
        repo_id=envelope["repo_id"],
        body="human-only observation",
        sent_at="2099-01-01T00:00:00Z",
        reply_to=envelope["id"],
    )
    remote = RemoteStore(store, config.connection)
    with monkeypatch.context() as patch:
        patch.setattr("llm_bus.policy.inbound.optional_config", lambda: monitor)
        pending = store.pending_count(envelope["sender"])
        assert remote.admit_message(reply, repository.scope) == ("monitor", None, True)
        assert store.pending_count(envelope["sender"]) == pending
        assert len(RemoteStore.monitor_history(store, repository=None)) == 1
        assert clock_state(store, message_id) == before
