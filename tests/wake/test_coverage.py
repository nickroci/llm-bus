"""dispatch."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import llm_bus.wake.adapters
import llm_bus.wake.dispatch
from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.store import Store
from tests.support import assert_wake_coverage

from .support import mailbox as _mailbox
from .support import requested as _requested


def test_wake_arrival_after_snapshot_gets_followup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, recipient, sender = _mailbox(tmp_path)
    calls: list[int] = []

    def native(_recipient: str, key: int, _sender: str) -> llm_bus.wake.adapters.WakeResult:
        calls.append(key)
        # This arrival occurs inside the external call, after the coverage snapshot.
        if len(calls) == 1:
            store.acknowledge(recipient, [key])
            sent = store.send(sender, recipient, "arrived during wake")
            assert (
                llm_bus.wake.dispatch.dispatch_pending(store, recipient, trigger_id=sent["id"])[
                    "status"
                ]
                == "coalesced"
            )
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr(llm_bus.wake.dispatch, "wake", native)
    assert llm_bus.wake.dispatch.dispatch_pending(store, recipient)["status"] == "requested"
    assert calls == [1, 2]
    assert_wake_coverage(store)


@pytest.mark.parametrize("status", ["failed", "unknown"])
def test_wake_failed_or_unknown_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    store, recipient, _sender = _mailbox(tmp_path)
    clock = datetime.now(UTC)
    calls: list[int] = []

    def native(_recipient: str, key: int, _sender: str) -> llm_bus.wake.adapters.WakeResult:
        calls.append(key)
        return {"status": status, "via": "test"}

    monkeypatch.setattr(llm_bus.wake.dispatch, "_now", lambda: clock)
    monkeypatch.setattr(llm_bus.wake.dispatch, "wake", native)
    for delay in [5, 10, 20, 40, 80, 160, 300, 300]:
        assert (
            llm_bus.wake.dispatch.dispatch_pending(Store(store.path), recipient)["status"] == status
        )
        clock += timedelta(seconds=delay - 1)
        assert llm_bus.wake.dispatch.dispatch_pending(store, recipient)["status"] == "coalesced"
        clock += timedelta(seconds=1)
    assert len(calls) == 8


@pytest.mark.parametrize("status", ["failed", "unknown"])
def test_wake_retry_excludes_previously_successful_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    store, recipient, sender = _mailbox(tmp_path)
    clock = datetime.now(UTC)
    monkeypatch.setattr(llm_bus.wake.dispatch, "_now", lambda: clock)
    monkeypatch.setattr(llm_bus.wake.dispatch, "wake", _requested)
    assert llm_bus.wake.dispatch.dispatch_pending(store, recipient)["status"] == "requested"
    later = store.send(sender, recipient, "later")
    calls: list[int] = []

    def native(_recipient: str, key: int, _sender: str) -> llm_bus.wake.adapters.WakeResult:
        calls.append(key)
        return {"status": status, "via": "test"}

    monkeypatch.setattr(llm_bus.wake.dispatch, "wake", native)
    assert llm_bus.wake.dispatch.dispatch_pending(store, recipient)["status"] == status
    clock += timedelta(seconds=5)
    llm_bus.wake.dispatch.dispatch_due(Store(store.path))
    assert calls == [later["id"], later["id"]]
    with store.transaction() as db:
        assert db.execute("SELECT status FROM wake_coverage ORDER BY message_id").fetchall() == [
            ("requested",),
            (status,),
        ]
    assert store.pending_count(recipient) == 2


def test_wake_two_attempt_limit_leaves_recoverable_due_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, recipient, sender = _mailbox(tmp_path)
    calls: list[int] = []

    def native(_recipient: str, key: int, _sender: str) -> llm_bus.wake.adapters.WakeResult:
        calls.append(key)
        store.send(sender, recipient, "another generation")
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr(llm_bus.wake.dispatch, "wake", native)
    llm_bus.wake.dispatch.dispatch_pending(store, recipient)
    assert len(calls) == 2
    with store.transaction() as db:
        assert db.execute(
            "SELECT requested_generation,dispatched_generation,token,next_attempt_at FROM wake_dispatch"
        ).fetchone() == (3, 2, None, None)
        assert db.execute("SELECT COUNT(*) FROM wake_coverage").fetchone() == (2,)
    # A bridge pass recovers the third generation without any relay replay.
    monkeypatch.setattr(llm_bus.wake.dispatch, "wake", _requested)
    llm_bus.wake.dispatch.dispatch_due(Store(store.path))
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM wake_coverage").fetchone() == (3,)


def test_wake_requested_coverage_persists_but_never_covers_later_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, recipient, sender = _mailbox(tmp_path)
    clock = datetime.now(UTC)
    calls: list[int] = []

    def native(_recipient: str, key: int, _sender: str) -> llm_bus.wake.adapters.WakeResult:
        calls.append(key)
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr(llm_bus.wake.dispatch, "_now", lambda: clock)
    monkeypatch.setattr(llm_bus.wake.dispatch, "wake", native)
    llm_bus.wake.dispatch.dispatch_pending(store, recipient)
    later = store.send(sender, recipient, "later")
    llm_bus.wake.dispatch.dispatch_pending(store, recipient)
    assert calls == [1, later["id"]]
    for seconds in (31, 300, 3600):
        clock += timedelta(seconds=seconds)
        llm_bus.wake.dispatch.dispatch_due(Store(store.path))
        assert llm_bus.wake.dispatch.dispatch_pending(store, recipient)["status"] == "coalesced"
    assert calls == [1, later["id"]]
    assert store.pending_count(recipient) == 2
    with store.transaction() as db:
        assert db.execute("SELECT next_attempt_at FROM wake_dispatch").fetchone() == (None,)


def test_wake_legacy_due_time_does_not_repeat_successful_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, recipient, _sender = _mailbox(tmp_path)
    clock = datetime.now(UTC)
    monkeypatch.setattr(llm_bus.wake.dispatch, "_now", lambda: clock)
    monkeypatch.setattr(llm_bus.wake.dispatch, "wake", _requested)
    assert llm_bus.wake.dispatch.dispatch_pending(store, recipient)["status"] == "requested"
    with store.transaction() as db:
        db.execute(
            "UPDATE wake_dispatch SET next_attempt_at=?",
            ((clock + timedelta(seconds=30)).isoformat(),),
        )

    def forbid(*_args: object) -> llm_bus.wake.adapters.WakeResult:
        pytest.fail("successful wake must not repeat")

    monkeypatch.setattr(llm_bus.wake.dispatch, "wake", forbid)
    clock += timedelta(seconds=29)
    assert llm_bus.wake.dispatch.dispatch_pending(store, recipient)["status"] == "coalesced"
    clock += timedelta(seconds=1)
    assert (
        llm_bus.wake.dispatch.dispatch_pending(Store(store.path), recipient)["status"]
        == "coalesced"
    )


def test_wake_unsupported_waits_for_capability_or_new_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("claude", "recipient", str(tmp_path))
    sender = SenderContext("codex", "sender", str(tmp_path))
    store.send(sender, recipient, "first")
    clock = datetime.now(UTC)
    calls: list[int] = []

    def native(
        _recipient: str, key: int, _sender: str, **_kwargs: object
    ) -> llm_bus.wake.adapters.WakeResult:
        calls.append(key)
        return {"status": "unsupported", "via": "test"}

    monkeypatch.setattr(llm_bus.wake.dispatch, "_now", lambda: clock)
    monkeypatch.setattr(llm_bus.wake.dispatch, "wake", native)
    llm_bus.wake.dispatch.dispatch_due(store)
    clock += timedelta(seconds=600)
    llm_bus.wake.dispatch.dispatch_due(Store(store.path))
    assert calls == [1]
    store.register("claude", "recipient", str(tmp_path), messaging_socket="/test/new.sock")
    llm_bus.wake.dispatch.dispatch_due(store)
    assert calls == [1, 1]
    sent = store.send(sender, recipient, "later send")
    llm_bus.wake.dispatch.dispatch_pending(store, recipient, trigger_id=sent["id"])
    assert calls == [1, 1, 1]


def test_wake_burst_has_explicit_coverage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("codex", "recipient", str(tmp_path))
    sender = SenderContext("claude", "sender", str(tmp_path))
    messages = [store.send(sender, recipient, f"message {index}") for index in range(150)]
    calls: list[int] = []

    def native(_recipient: str, message_id: int, _sender: str) -> llm_bus.wake.adapters.WakeResult:
        calls.append(message_id)
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr(llm_bus.wake.dispatch, "wake", native)
    assert llm_bus.wake.dispatch.dispatch_pending(store, recipient)["status"] == "requested"
    assert llm_bus.wake.dispatch.dispatch_pending(store, recipient)["status"] == "coalesced"
    assert calls == [messages[0]["id"]]
    with store.transaction() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM wake_coverage WHERE status='requested'"
        ).fetchone() == (150,)
    assert store.pending_count(recipient) == 150
