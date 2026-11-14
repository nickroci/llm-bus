"""Silence clocks observe task communication, never roster or polling activity."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from llm_bus.cli.commands import human_command
from llm_bus.cli.parser import build_parser
from llm_bus.coordination import stalls
from llm_bus.coordination.cli import run
from llm_bus.coordination.store import project
from llm_bus.errors import BusError
from llm_bus.locks import notices
from llm_bus.locks import timer as lock_timer
from llm_bus.locks.timer import TIMER_POLL_SECONDS
from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.schema import SCHEMA_VERSION
from llm_bus.mailbox.store import Store
from llm_bus.remote.models import Delivery
from llm_bus.remote.outgoing import enqueue_remote
from llm_bus.remote.store import RemoteStore
from llm_bus.reports.store import Reports
from llm_bus.routing import routing_scope
from tests.coordination.test_work_state import OWNER, WORKER, relay_config, report, send

if TYPE_CHECKING:
    from pathlib import Path
    from sqlite3 import Connection

    from llm_bus.coordination.models import Direction

START = datetime(2026, 1, 1, tzinfo=UTC)
DUE = START + timedelta(minutes=30)


def clock_state(store: Store, message_id: int) -> tuple[str, int]:
    with store.transaction() as db:
        return cast(
            "tuple[str, int]",
            db.execute(
                "SELECT last_communication_at,epoch FROM coordination_stall_state WHERE message_id=?",
                (message_id,),
            ).fetchone(),
        )


def test_admitted_local_events_and_invalid_replies(
    enrolled: tuple[Store, Direction, SenderContext],
) -> None:
    store, _direction, owner = enrolled
    message_id = send(store, owner, "assign")
    initial = clock_state(store, message_id)
    assert initial[1] == 1
    store.acknowledge(WORKER, [message_id])
    assert clock_state(store, message_id) == initial
    report(store, message_id, "proceeding", "started")
    reported = clock_state(store, message_id)
    assert reported[1] == 2
    report(store, message_id, "proceeding", "started")
    worker = SenderContext("claude", "worker", owner.project)
    store.send(worker, OWNER, "unlinked")
    store.send(owner, WORKER, "coordinator reply", reply_to=message_id)
    assert clock_state(store, message_id) == reported
    child = store.send(worker, OWNER, "phase changed", reply_to=message_id)
    assert clock_state(store, message_id)[1] == 3
    store.send(worker, OWNER, "descendant", reply_to=child["id"])
    assert clock_state(store, message_id)[1] == 4
    store.register("codex", "other", owner.project)
    with pytest.raises(BusError, match="participants"):
        store.send(worker, "codex:other", "wrong", reply_to=message_id)
    with store.transaction() as db:
        db.execute("UPDATE messages SET sender_scope='other' WHERE id=?", (message_id,))
    with pytest.raises(BusError, match="scope"):
        store.send(worker, OWNER, "wrong scope", reply_to=message_id)
    assert clock_state(store, message_id)[1] == 4


def test_stall_cli_is_read_only_and_configuration_is_local(
    enrolled: tuple[Store, Direction, SenderContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code
    store, _direction, owner = enrolled
    send(store, owner, "assign")
    statements: list[str] = []
    connect = store._connect  # pyright: ignore[reportPrivateUsage]

    def read_connection() -> Connection:
        db = connect()
        db.execute("PRAGMA query_only=ON")
        db.set_trace_callback(statements.append)
        return db

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("inspection must not apply decisions, fetch, wake or schedule")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_connect", read_connection)
        patch.setattr("llm_bus.coordination.decisions.apply_due", forbidden)
        patch.setattr("llm_bus.remote.bridge.Bridge.sync", forbidden)
        patch.setattr("llm_bus.locks.notices.deliver", forbidden)
        patch.setattr("llm_bus.locks.timer.spawn", forbidden)
        args = vars(
            build_parser().parse_args(["coordination", "stalls", "demo", "--threshold", "1m"])
        )
        result = run(store, args, OWNER, owner.project)
        assert result["threshold_seconds"] == 60
        assert result["items"] == []
    assert not any(sql.startswith(("BEGIN", "INSERT", "UPDATE", "DELETE")) for sql in statements)
    with store.transaction() as db:
        before = list(db.execute("SELECT * FROM coordination_grants"))
    args = vars(
        build_parser().parse_args(["coordination", "stall-config", "demo", "--threshold", "2h"])
    )
    with pytest.raises(BusError, match="coordinator"):
        run(store, args, WORKER, owner.project)
    assert run(store, args, OWNER, owner.project)["threshold_seconds"] == 7200
    with store.transaction() as db:
        assert list(db.execute("SELECT * FROM coordination_grants")) == before
        assert stalls.threshold(db, project(db, routing_scope(owner.project), "demo")) == 7200


def test_remote_events_use_local_acceptance_and_reject_replays(
    enrolled: tuple[Store, Direction, SenderContext],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pylint: disable=too-many-locals
    store, direction, owner = enrolled
    config = relay_config(tmp_path)
    repository = config.repositories[0]
    peer = replace(repository.peers[0], receive_reports=("proceeding",))
    config = replace(config, repositories=(replace(repository, peers=(peer,)),))
    monkeypatch.setattr(stalls, "optional_config", lambda: config)
    monkeypatch.setattr("llm_bus.policy.inbound.optional_config", lambda: config)
    recipient = "b/xyz/claude:worker"
    sent = enqueue_remote(store, config, owner, recipient, "assign", direction=direction)
    message_id = cast("int", sent["id"])
    remote_id = cast("str", sent["remote_id"])
    with store.transaction() as db:
        db.execute(
            "INSERT INTO coordination_targets VALUES (?,?,?,?,?)",
            (
                repository.scope,
                direction.project_id,
                recipient,
                direction.grant_id,
                direction.generation,
            ),
        )
    remote = RemoteStore(store, config.connection)
    envelope = remote.outgoing()[0][0]
    remote.set_state(remote_id, "acknowledged")
    assert clock_state(store, message_id)[1] == 1
    reports = Reports(store, config.connection)
    value = {"message_id": remote_id, "revision": 1, "status": "proceeding", "reason": "started"}
    before = datetime.now(UTC)
    reports.receive(envelope, value, config)
    observed = clock_state(store, message_id)
    assert before <= datetime.fromisoformat(observed[0]) <= datetime.now(UTC)
    assert observed[1] == 2
    reports.receive(envelope, value, config)
    with pytest.raises(BusError, match="revision"):
        reports.receive(envelope, {**value, "revision": 0, "status": None, "reason": None}, config)
    assert clock_state(store, message_id) == observed
    reply = Delivery(
        id=str(uuid4()),
        sequence=1,
        sender=recipient,
        recipient="a/xyz/codex:owner",
        repo_id="xyz",
        body="phase changed",
        sent_at="2099-01-01T00:00:00Z",
        reply_to=remote_id,
    )
    before = datetime.now(UTC)
    assert remote.admit_message(reply, repository.scope) is not None
    replied = clock_state(store, message_id)
    assert replied[1] == 3
    assert before <= datetime.fromisoformat(replied[0]) <= datetime.now(UTC)
    assert remote.admit_message(reply, repository.scope) is not None
    assert clock_state(store, message_id) == replied
    unlinked = reply.copy()
    unlinked["id"] = str(uuid4())
    del unlinked["reply_to"]
    assert remote.admit_message(unlinked, repository.scope) is not None
    assert clock_state(store, message_id) == replied
    denied = replace(
        config,
        repositories=(
            replace(
                repository, peers=(replace(peer, receive_messages="deny", receive_reports=()),)
            ),
        ),
    )
    monkeypatch.setattr("llm_bus.policy.inbound.optional_config", lambda: denied)
    assert remote.admit_message({**reply, "id": str(uuid4())}, repository.scope) is None
    with pytest.raises(BusError, match="policy"):
        reports.receive(envelope, {**value, "revision": 2}, denied)
    assert clock_state(store, message_id) == replied


def setup_clock(db: Connection) -> dict[str, object]:
    db.execute("DELETE FROM coordination_stall_state")
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
        db.execute(
            "UPDATE coordination_stall_state SET last_communication_at=?", (DUE.isoformat(),)
        )
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


@pytest.mark.parametrize("idle_address", [OWNER, WORKER])
def test_idle_cutoff_is_effective_without_persisting_expiry(
    enrolled: tuple[Store, Direction, SenderContext], idle_address: str
) -> None:
    store, _direction, owner = enrolled
    send(store, owner, "assign")
    cutoff = START + timedelta(hours=24)
    with store.transaction() as db:
        record = setup_clock(db)
        db.execute("UPDATE agents SET last_seen=?", (cutoff.isoformat(),))
        db.execute(
            "UPDATE agents SET last_seen=? WHERE address=?", (START.isoformat(), idle_address)
        )
        grant = cast(
            "tuple[str]", db.execute("SELECT grant_id FROM coordination_grants").fetchone()
        )[0]
        db.execute("INSERT INTO coordination_session_consents VALUES (?,?,NULL)", (grant, WORKER))
        stalls.reconcile(db, record, now=DUE)
        before = db.total_changes
        assert len(items(db, record, cutoff - timedelta(microseconds=1))) == 1
        for now in (cutoff, cutoff + timedelta(microseconds=1)):
            assert list(stalls.eligible(db, record, now=now)) == []
            if idle_address == OWNER:
                with pytest.raises(BusError, match="coordinator"):
                    items(db, record, now)
            else:
                assert items(db, record, now) == []
        assert db.total_changes == before


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


@pytest.mark.parametrize("snapshot_revision", [None, 0])
def test_remote_acknowledgment_retains_known_age_without_report(  # pylint: disable=too-many-locals
    enrolled: tuple[Store, Direction, SenderContext],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot_revision: int | None,
) -> None:
    store, direction, owner = enrolled
    config = relay_config(tmp_path)
    recipient = "b/xyz/claude:worker"
    sent = enqueue_remote(store, config, owner, recipient, "assign", direction=direction)
    remote = RemoteStore(store, config.connection)
    remote_id = str(sent["remote_id"])
    remote.set_state(remote_id, "acknowledged")
    monkeypatch.setattr(stalls, "optional_config", lambda: config)
    with store.transaction() as db:
        record = project(db, routing_scope(owner.project), "demo")
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
        if snapshot_revision is not None:
            db.execute(
                "INSERT INTO remote_reports VALUES (?,?)",
                (
                    remote_id,
                    json.dumps(
                        {"message_id": remote_id, "revision": 0, "status": None, "reason": None}
                    ),
                ),
            )
        acknowledged = cast(
            "str",
            db.execute("SELECT acknowledged_at FROM messages WHERE id=?", (sent["id"],)).fetchone()[
                0
            ],
        )
        recovered_at = datetime.fromisoformat(acknowledged) + timedelta(hours=2)
        before = db.total_changes
        found = items(db, record, recovered_at)
        assert len(found) == 1
        item = found[0]
        assert item["last_communication_at"] == acknowledged
        assert (
            item["due_at"]
            == (datetime.fromisoformat(acknowledged) + timedelta(minutes=30)).isoformat()
        )
        assert item["silence_seconds"] == 7200
        assert item["observation"] == "remote_observed"
        assert db.total_changes == before
        stalls.reconcile(db, record, now=recovered_at)
        assert items(db, record, recovered_at) == found
    remote.set_state(remote_id, "acknowledged")
    with store.transaction() as db:
        stalls.reconcile(db, record, now=recovered_at + timedelta(minutes=5))
        assert items(db, record, recovered_at)[0]["last_communication_at"] == acknowledged
        assert items(db, record, recovered_at)[0]["epoch"] == 1


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
        db.execute(
            "INSERT INTO coordination_stall_timers VALUES (?,?,?,?,?,?)",
            (
                record["scope"],
                record["project_id"],
                record["generation"],
                "retained-token",
                DUE.isoformat(),
                DUE.isoformat(),
            ),
        )
        db.execute("PRAGMA user_version=15")
    upgraded = Store(store.path)
    with upgraded.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 16
        assert stalls.threshold(db, record) == 3600
        assert db.execute(
            "SELECT token,lease_until,due_at FROM coordination_stall_timers"
        ).fetchone() == ("retained-token", DUE.isoformat(), DUE.isoformat())
        assert db.execute("SELECT count(*) FROM coordination_stall_state").fetchone()[0] == 0


def timer_claim(store: Store) -> tuple[str, str, str] | None:
    with store.transaction() as db:
        return cast(
            "tuple[str, str, str] | None",
            db.execute("SELECT token,lease_until,due_at FROM coordination_stall_timers").fetchone(),
        )


def prepare_timer(store: Store) -> dict[str, object]:
    with store.transaction() as db:
        record = setup_clock(db)
        db.execute("DELETE FROM coordination_stall_timers")
    return record


def notices_for(store: Store) -> list[tuple[int, str]]:
    with store.transaction() as db:
        return list(db.execute("SELECT id,body FROM messages WHERE sender=recipient"))


def test_concurrent_hooks_persist_once_and_real_reply_rearms(
    enrolled: tuple[Store, Direction, SenderContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _direction, owner = enrolled
    message_id = send(store, owner, "assign")
    prepare_timer(store)
    delivered: list[int] = []

    def dispatch(mailbox: Store, _recipient: str, *, trigger_id: int) -> dict[str, str]:
        # A distinct connection must see both notice and marker before the wake.
        with mailbox.transaction() as db:
            assert (
                db.execute(
                    "SELECT notice_message_id FROM coordination_stall_state WHERE message_id=?",
                    (message_id,),
                ).fetchone()[0]
                == trigger_id
            )
        delivered.append(trigger_id)
        raise RuntimeError("host unavailable")

    monkeypatch.setattr(notices, "dispatch_pending", dispatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(stalls.apply_for_coordinator, store, OWNER, now=DUE) for _ in range(2)
        ]
        for future in futures:
            future.result()
    first = notices_for(store)
    assert len(first) == 1
    assert delivered == [first[0][0]]
    assert f"direction #{message_id}" in first[0][1]
    assert "informational" in first[0][1]
    store.acknowledge(OWNER, [first[0][0]])
    stalls.apply_for_coordinator(store, OWNER, now=DUE + timedelta(minutes=1))
    assert notices_for(store) == first
    worker = SenderContext("claude", "worker", owner.project)
    store.send(worker, OWNER, "PRIVATE WORKER TEXT", reply_to=message_id)
    at = datetime.fromisoformat(clock_state(store, message_id)[0])
    stalls.apply_for_coordinator(store, OWNER, now=at + timedelta(minutes=30))
    assert len(notices_for(store)) == 2
    assert all("PRIVATE WORKER TEXT" not in body for _identifier, body in notices_for(store))


def test_timer_claim_reuse_earlier_deadline_failure_and_lease_recovery(
    enrolled: tuple[Store, Direction, SenderContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _direction, owner = enrolled
    send(store, owner, "assign")
    record = prepare_timer(store)
    launches: list[tuple[tuple[str, ...], object]] = []

    def launch(*args: str, database: object) -> bool:
        launches.append((args, database))
        return True

    monkeypatch.setattr(stalls, "spawn_checked", launch)
    stalls.schedule(store, record, now=START)
    first = timer_claim(store)
    assert first is not None
    assert first[2] == DUE.isoformat()
    assert launches[0][1] == store.path
    assert launches[0][0][2] == record["project_id"]
    stalls.schedule(store, record, now=START + timedelta(seconds=1))
    assert timer_claim(store) == first
    assert len(launches) == 1
    with store.transaction() as db:
        stalls.configure(db, record, OWNER, 60)
    stalls.schedule(store, record, now=START + timedelta(seconds=2))
    earlier = timer_claim(store)
    assert earlier is not None
    assert earlier[0] != first[0]
    assert earlier[2] == (START + timedelta(minutes=1)).isoformat()
    stalls.run_timer(
        store,
        str(record["scope"]),
        str(record["project_id"]),
        str(record["generation"]),
        first[0],
        clock=lambda: START,
    )
    assert notices_for(store) == []
    expiry = datetime.fromisoformat(earlier[1])

    def failed_launch(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(stalls, "spawn_checked", failed_launch)
    stalls.schedule(store, record, now=expiry)
    assert timer_claim(store) is None
    monkeypatch.setattr(stalls, "spawn_checked", launch)
    stalls.schedule(store, record, now=expiry)
    recovered = timer_claim(store)
    assert recovered is not None
    assert recovered[0] != earlier[0]
    assert len(launches) == 3


def test_timer_bounded_sleep_cwd_independence_and_hook_race(
    enrolled: tuple[Store, Direction, SenderContext],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _direction, owner = enrolled
    send(store, owner, "assign")
    record = prepare_timer(store)
    stalls.schedule(store, record, now=START)
    claim = timer_claim(store)
    assert claim is not None
    moment = START
    slept: list[float] = []
    monkeypatch.chdir(tmp_path.parent)

    def unsupported(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"status": "unsupported"}

    monkeypatch.setattr(notices, "dispatch_pending", unsupported)

    def sleep(seconds: float) -> None:
        nonlocal moment
        slept.append(seconds)
        moment += timedelta(seconds=seconds)
        if moment == DUE:
            stalls.apply_for_coordinator(store, OWNER, now=moment)

    stalls.run_timer(
        store,
        str(record["scope"]),
        str(record["project_id"]),
        str(record["generation"]),
        claim[0],
        sleep=sleep,
        clock=lambda: moment,
    )
    assert slept == [TIMER_POLL_SECONDS, TIMER_POLL_SECONDS]
    assert len(notices_for(store)) == 1
    assert timer_claim(store) is None


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE coordination_grants SET revoked_at='2026-01-01'",
        "UPDATE coordination_projects SET generation='replacement'",
        "UPDATE coordination_projects SET revoked_at='2026-01-01'",
        "DELETE FROM coordination_enrollments WHERE role='coordinator'",
        "UPDATE agents SET ended_at='2026-01-01' WHERE address='codex:owner'",
    ],
)
def test_timer_rechecks_lifecycle_before_notice(
    enrolled: tuple[Store, Direction, SenderContext],
    mutation: str,
) -> None:
    store, _direction, owner = enrolled
    send(store, owner, "assign")
    record = prepare_timer(store)
    stalls.schedule(store, record, now=START)
    claim = timer_claim(store)
    assert claim is not None
    moment = START

    def sleep(seconds: float) -> None:
        nonlocal moment
        moment += timedelta(seconds=seconds)
        with store.transaction() as db:
            db.execute(mutation)

    stalls.run_timer(
        store,
        str(record["scope"]),
        str(record["project_id"]),
        str(record["generation"]),
        claim[0],
        sleep=sleep,
        clock=lambda: moment,
    )
    assert notices_for(store) == []


def test_worker_and_native_child_hook_cannot_start_detection(
    enrolled: tuple[Store, Direction, SenderContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _direction, owner = enrolled
    send(store, owner, "assign")
    prepare_timer(store)
    stalls.apply_for_coordinator(store, WORKER, now=DUE)

    def child(_address: str) -> bool:
        return True

    monkeypatch.setattr(stalls, "is_subagent", child)
    stalls.apply_for_coordinator(store, OWNER, now=DUE)
    assert notices_for(store) == []
    assert timer_claim(store) is None


@pytest.mark.parametrize("terminal", ["cancel", "completed"])
def test_terminal_work_stops_running_timer(
    enrolled: tuple[Store, Direction, SenderContext],
    terminal: str,
) -> None:
    store, _direction, owner = enrolled
    message_id = send(store, owner, "assign")
    record = prepare_timer(store)
    stalls.schedule(store, record, now=START)
    claim = timer_claim(store)
    assert claim is not None
    moment = START

    def sleep(seconds: float) -> None:
        nonlocal moment
        moment += timedelta(seconds=seconds)
        if terminal == "cancel":
            send(store, owner, "cancel")
        else:
            report(store, message_id, "completed")

    stalls.run_timer(
        store,
        str(record["scope"]),
        str(record["project_id"]),
        str(record["generation"]),
        claim[0],
        sleep=sleep,
        clock=lambda: moment,
    )
    assert notices_for(store) == []
    assert timer_claim(store) is None


def test_scheduled_hidden_dispatch_reaches_durable_notice(
    enrolled: tuple[Store, Direction, SenderContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _direction, owner = enrolled
    send(store, owner, "assign")
    record = prepare_timer(store)
    actual_timer = stalls.run_timer
    moment = START
    dispatched: list[dict[str, object]] = []

    def sleep(seconds: float) -> None:
        nonlocal moment
        moment += timedelta(seconds=seconds)

    def run_bound_timer(
        mailbox: Store, scope: str, project_id: str, generation: str, token: str
    ) -> None:
        actual_timer(
            mailbox, scope, project_id, generation, token, sleep=sleep, clock=lambda: moment
        )

    def launch(*args: str, database: object) -> bool:
        assert database == store.path
        parsed = cast("dict[str, object]", vars(build_parser().parse_args(args)))
        dispatched.append(parsed)
        # Equivalent hidden-child path, without a real fork or host environment.
        assert human_command(store, parsed["command"], parsed)
        return True

    def unsupported(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"status": "unsupported"}

    monkeypatch.setattr(stalls, "spawn_checked", launch)
    monkeypatch.setattr(stalls, "run_timer", run_bound_timer)
    monkeypatch.setattr("llm_bus.cli.commands.detach", lambda: False)
    monkeypatch.setattr(notices, "dispatch_pending", unsupported)
    stalls.schedule(store, record, now=START)
    assert len(dispatched) == 1
    assert moment == DUE
    assert len(notices_for(store)) == 1
    assert timer_claim(store) is None


@pytest.mark.parametrize("failure", [None, "exit", "os", "timeout"])
def test_checked_launcher_reports_outcome_and_binds_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    calls: list[dict[str, object]] = []

    def run_process(command: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(options)
        assert command[-2:] == ["coordination", "stall-timer"]
        if failure == "os":
            raise OSError("launch unavailable")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 10)
        return subprocess.CompletedProcess(command, 1 if failure == "exit" else 0)

    monkeypatch.setattr("llm_bus.locks.timer.subprocess.run", run_process)
    database = tmp_path / "isolated.sqlite"
    assert lock_timer.spawn_checked("coordination", "stall-timer", database=database) == (
        failure is None
    )
    assert cast("dict[str, str]", calls[0]["env"])["LLM_BUS_DB"] == str(database.resolve())


def test_notice_and_marker_rollback_together(
    enrolled: tuple[Store, Direction, SenderContext],
) -> None:
    store, _direction, owner = enrolled
    send(store, owner, "assign")
    record = prepare_timer(store)
    with store.transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_marker BEFORE UPDATE OF notified_epoch ON coordination_stall_state "
            "BEGIN SELECT RAISE(ABORT,'marker failed'); END"
        )

    def persist() -> None:
        with store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            stalls.apply_due(store, db, record, now=DUE)

    with pytest.raises(sqlite3.IntegrityError, match="marker failed"):
        persist()
    assert notices_for(store) == []
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM coordination_stall_state").fetchone()[0] == 0
