"""Retention is enforced before effects, independently of physical maintenance."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar, cast
from uuid import uuid4

import pytest

from llm_bus.errors import BusError
from llm_bus.mailbox import eligibility, retention
from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.store import Store
from llm_bus.remote import admission, incoming, outgoing
from llm_bus.remote.bridge import Bridge
from llm_bus.remote.store import RemoteStore
from llm_bus.reports import snapshots
from llm_bus.reports.store import Reports
from llm_bus.reports.sync import submit_reports
from llm_bus.wake.dispatch import dispatch_pending

from .support import allow_reports, delivery, deny_inbox, monitor_only, setup_machine
from .test_coordination import availability_fixture, direction_fixture, enroll

if TYPE_CHECKING:
    from pathlib import Path

    from llm_bus.remote.models import Envelope


class Clock(datetime):
    instant: ClassVar[datetime]

    @classmethod
    def now(cls, tz: object = None) -> datetime:
        del tz
        return cls.instant


@pytest.fixture(name="clock")
def retention_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    now = datetime.now(UTC).replace(microsecond=0)
    Clock.instant = now
    monkeypatch.setattr(eligibility, "datetime", Clock)
    monkeypatch.setattr(admission, "datetime", Clock)
    monkeypatch.setattr(snapshots, "datetime", Clock)

    # Leave expired rows physically present to prove visibility does not depend on purge.
    def no_maintenance(_db: sqlite3.Connection) -> None:
        return None

    monkeypatch.setattr(retention, "maintain", no_maintenance)
    return now


@pytest.mark.parametrize("kind", ["ordinary", "monitor", "direction", "availability"])
@pytest.mark.parametrize("milliseconds", [-1, 0, 1])
def test_first_admission_delivery_boundary(
    tmp_path: Path, clock: datetime, kind: str, milliseconds: int
) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    item = delivery()
    if kind == "monitor":
        monitor_only()
    elif kind == "direction":
        direction = direction_fixture()
        enroll(store, config, direction)
        item["coordination"] = direction.wire()
    elif kind == "availability":
        item["availability"] = availability_fixture(store, config)
    item["sent_at"] = (
        clock - timedelta(hours=48) - timedelta(milliseconds=milliseconds)
    ).isoformat()
    admitted = RemoteStore(store, config.connection).admit_message(
        item, config.repositories[0].scope
    )
    assert (admitted is not None) == (milliseconds < 0)
    if kind == "monitor":
        assert store.pending_members("claude:recipient") == []


def test_admitted_item_survives_delivery_deadline_until_retention(
    tmp_path: Path, clock: datetime
) -> None:
    store, config = setup_machine(tmp_path, "b")
    recipient = store.register("claude", "recipient", str(tmp_path / "repo"))
    item = delivery()
    item["sent_at"] = clock.isoformat()
    remote = RemoteStore(store, config.connection)
    admitted = remote.admit_message(item, config.repositories[0].scope)
    assert admitted is not None
    Clock.instant = clock + timedelta(days=3)
    assert remote.admit_message(item, config.repositories[0].scope) == ("inbox", admitted[1], False)
    assert len(store.inbox(recipient)) == 1
    assert store.acknowledge(recipient, [cast("int", admitted[1])]) == 1
    Clock.instant = clock + timedelta(days=14)
    assert remote.admit_message(item, config.repositories[0].scope) is None
    assert store.history(folder=None) == []
    assert remote.acknowledgements() == []
    with pytest.raises(BusError, match="Unknown message ID"):
        store.message_status(cast("int", admitted[1]))


def test_buffered_inbox_crossing_delivery_deadline_is_not_receipted(
    tmp_path: Path, clock: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    item = delivery()
    item["sent_at"] = (clock - timedelta(hours=48) + timedelta(seconds=1)).isoformat()

    def request(_self: object, _method: str, route: str, _data: object = None) -> object:
        assert route.startswith("/v1/inbox?")
        Clock.instant = clock + timedelta(seconds=1)
        return [item] if "after=0" in route else []

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    assert (
        incoming.receive_route(
            Bridge(store, config), config.repositories[0], "/v1/inbox", coordination=False
        )
        == 0
    )
    assert store.history(folder=None) == []


def test_expired_visibility_and_recent_reply_before_purge(
    tmp_path: Path, clock: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _config = setup_machine(tmp_path, "b")
    recipient = store.register("claude", "recipient", str(tmp_path / "repo"))
    sender = SenderContext("codex", "sender", str(tmp_path / "repo"))
    parent = store.send(sender, recipient, "old")
    child = store.send(sender, recipient, "recent reply", reply_to=parent["id"])
    with store.transaction() as db:
        db.execute(
            "UPDATE messages SET sent_at=? WHERE id=?",
            ((clock - timedelta(days=14)).isoformat(), parent["id"]),
        )
    assert [m["id"] for m in store.history(folder=None, conversation=child["id"])] == [child["id"]]
    assert store.pending_count(recipient) == 1
    assert not store.pending_allowed(parent["id"])
    with pytest.raises(BusError, match="Unknown reply parent"):
        store.send(sender, recipient, "new reply", reply_to=parent["id"])
    wakes: list[int] = []

    def wake(_recipient: str, key: int, _sender: str, **_kwargs: object) -> dict[str, str]:
        wakes.append(key)
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr("llm_bus.wake.dispatch.wake", wake)
    dispatch_pending(store, recipient)
    assert wakes == [child["id"]]


def test_partial_batch_purge_during_native_wake_keeps_survivor_coverage(
    tmp_path: Path, clock: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _config = setup_machine(tmp_path, "b")
    recipient = store.register("claude", "recipient", str(tmp_path / "repo"))
    sender = SenderContext("codex", "sender", str(tmp_path / "repo"))
    first = store.send(sender, recipient, "first")
    second = store.send(sender, recipient, "second")

    def wake(*_args: object, **_kwargs: object) -> dict[str, str]:
        with store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE messages SET sent_at=? WHERE id=?",
                ((clock - timedelta(days=14)).isoformat(), first["id"]),
            )
            assert retention.purge(db, now=clock) == {"messages": 1, "monitors": 0}
            assert db.execute("SELECT token FROM wake_dispatch").fetchone()[0] is not None
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr("llm_bus.wake.dispatch.wake", wake)
    assert dispatch_pending(store, recipient)["status"] == "requested"
    with store.transaction() as db:
        assert db.execute("SELECT message_id,status FROM wake_coverage").fetchall() == [
            (second["id"], "requested")
        ]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_recent_child_detaches_only_known_parent_and_still_checks_policy(
    tmp_path: Path, clock: datetime
) -> None:
    store, config = setup_machine(tmp_path, "b")
    recipient = store.register("claude", "recipient", str(tmp_path / "repo"))
    item = delivery()
    item["sent_at"] = clock.isoformat()
    item["reply_to"] = str(uuid4())
    remote = RemoteStore(store, config.connection)
    assert remote.admit_message(item, config.repositories[0].scope) is None
    with store.transaction() as db:
        db.execute("INSERT INTO remote_replay_guard VALUES (?)", (item["reply_to"],))
    assert remote.admit_message(item, config.repositories[0].scope) is not None
    assert store.inbox(recipient)[0]["reply_to"] is None
    assert store.inbox(recipient)[0]["body"] == item["body"]
    assert (
        remote.admit_message(
            {
                **item,
                "id": str(uuid4()),
                "coordination": direction_fixture().wire(),
            },
            config.repositories[0].scope,
        )
        is None
    )
    deny_inbox()
    assert remote.admit_message({**item, "id": str(uuid4())}, config.repositories[0].scope) is None


def test_legacy_deadline_bound_persists_without_rewriting_advertised_deadline(
    tmp_path: Path, clock: datetime
) -> None:
    store, config = setup_machine(tmp_path, "a")
    remote = RemoteStore(store, config.connection)
    sent = remote.enqueue(
        SenderContext("codex", "sender", str(tmp_path / "repo")), "b/xyz/claude:r", "xyz", "message"
    )
    key = str(sent["remote_id"])
    reports = Reports(store, config.connection)
    legacy = (clock + timedelta(days=30)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    bound = (clock + timedelta(hours=1)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    reports.deadline(key, legacy)
    reports.deadline(key, legacy, bound)
    reports.deadline(key, legacy)
    remote.set_state(key, "acknowledged")
    reopened = Reports(Store(store.path), config.connection)
    with store.transaction() as db:
        assert db.execute(
            "SELECT expires_at,retention_expires_at FROM remote_report_windows"
        ).fetchone() == (legacy, bound)
    Clock.instant = clock + timedelta(hours=1)
    assert not any(reopened.pollable_pages(now=Clock.instant))
    assert reopened.snapshot(key) is None


def test_buffered_status_and_report_retry_cannot_restore_purged_state(
    tmp_path: Path, clock: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, config = setup_machine(tmp_path, "b")
    allow_reports()
    store.register("claude", "recipient", str(tmp_path / "repo"))
    item = delivery()
    item["sent_at"] = clock.isoformat()
    remote = RemoteStore(store, config.connection)
    admitted = remote.admit_message(item, config.repositories[0].scope)
    assert admitted is not None
    assert admitted[1] is not None
    reports = Reports(store, config.connection)
    reports.enqueue(admitted[1], "claude:recipient", "received", None)
    remote.record_sync_result("report_submit", item["id"] + ":0", clock, error_code="unavailable")

    def request(_self: object, _method: str, route: str, _data: object = None) -> object:
        assert route == "/v1/receipts"
        with store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE messages SET sent_at=?", ((clock - timedelta(days=14)).isoformat(),))
            retention.purge(db, now=clock)
        return {}

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    bridge = Bridge(store, config, clock=lambda: clock + timedelta(seconds=5))
    assert submit_reports(bridge) == 0
    remote.observe_acceptance(item["id"], clock)
    remote.record_sync_result("status", item["id"], clock, error_code=None)
    with store.transaction() as db:
        for table in ("messages", "remote_report_submissions", "remote_sync_work"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)  # noqa: S608 - fixed tables
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_read_only_legacy_status_hides_expired_rows_without_migration(
    tmp_path: Path, clock: datetime
) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, sent_at TEXT)")
        db.execute("INSERT INTO messages VALUES (1,?)", ((clock - timedelta(days=14)).isoformat(),))
        db.execute("PRAGMA user_version=1")
    before = path.read_bytes()
    with pytest.raises(BusError, match="Unknown message ID"):
        Store.read_only(path).message_status(1)
    assert path.read_bytes() == before


@pytest.mark.parametrize("purged", [False, True])
def test_buffered_transport_response_and_cached_upload_cannot_restore_old_content(
    tmp_path: Path, clock: datetime, *, purged: bool
) -> None:
    store, config = setup_machine(tmp_path, "a")
    remote = RemoteStore(store, config.connection)
    sent = remote.enqueue(
        SenderContext("codex", "sender", str(tmp_path / "repo")), "b/xyz/claude:r", "xyz", "old"
    )
    envelope = remote.outgoing()[0][0]
    bridge = Bridge(store, config, clock=lambda: Clock.instant)
    Clock.instant = clock + timedelta(days=14, seconds=1)
    if purged:
        with store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            retention.purge(db, now=Clock.instant)
    outgoing.apply_status(
        bridge,
        envelope,
        {
            "id": envelope["id"],
            "status": "acknowledged",
            "report_expires_at": "2099-01-01T00:00:00.000Z",
            "retention_expires_at": "2099-01-01T00:00:00.000Z",
        },
    )
    assert not outgoing.permits_outgoing(bridge, envelope)
    assert remote.outgoing() == []
    assert remote.statuses() == []
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM remote_report_windows").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM remote_sync_work").fetchone() == (0,)
        if not purged:
            assert db.execute(
                "SELECT state FROM remote_outbox WHERE id=?", (sent["remote_id"],)
            ).fetchone() == ("queued_local",)


def test_monitor_backlog_hides_original_relay_age_at_retention(
    tmp_path: Path, clock: datetime
) -> None:
    item = delivery()
    item["sent_at"] = clock.isoformat()
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    monitor_only()
    remote = RemoteStore(store, config.connection)
    assert remote.admit_message(item, config.repositories[0].scope) == ("monitor", None, True)
    Clock.instant = clock + timedelta(days=3)
    assert remote.admit_message(item, config.repositories[0].scope) == ("monitor", None, False)
    assert len(RemoteStore.monitor_history(store, repository=None)) == 1
    Clock.instant = clock + timedelta(days=14)
    assert RemoteStore.monitor_history(store, repository=None) == []
    assert remote.admit_message(item, config.repositories[0].scope) is None
    assert store.pending_members("claude:recipient") == []


def test_report_window_upgrade_preserves_legacy_value_and_adds_null_bound(
    tmp_path: Path, clock: datetime
) -> None:
    store, config = setup_machine(tmp_path, "a")
    sent = RemoteStore(store, config.connection).enqueue(
        SenderContext("codex", "sender", str(tmp_path / "repo")), "b/xyz/claude:r", "xyz", "legacy"
    )
    legacy = (clock + timedelta(days=30)).isoformat()
    with store.transaction() as db:
        db.execute("ALTER TABLE remote_report_windows DROP COLUMN retention_expires_at")
        db.execute("INSERT INTO remote_report_windows VALUES (?,?)", (sent["remote_id"], legacy))
        db.execute("PRAGMA user_version=20")
    reopened = Store(store.path)
    with reopened.transaction() as db:
        assert db.execute(
            "SELECT expires_at,retention_expires_at FROM remote_report_windows"
        ).fetchone() == (legacy, None)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_buffered_report_snapshot_after_retention_cannot_persist(
    tmp_path: Path, clock: datetime
) -> None:
    store, config = setup_machine(tmp_path, "a")
    repo = config.repositories[0]
    config = replace(
        config,
        repositories=(
            replace(repo, peers=(replace(repo.peers[0], receive_reports=("received",)),)),
        ),
    )
    remote = RemoteStore(store, config.connection)
    remote.enqueue(
        SenderContext("codex", "sender", str(tmp_path / "repo")), "b/xyz/claude:r", "xyz", "message"
    )
    envelope = remote.outgoing()[0][0]
    Clock.instant = clock + timedelta(days=14, seconds=1)
    reports = Reports(store, config.connection)
    reports.receive(
        envelope,
        {
            "message_id": envelope["id"],
            "revision": 1,
            "status": "received",
            "reason": None,
        },
        config,
    )
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM remote_reports").fetchone() == (0,)


def test_body_free_status_and_snapshot_apply_both_report_windows(
    tmp_path: Path, clock: datetime
) -> None:
    store, config = setup_machine(tmp_path, "a")
    remote = RemoteStore(store, config.connection)
    sent = remote.enqueue(
        SenderContext("codex", "sender", str(tmp_path / "repo")), "b/xyz/claude:r", "xyz", "message"
    )
    key = str(sent["remote_id"])
    reports = Reports(store, config.connection)
    legacy = (clock + timedelta(days=30)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    expired = (
        (clock - timedelta(seconds=1)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    reports.deadline(key, legacy, expired)
    with store.transaction() as db:
        db.execute(
            "INSERT INTO remote_reports VALUES (?,?)",
            (
                key,
                json.dumps(
                    {
                        "message_id": key,
                        "revision": 1,
                        "status": "received",
                        "reason": None,
                    }
                ),
            ),
        )
    assert reports.snapshot(key) is None
    assert store.message_status(cast("int", sent["id"]))["work_report"] is None


def test_outgoing_cursor_purged_after_selection_restarts_without_skipping_survivor(
    tmp_path: Path, clock: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, config = setup_machine(tmp_path, "a")
    remote = RemoteStore(store, config.connection)
    sender = SenderContext("codex", "sender", str(tmp_path / "repo"))
    first = remote.enqueue(sender, "b/xyz/claude:r", "xyz", "survivor")
    last = remote.enqueue(sender, "b/xyz/claude:r", "xyz", "cursor")
    outgoing_page = remote.outgoing
    selected = False

    def page(*, after: int = 0) -> list[tuple[Envelope, str]]:
        nonlocal selected
        result = outgoing_page(after=after)
        if not selected:
            selected = True
            with store.transaction() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE messages SET sent_at=? WHERE id=?",
                    ((clock - timedelta(days=14)).isoformat(), last["id"]),
                )
                retention.purge(db, now=clock)
        return result

    monkeypatch.setattr(remote, "outgoing", page)
    assert [envelope["id"] for batch in remote.outgoing_pages() for envelope, _state in batch] == [
        first["remote_id"]
    ]
