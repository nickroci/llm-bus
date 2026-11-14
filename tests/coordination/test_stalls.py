"""Silence clocks observe task communication, never roster or polling activity."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from llm_bus.coordination import stalls
from llm_bus.coordination.store import project
from llm_bus.errors import BusError
from llm_bus.mailbox.schema import SCHEMA_VERSION
from llm_bus.mailbox.store import Store
from llm_bus.remote.outgoing import enqueue_remote
from llm_bus.routing import routing_scope
from tests.coordination.test_work_state import OWNER, WORKER, relay_config, report, send

if TYPE_CHECKING:
    from pathlib import Path
    from sqlite3 import Connection

    from llm_bus.coordination.models import Direction
    from llm_bus.mailbox.models import SenderContext

START = datetime(2026, 1, 1, tzinfo=UTC)
DUE = START + timedelta(minutes=30)


def setup_clock(db: Connection) -> dict[str, object]:
    db.execute(
        "UPDATE messages SET acknowledged_at=? WHERE acknowledged_at IS NOT NULL",
        (START.isoformat(),),
    )
    scope = cast("tuple[str]", db.execute("SELECT scope FROM coordination_projects").fetchone())[0]
    return project(db, scope, "demo")


def items(
    db: Connection, record: dict[str, object], now: datetime = DUE
) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", stalls.inspect(db, record, OWNER, now=now)["items"])


def test_boundary_baseline_read_only_and_roster(
    enrolled: tuple[Store, Direction, SenderContext],
) -> None:
    store, _direction, owner = enrolled
    message_id = send(store, owner, "assign")
    with store.transaction() as db:
        record = setup_clock(db)
        before = db.total_changes
        assert items(db, record, DUE - timedelta(microseconds=1)) == []
        projected = stalls.projection(db, record, OWNER, now=START)
        assert len(cast("list[object]", projected["items"])) == 1
        found = items(db, record)
        assert len(found) == 1
        item = found[0]
        assert item["message_id"] == message_id
        assert item["silence_seconds"] == 1800
        assert item["work_status"] is None
        assert item["observation"] == "local"
        assert db.total_changes == before
        stalls.reconcile(db, record, now=DUE)
        db.execute("UPDATE agents SET last_seen=?", (DUE.isoformat(),))
        stalls.reconcile(db, record, now=DUE + timedelta(minutes=10))
        assert items(db, record)[0]["epoch"] == 1
        assert items(db, record)[0]["last_communication_at"] == START.isoformat()


def test_blocked_report_advances_once_and_notified_epoch_survives_config(
    enrolled: tuple[Store, Direction, SenderContext],
) -> None:
    store, _direction, owner = enrolled
    message_id = send(store, owner, "review")
    with store.transaction() as db:
        record = setup_clock(db)
        stalls.reconcile(db, record, now=START)
        db.execute("UPDATE coordination_stall_state SET notified_epoch=epoch")
        stalls.configure(db, record, OWNER, 60)
        assert items(db, record)[0]["notified"] is True
    report(store, message_id, "not_proceeding", "needs_approval")
    with store.transaction() as db:
        db.execute("UPDATE coordination_work_reports SET reported_at=?", (DUE.isoformat(),))
        stalls.observe(db, record, message_id, now=DUE)
        stalls.observe(db, record, message_id, now=DUE + timedelta(minutes=1))
        assert items(db, record) == []
        found = items(db, record, DUE + timedelta(minutes=1))
        assert len(found) == 1
        item = found[0]
        assert item["epoch"] == 2
        assert item["notified"] is False
        assert item["work_status"] == "not_proceeding"


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE coordination_grants SET revoked_at='2026-01-01'",
        "UPDATE coordination_grants SET expires_at='2026-01-01T00:00:00Z'",
        "UPDATE coordination_grants SET operations_json='[]'",
        "DELETE FROM coordination_enrollments WHERE role='worker'",
        "UPDATE coordination_enrollments SET generation='old' WHERE role='worker'",
        "UPDATE agents SET ended_at='2026-01-01' WHERE address='claude:worker'",
        "UPDATE agents SET scope='other' WHERE address='claude:worker'",
        "UPDATE coordination_projects SET generation='replacement'",
    ],
)
def test_current_authority_excludes_without_mutation(
    enrolled: tuple[Store, Direction, SenderContext],
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _direction, owner = enrolled
    send(store, owner, "assign")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("read projection called mutating authority")

    monkeypatch.setattr("llm_bus.coordination.authority.authorize", forbidden)
    with store.transaction() as db:
        record = setup_clock(db)
        db.execute(mutation)
        before = db.total_changes
        assert list(stalls.eligible(db, record, now=DUE)) == []
        assert db.total_changes == before


def test_eligible_operations_cancel_terminal_and_uncapped(
    enrolled: tuple[Store, Direction, SenderContext],
) -> None:
    store, _direction, owner = enrolled
    old = send(store, owner, "assign")
    send(store, owner, "cancel")
    terminal = send(store, owner, "review")
    report(store, terminal, "completed")
    send(store, owner, "reprioritize")
    send(store, owner, "assign", ack=False)
    active = {send(store, owner, "assign") for _ in range(101)}
    with store.transaction() as db:
        record = setup_clock(db)
        assert [item["message_id"] for item in items(db, record)] == sorted(active)
        assert old not in active


def test_exact_grant_cancel_and_consent(enrolled: tuple[Store, Direction, SenderContext]) -> None:
    store, _direction, owner = enrolled
    message_id = send(store, owner, "assign")
    cancel_id = send(store, owner, "cancel")
    with store.transaction() as db:
        record = setup_clock(db)
        db.execute(
            "UPDATE coordination_messages SET metadata_json="
            "json_set(metadata_json,'$.grant_id','other') WHERE message_id=?",
            (cancel_id,),
        )
        assert [item["message_id"] for item in items(db, record)] == [message_id]
        grant = cast(
            "tuple[str]", db.execute("SELECT grant_id FROM coordination_grants").fetchone()
        )[0]
        db.execute(
            "INSERT INTO coordination_session_consents VALUES (?,?,?)",
            (grant, WORKER, START.isoformat()),
        )
        assert items(db, record) == []


def test_only_bound_worker_replies_reset_clock(
    enrolled: tuple[Store, Direction, SenderContext],
) -> None:
    store, _direction, owner = enrolled
    message_id = send(store, owner, "assign")
    with store.transaction() as db:
        record = setup_clock(db)
        stalls.reconcile(db, record, now=START)
        scope = record["scope"]

        def reply(
            sender: str, recipient: str, parent: int | None, sender_scope: object = scope
        ) -> int:
            return cast(
                "int",
                db.execute(
                    "INSERT INTO messages(sender,recipient,body,sender_scope,recipient_scope,"
                    "sent_at,reply_to) VALUES (?,?,?,?,?,?,?) RETURNING id",
                    (sender, recipient, "phase", sender_scope, scope, DUE.isoformat(), parent),
                ).fetchone()[0],
            )

        reply(WORKER, OWNER, None)
        reply(WORKER, OWNER, message_id, "wrong")
        coordinator_reply = reply(OWNER, WORKER, message_id)
        stalls.reconcile(db, record, now=DUE)
        assert items(db, record)[0]["epoch"] == 1
        worker_reply = reply(WORKER, OWNER, coordinator_reply)
        stalls.observe(db, record, worker_reply, now=DUE)
        assert items(db, record) == []
        assert items(db, record, DUE + timedelta(minutes=30))[0]["epoch"] == 2


def test_remote_pending_recovery_and_snapshot_idempotency(
    enrolled: tuple[Store, Direction, SenderContext],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, direction, owner = enrolled
    config = relay_config(tmp_path)
    recipient = "b/xyz/claude:worker"
    sent = enqueue_remote(store, config, owner, recipient, "assign", direction=direction)
    monkeypatch.setattr(stalls, "optional_config", lambda: config)
    with store.transaction() as db:
        record = setup_clock(db)
        db.execute(
            "INSERT INTO coordination_targets VALUES (?,?,?,?,?)",
            (
                routing_scope(owner.project),
                direction.project_id,
                recipient,
                direction.grant_id,
                direction.generation,
            ),
        )
        db.execute(
            "UPDATE messages SET acknowledged_at=? WHERE id=?", (START.isoformat(), sent["id"])
        )
        db.execute(
            "INSERT INTO remote_reports VALUES (?,?)",
            (
                sent["remote_id"],
                json.dumps(
                    {
                        "message_id": sent["remote_id"],
                        "revision": 1,
                        "status": "proceeding",
                        "reason": "started",
                    }
                ),
            ),
        )
        found = items(db, record)
        assert len(found) == 1
        pending = found[0]
        assert pending["observation"] == "observation_pending"
        assert pending["due_at"] is None
        assert pending["silence_seconds"] is None
        assert db.execute("SELECT count(*) FROM coordination_stall_state").fetchone()[0] == 0
        stalls.reconcile(db, record, now=DUE)
        stalls.reconcile(db, record, now=DUE + timedelta(minutes=5))
        assert items(db, record) == []
        found = items(db, record, DUE + timedelta(minutes=30))
        assert len(found) == 1
        item = found[0]
        assert item["last_communication_at"] == DUE.isoformat()
        assert item["epoch"] == 1
        assert item["observation"] == "remote_observed"
        db.execute("UPDATE remote_reports SET payload=json_set(payload,'$.revision',2)")
        observed = DUE + timedelta(minutes=10)
        stalls.observe(db, record, cast("int", sent["id"]), now=observed)
        stalls.observe(db, record, cast("int", sent["id"]), now=observed + timedelta(minutes=5))
        stalls.reconcile(db, record, now=observed + timedelta(minutes=10))
        assert items(db, record, observed + timedelta(minutes=30))[0]["epoch"] == 2
        assert (
            items(db, record, observed + timedelta(minutes=30))[0]["last_communication_at"]
            == observed.isoformat()
        )
        db.execute("DELETE FROM coordination_targets WHERE recipient=?", (recipient,))
        assert items(db, record, DUE + timedelta(minutes=30)) == []
        db.execute(
            "INSERT INTO coordination_targets VALUES (?,?,?,?,?)",
            (
                record["scope"],
                direction.project_id,
                recipient,
                direction.grant_id,
                direction.generation,
            ),
        )
        monkeypatch.setattr(stalls, "optional_config", lambda: None)
        assert items(db, record, observed + timedelta(minutes=30)) == []


def test_upgrade_from_15_keeps_source_history(
    enrolled: tuple[Store, Direction, SenderContext],
) -> None:
    store, _direction, owner = enrolled
    message_id = send(store, owner, "assign")
    report(store, message_id, "received")
    with store.transaction() as db:
        for table in (
            "coordination_stall_state",
            "coordination_stall_timers",
            "coordination_stall_settings",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("PRAGMA user_version=15")
    upgraded = Store(store.path)
    with upgraded.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 16
        assert (
            db.execute(
                "SELECT status FROM coordination_work_reports WHERE message_id=?", (message_id,)
            ).fetchone()[0]
            == "received"
        )
        assert db.execute("SELECT count(*) FROM coordination_stall_state").fetchone()[0] == 0


@pytest.mark.parametrize(("value", "expected"), [("1m", 60), ("30m", 1800), ("24h", 86400)])
def test_threshold_units(value: str, expected: int) -> None:
    assert stalls.parse_threshold(value) == expected


@pytest.mark.parametrize("value", ["0m", "25h", "1s", "1.5h", "-1m", "1441m"])
def test_invalid_threshold(value: str) -> None:
    with pytest.raises(BusError, match="Threshold"):
        stalls.parse_threshold(value)


def test_settings_binding_authority_and_schema_reupgrade(
    enrolled: tuple[Store, Direction, SenderContext],
) -> None:
    store, _direction, _owner = enrolled
    with store.transaction() as db:
        record = setup_clock(db)
        with pytest.raises(BusError, match="coordinator"):
            stalls.configure(db, record, WORKER, 60)
        assert stalls.threshold(db, record) == 1800
        stalls.configure(db, record, OWNER, 3600)
        assert stalls.threshold(db, {**record, "generation": "new"}) == 1800
        db.execute("PRAGMA user_version=15")
    upgraded = Store(store.path)
    with upgraded.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 16
        assert stalls.threshold(db, record) == 3600
        assert db.execute("SELECT count(*) FROM coordination_stall_timers").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM coordination_stall_state").fetchone()[0] == 0
