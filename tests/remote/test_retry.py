"""retry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.store import Store
from llm_bus.remote import batches as remote_batches
from llm_bus.remote.bridge import Bridge
from llm_bus.remote.store import RemoteStore
from llm_bus.reports.store import Reports

from .support import FakeClock, setup_machine

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_retry_schedule_survives_reopen(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "a")
    remote = RemoteStore(store, config.connection)
    now = datetime(2026, 10, 20, tzinfo=UTC)
    first = remote.enqueue(
        SenderContext("codex", "s", str(tmp_path / "repo")), "b/xyz/claude:r", "xyz", "first"
    )
    second = remote.enqueue(
        SenderContext("codex", "s", str(tmp_path / "repo")), "b/xyz/claude:r", "xyz", "second"
    )
    one, two = str(first["remote_id"]), str(second["remote_id"])
    remote.record_sync_result("upload", one, now, error_code="unavailable")
    reopened = RemoteStore(Store(store.path), config.connection)
    assert not reopened.work_due("upload", one, now + timedelta(seconds=4))
    assert reopened.work_due("upload", one, now + timedelta(seconds=5))
    for seconds in (5, 10, 20, 40, 80, 160, 300, 300):
        reopened.record_sync_result("status", two, now, error_code="unavailable")
        assert not reopened.work_due("status", two, now + timedelta(seconds=seconds - 1))
        assert reopened.work_due("status", two, now + timedelta(seconds=seconds))
        now += timedelta(seconds=seconds)
    reopened.record_sync_result("status", two, now, error_code=None)
    assert reopened.sync_snapshot("status", two)["attempts"] == 0
    assert not reopened.work_due("status", two, now + timedelta(seconds=29))
    assert reopened.work_due("status", two, now + timedelta(seconds=30))
    reopened.record_sync_result("upload", one, now + timedelta(seconds=5), error_code=None)
    assert reopened.work_due("upload", one, now + timedelta(seconds=5))


def test_retry_schema_migration_preserves_version_one_history(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "a")
    remote = RemoteStore(store, config.connection)
    remote.enqueue(
        SenderContext("codex", "s", str(tmp_path / "repo")), "b/xyz/claude:r", "xyz", "retained"
    )
    with store.transaction() as db:
        db.execute("DROP TABLE remote_sync_work")
        db.execute("DROP TABLE remote_peer_sync")
        db.execute("DROP TABLE wake_coverage")
        db.execute("DROP TABLE wake_dispatch")
        db.execute("DROP TABLE hook_guidance")
        db.execute("PRAGMA user_version=1")
    reopened = RemoteStore(Store(store.path), config.connection)
    assert reopened.statuses()[0]["status"] == "queued_local"
    assert reopened.sync_snapshot("status", "new")["last_success_at"] is None
    assert store.history(folder=None)[0]["body"] == "retained"


def test_horizon_retains_last_known_delivery(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "a")
    remote = RemoteStore(store, config.connection)
    now = datetime(2026, 10, 20, tzinfo=UTC)
    sent = remote.enqueue(
        SenderContext("codex", "s", str(tmp_path / "repo")), "b/xyz/claude:r", "xyz", "hello"
    )
    remote_id = str(sent["remote_id"])
    remote.set_state(remote_id, "delivered_local")
    remote.observe_acceptance(remote_id, now)
    assert not remote.work_due("status", remote_id, now + timedelta(days=30))
    assert remote.work_due("status", remote_id, now + timedelta(days=31), force=True)
    assert remote.statuses()[0]["status"] == "delivered_local"
    assert remote.sync_snapshot("status", remote_id)["polling_suspended_at"] is not None


def test_peer_freshness_after_failed_refresh(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "a")
    remote = RemoteStore(store, config.connection)
    now = datetime(2026, 10, 20, tzinfo=UTC)
    remote.record_peer_result("xyz", now, error_code=None)
    remote.record_peer_result("xyz", now + timedelta(seconds=60), error_code="unavailable")
    snapshot = remote.peer_snapshot("xyz", now + timedelta(seconds=60))
    assert snapshot["last_success_at"] == now.isoformat()
    assert snapshot["stale"] is True


def test_report_polling_deadline_remains_independent_of_transport(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "a")
    reports = Reports(store, config.connection)
    sent = reports.remote.enqueue(
        SenderContext("codex", "s", str(tmp_path / "repo")), "b/xyz/claude:r", "xyz", "hello"
    )
    key = str(sent["remote_id"])
    reports.remote.set_state(key, "expired")
    reports.deadline(key, "2099-01-01T00:00:00.000Z")
    assert [e["id"] for page in reports.pollable_pages(now=FakeClock()()) for e in page] == [key]
    assert not any(reports.pollable_pages(now=datetime(2100, 1, 1, tzinfo=UTC)))


def test_horizon_force_refresh_never_uploads_accepted_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code,clean-code-redundant-comment
    store, config = setup_machine(tmp_path, "a")
    clock = FakeClock()
    bridge = Bridge(store, config, clock=clock)
    sent = bridge.remote.enqueue(
        SenderContext("codex", "s", str(tmp_path / "repo")), "b/xyz/claude:r", "xyz", "one"
    )
    key = str(sent["remote_id"])
    bridge.remote.set_state(key, "delivered_local")
    bridge.remote.observe_acceptance(key, clock())
    clock.advance(30 * 86400)
    calls: list[str] = []

    def request(_self: object, _method: str, path: str, _data: object = None) -> object:
        calls.append(path)
        assert path == "/v1/status/batch"
        return {
            "results": [{"id": key, "value": {"id": key, "status": "acknowledged"}, "error": None}]
        }

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    assert remote_batches.sync_outgoing(bridge) == (0, 0)
    assert calls == []
    forced = Bridge(Store(store.path), config, clock=clock, refresh_status=True)
    assert remote_batches.sync_outgoing(forced) == (1, 0)
    assert calls == ["/v1/status/batch"]
    assert forced.remote.statuses()[0]["status"] == "acknowledged"
