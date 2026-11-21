"""Main-mailbox retention deletes content without losing durable safety state."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from llm_bus.coordination import availability
from llm_bus.coordination.models import Availability
from llm_bus.coordination.recovery import create_tables
from llm_bus.errors import BusError
from llm_bus.mailbox import retention
from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.retention_schema import allocate_id
from llm_bus.mailbox.store import Store
from llm_bus.remote.store import RemoteStore
from tests.coordination.test_enrollment import create, invoke

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2030, 1, 15, tzinfo=UTC)
OLD = "2030-01-01T00:00:00.000Z"
RECENT = "2030-01-14T00:00:00.000Z"


def _mailbox(tmp_path: Path) -> Store:
    mailbox = Store(tmp_path / "bus.db")
    mailbox.register("claude", "recipient", str(tmp_path))
    mailbox.register("codex", "sender", str(tmp_path))
    return mailbox


def _message(mailbox: Store, *, at: str = OLD, reply_to: int | None = None) -> int:
    sent = mailbox.send(
        SenderContext("codex", "sender", "/project"),
        "claude:recipient",
        "message body",
        cross_folder=True,
        reply_to=reply_to,
    )
    with mailbox.transaction() as db:
        db.execute("UPDATE messages SET sent_at=? WHERE id=?", (at, sent["id"]))
    return sent["id"]


def _purge(mailbox: Store, *, limit: int = 100) -> dict[str, int]:
    with mailbox.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        return retention.purge(db, now=NOW, limit=limit)


def test_atomic_dependencies_reply_replay_and_provider_state(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    parent = _message(mailbox)
    imported = _message(mailbox)
    child = _message(mailbox, at=RECENT, reply_to=parent)
    outgoing_id, incoming_id, monitor_id = (str(uuid4()) for _ in range(3))
    child_envelope = json.dumps({"body": "recent reply", "reply_to": outgoing_id})
    with mailbox.transaction() as db:
        create_tables(db)
        db.execute("INSERT INTO remote_outbox VALUES (?,?,?,'queued')", (outgoing_id, parent, "{}"))
        db.execute(
            "INSERT INTO remote_imports(id,local_id,envelope) VALUES (?,?,?)",
            (incoming_id, imported, "{}"),
        )
        db.execute(
            "INSERT INTO remote_imports(id,local_id,envelope) VALUES (?,?,?)",
            (str(uuid4()), child, child_envelope),
        )
        db.execute("UPDATE messages SET acknowledged_at=sent_at WHERE id=?", (imported,))
        db.execute("INSERT INTO remote_report_submissions VALUES (?,1,'private',0)", (incoming_id,))
        db.execute("INSERT INTO remote_reports VALUES (?,'private')", (outgoing_id,))
        db.execute("INSERT INTO remote_report_windows VALUES (?,'2030-02-01')", (outgoing_id,))
        for kind in ("upload", "status", "report_read", "report_submit"):
            db.execute("INSERT INTO remote_sync_work(kind,key) VALUES (?,?)", (kind, outgoing_id))
        db.execute("INSERT INTO remote_sync_work(kind,key) VALUES ('ack',?)", (incoming_id,))
        db.execute("INSERT INTO remote_sync_work(kind,key) VALUES ('status','unrelated')")
        db.execute("INSERT INTO coordination_messages VALUES (?,'private')", (parent,))
        db.execute("INSERT INTO coordination_approvals VALUES (?,'private')", (parent,))
        db.execute(
            "INSERT INTO coordination_work_reports VALUES (?,1,'working',NULL,?)", (parent, OLD)
        )
        db.execute("INSERT INTO wake_coverage VALUES (?,'token','delivered',?)", (parent, OLD))
        db.execute(
            "INSERT INTO coordination_run_merges VALUES (?,'run','ticket','provider-operation','unknown')",
            (parent,),
        )
        db.execute(
            "INSERT INTO remote_monitor(monitor_id,id,envelope,scope,connection,retention_at) VALUES (?,?,?,'scope','connection',?)",
            (allocate_id(db, "monitor"), monitor_id, '{"body":"private"}', OLD),
        )
    assert _purge(mailbox) == {"messages": 2, "monitors": 1}
    with mailbox.transaction() as db:
        assert db.execute("SELECT id,body,reply_to,sent_at FROM messages").fetchall() == [
            (child, "message body", None, RECENT)
        ]
        assert db.execute("SELECT envelope FROM remote_imports").fetchone() == (child_envelope,)
        for table in (
            "remote_outbox",
            "remote_monitor",
            "remote_report_submissions",
            "remote_reports",
            "remote_report_windows",
            "coordination_messages",
            "coordination_approvals",
            "coordination_work_reports",
            "wake_coverage",
        ):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)  # noqa: S608
        assert {
            row[0]
            for row in cast(
                "list[tuple[str]]", db.execute("SELECT id FROM remote_replay_guard").fetchall()
            )
        } == {outgoing_id, incoming_id, monitor_id}
        assert db.execute("SELECT key FROM remote_sync_work").fetchall() == [("unrelated",)]
        assert db.execute("SELECT payload,status FROM coordination_run_merges").fetchone() == (
            "provider-operation",
            "unknown",
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _purge(mailbox) == {"messages": 0, "monitors": 0}
    with (
        pytest.raises(sqlite3.IntegrityError, match="Purged remote message ID"),
        mailbox.transaction() as db,
    ):
        db.execute("INSERT INTO remote_outbox VALUES (?,?,?,'queued')", (outgoing_id, child, "{}"))


def test_boundary_fair_batches_and_monotonic_ids(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    ids = [_message(mailbox) for _ in range(3)]
    retained = _message(mailbox, at="2030-01-01T00:00:00.001Z")
    with mailbox.transaction() as db:
        for _ in range(3):
            db.execute(
                "INSERT INTO remote_monitor(monitor_id,id,envelope,scope,connection,retention_at) VALUES (?,?,?,'s','c',?)",
                (allocate_id(db, "monitor"), str(uuid4()), "{}", OLD),
            )
        plan = cast(
            "list[tuple[int,int,int,str]]",
            db.execute(
                "EXPLAIN QUERY PLAN SELECT monitor_id FROM remote_monitor WHERE retention_at<=? ORDER BY retention_at,monitor_id LIMIT 2",
                (OLD,),
            ).fetchall(),
        )
        assert any("remote_monitor_retention" in row[3] for row in plan)
        for query, parameters, index in (
            ("SELECT 1 FROM remote_sync_work WHERE key=?", ("uuid",), "remote_sync_work_key"),
            (
                "SELECT 1 FROM coordination_stall_state WHERE notice_message_id=?",
                (1,),
                "coordination_stall_notice",
            ),
            (
                "SELECT 1 FROM coordination_stall_state WHERE scope=? AND project_id=? AND generation=?",
                ("s", "p", "g"),
                "coordination_stall_binding",
            ),
        ):
            plan = cast(
                "list[tuple[int,int,int,str]]",
                db.execute(f"EXPLAIN QUERY PLAN {query}", parameters).fetchall(),
            )
            assert any(index in row[3] for row in plan)
    assert _purge(mailbox, limit=2) == {"messages": 2, "monitors": 2}
    with mailbox.transaction() as db:
        assert db.execute("SELECT id FROM messages ORDER BY id").fetchall() == [
            (ids[-1],),
            (retained,),
        ]
    assert _purge(mailbox, limit=2) == {"messages": 1, "monitors": 1}
    with mailbox.transaction() as db:
        assert allocate_id(db, "message") > retained
        assert allocate_id(db, "monitor") == 4


def test_failure_rolls_back_content_ledger_and_detachment(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    parent = _message(mailbox)
    child = _message(mailbox, at=RECENT, reply_to=parent)
    remote_id = str(uuid4())
    with mailbox.transaction() as db:
        db.execute("INSERT INTO remote_outbox VALUES (?,?,?,'queued')", (remote_id, parent, "{}"))
        db.execute(
            "CREATE TRIGGER reject_purge BEFORE DELETE ON messages BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        _purge(mailbox)
    with mailbox.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone() == (2,)
        assert db.execute("SELECT reply_to FROM messages WHERE id=?", (child,)).fetchone() == (
            parent,
        )
        assert db.execute("SELECT COUNT(*) FROM remote_outbox").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM remote_replay_guard").fetchone() == (0,)


def test_bad_candidate_stops_before_any_deletion(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    _message(mailbox)
    with mailbox.transaction() as db:
        db.execute("UPDATE messages SET retention_at='2000-02-30T00:00:00.000Z'")
    with pytest.raises(BusError, match="Invalid message retention timestamp"):
        _purge(mailbox)
    with mailbox.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone() == (1,)


def test_write_maintenance_rate_limit_and_read_only_store(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    _message(mailbox)
    with mailbox.transaction() as db:
        db.execute("UPDATE mailbox_maintenance SET next_at='2000-01-01T00:00:00.000Z'")
    # A read-only store must not consume the due maintenance, even via transaction().
    with Store.read_only(mailbox.path).transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone() == (1,)
    # The next write uses its own real clock; use an age eligible on supported hosts.
    with sqlite3.connect(mailbox.path) as db:
        db.execute("UPDATE messages SET sent_at='2000-01-01T00:00:00.000Z'")
    mailbox.register("codex", "new", str(tmp_path))
    with sqlite3.connect(mailbox.path) as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
        next_at = cast(
            "tuple[str]", db.execute("SELECT next_at FROM mailbox_maintenance").fetchone()
        )
        db.execute(
            "INSERT INTO messages(sender,recipient,body,sent_at) VALUES ('codex:sender','claude:recipient','expired','2000-01-01T00:00:00Z')"
        )
    Store(mailbox.path).register("codex", "newer", str(tmp_path))
    with sqlite3.connect(mailbox.path) as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone() == (1,)
        assert db.execute("SELECT next_at FROM mailbox_maintenance").fetchone() == next_at


def test_local_availability_dedup_survives_purge_and_keeps_authority(tmp_path: Path) -> None:
    mailbox = Store(tmp_path / "bus.db")
    create(mailbox, tmp_path)
    first = invoke(mailbox, tmp_path, "join", "demo", address="claude:worker")
    with mailbox.transaction() as db:
        db.execute("UPDATE messages SET sent_at=?", (OLD,))
        before = {
            table: db.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
            for table in (
                "coordination_projects",
                "coordination_grants",
                "coordination_targets",
                "coordination_enrollments",
            )
        }
    assert _purge(mailbox)["messages"] == 1
    second = invoke(mailbox, tmp_path, "join", "demo", address="claude:worker")
    assert first["grants"] == second["grants"]
    with mailbox.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
        for table, rows in before.items():
            assert db.execute(f"SELECT * FROM {table}").fetchall() == rows  # noqa: S608


def test_existing_bridge_roster_cycle_runs_due_maintenance(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    remote = RemoteStore(mailbox, "disposable-connection")
    _message(mailbox)
    with sqlite3.connect(mailbox.path) as db:
        db.execute("UPDATE messages SET sent_at='2000-01-01T00:00:00.000Z'")
        db.execute("UPDATE mailbox_maintenance SET next_at='2000-01-01T00:00:00.000Z'")
    assert len(remote.sessions()) == 2
    with sqlite3.connect(mailbox.path) as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)


def test_remote_availability_ordering_survives_message_deletion(tmp_path: Path) -> None:
    mailbox = Store(tmp_path / "bus.db")
    record = create(mailbox, tmp_path)
    message = mailbox.send(SenderContext("codex", "owner", str(tmp_path)), "codex:owner", "arrival")
    with mailbox.transaction() as db:
        db.execute("UPDATE messages SET sent_at=?", (OLD,))
        project = cast(
            "tuple[str,str,str]",
            db.execute("SELECT scope,project_id,generation FROM coordination_projects").fetchone(),
        )
        event = Availability(1, project[1], project[2], str(uuid4()), "joined")
        assert availability.accept(
            db,
            sender="peer/repo/claude:worker",
            recipient="codex:owner",
            scope=project[0],
            event=event,
            message_id=message["id"],
            sent_at="2030-01-01T00:00:00.000999Z",
            event_id=str(uuid4()),
        )
    # The event is a content-free binding; it requires no remaining envelope or FK.
    assert _purge(mailbox)["messages"] == 1
    with mailbox.transaction() as db:
        for event_age, candidate in (
            (RECENT, event),
            (
                "2029-12-31T00:00:00Z",
                Availability(1, project[1], project[2], str(uuid4()), "joined"),
            ),
            (
                "2030-01-01T00:00:00.000998Z",
                Availability(1, project[1], project[2], str(uuid4()), "joined"),
            ),
            (RECENT, Availability(1, project[1], str(uuid4()), str(uuid4()), "joined")),
        ):
            assert not availability.accept(
                db,
                sender="peer/repo/claude:worker",
                recipient="codex:owner",
                scope=project[0],
                event=candidate,
                message_id=901,
                sent_at=event_age,
                event_id=str(uuid4()),
            )
        assert db.execute("SELECT grant_id FROM coordination_targets").fetchone() == (
            event.grant_id,
        )
        assert db.execute("SELECT COUNT(*) FROM coordination_grants").fetchone() == (0,)
    assert record["project_id"] == event.project_id


def test_stall_notice_timer_and_wake_leases_do_not_reopen(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    expired = _message(mailbox)
    surviving = _message(mailbox, at=RECENT)
    with mailbox.transaction() as db:
        db.execute(
            "INSERT INTO coordination_stall_state VALUES (?,'scope','project','generation','[]',?,'local',3,3,?)",
            (surviving, RECENT, expired),
        )
        db.execute(
            "INSERT INTO coordination_stall_state VALUES (?,'scope','old-project','generation','[]',?,'local',1,1,NULL)",
            (expired, OLD),
        )
        for project in ("project", "old-project"):
            db.execute(
                "INSERT INTO coordination_stall_timers VALUES ('scope',?,'generation','timer-token',?,?)",
                (project, RECENT, OLD),
            )
        db.execute(
            "UPDATE wake_dispatch SET token='wake-token',lease_until=?,next_attempt_at=?,attempts=2",
            (RECENT, RECENT),
        )
    assert _purge(mailbox)["messages"] == 1
    with mailbox.transaction() as db:
        assert db.execute(
            "SELECT message_id,epoch,notified_epoch,notice_message_id FROM coordination_stall_state"
        ).fetchall() == [(surviving, 3, 3, None)]
        assert db.execute("SELECT project_id,token FROM coordination_stall_timers").fetchall() == [
            ("project", "timer-token")
        ]
        assert db.execute("SELECT token,requested_generation FROM wake_dispatch").fetchone() == (
            "wake-token",
            2,
        )
        db.execute("UPDATE messages SET sent_at=?", (OLD,))
    assert _purge(mailbox)["messages"] == 1
    with mailbox.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM coordination_stall_timers").fetchone() == (0,)
        assert db.execute(
            "SELECT token,lease_until,next_attempt_at,attempts,requested_generation,dispatched_generation FROM wake_dispatch"
        ).fetchone() == (None, None, None, 0, 2, 2)


@pytest.mark.parametrize("valid", [True, False])
def test_legacy_availability_binding_migration_is_atomic(tmp_path: Path, *, valid: bool) -> None:
    mailbox = _mailbox(tmp_path)
    message = _message(mailbox)
    remote_id = str(uuid4())
    event_id, project_id, generation, grant_id = (str(uuid4()) for _ in range(4))
    with mailbox.transaction() as db:
        envelope = (
            json.dumps(
                {
                    "availability": {
                        "project_id": project_id,
                        "generation": generation,
                        "grant_id": grant_id,
                    }
                }
            )
            if valid
            else "{}"
        )
        db.execute(
            "INSERT INTO remote_imports(id,local_id,envelope) VALUES (?,?,?)",
            (remote_id, message, envelope),
        )
        db.execute("DROP TABLE coordination_availability_events")
        db.execute(
            "CREATE TABLE coordination_availability_events(message_id INTEGER PRIMARY KEY REFERENCES messages(id),grant_id TEXT NOT NULL,sender TEXT NOT NULL,sent_at TEXT NOT NULL,event_id TEXT NOT NULL,UNIQUE(sender,grant_id))"
        )
        db.execute(
            "INSERT INTO coordination_availability_events VALUES (?,?,?,?,?)",
            (message, grant_id, "peer/repo/codex:sender", OLD, event_id),
        )
        db.execute("PRAGMA user_version=19")
    if not valid:
        with pytest.raises(BusError, match="Invalid availability binding"):
            Store(mailbox.path)
        with sqlite3.connect(mailbox.path) as db:
            assert db.execute("PRAGMA user_version").fetchone() == (19,)
            assert db.execute("SELECT * FROM coordination_availability_events").fetchone() == (
                message,
                grant_id,
                "peer/repo/codex:sender",
                OLD,
                event_id,
            )
        return
    reopened = Store(mailbox.path)
    with reopened.transaction() as db:
        assert db.execute(
            "SELECT recipient,scope,project_id,generation FROM coordination_availability_events"
        ).fetchone() == ("claude:recipient", str(tmp_path), project_id, generation)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        db.execute("BEGIN IMMEDIATE")
        retention.purge(db, now=NOW)
        assert db.execute("SELECT COUNT(*) FROM coordination_availability_events").fetchone() == (
            1,
        )


def test_concurrent_maintenance_consumes_one_rate_limited_batch(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    with mailbox.transaction() as db:
        db.executemany(
            "INSERT INTO messages(id,sender,recipient,body,sent_at) "
            "VALUES (?,'codex:sender','claude:recipient','expired',?)",
            [(message_id, OLD) for message_id in range(1, 102)],
        )

    def maintain(_index: int) -> None:
        with sqlite3.connect(mailbox.path, timeout=10) as db:
            db.execute("PRAGMA foreign_keys=ON")
            retention.maintain(db, now=NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(maintain, range(2)))
    with sqlite3.connect(mailbox.path) as db:
        assert db.execute("SELECT id FROM messages").fetchall() == [(101,)]
        assert db.execute("SELECT next_at FROM mailbox_maintenance").fetchone() == (
            "2030-01-15T00:01:00.000Z",
        )
