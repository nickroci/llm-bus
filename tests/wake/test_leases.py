"""dispatch."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

import llm_bus.wake.adapters
import llm_bus.wake.dispatch
from llm_bus.mailbox.store import Store

from .support import mailbox as _mailbox
from .support import requested as _requested


def test_wake_concurrent_sender_rechecks_active_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, recipient, sender = _mailbox(tmp_path)
    claimed = Event()
    release = Event()
    calls: list[int] = []

    def gather(_seconds: float) -> None:
        claimed.set()
        assert release.wait(5)

    def native(_recipient: str, key: int, _sender: str) -> llm_bus.wake.adapters.WakeResult:
        calls.append(key)
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr(llm_bus.wake.dispatch, "time", SimpleNamespace(sleep=gather))
    monkeypatch.setattr(llm_bus.wake.dispatch, "wake", native)
    with ThreadPoolExecutor(max_workers=1) as pool:
        leader = pool.submit(llm_bus.wake.dispatch.dispatch_pending, store, recipient)
        try:
            assert claimed.wait(5)
            sent = store.send(sender, recipient, "concurrent")
            assert (
                llm_bus.wake.dispatch.dispatch_pending(store, recipient, trigger_id=sent["id"])[
                    "status"
                ]
                == "coalesced"
            )
        finally:
            release.set()
        assert leader.result(timeout=5)["status"] == "requested"
    assert calls == [1]
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM wake_coverage").fetchone() == (2,)


def test_wake_lease_crash_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code
    store, recipient, _sender = _mailbox(tmp_path)
    clock = datetime.now(UTC)
    monkeypatch.setattr(llm_bus.wake.dispatch, "_now", lambda: clock)
    claim = llm_bus.wake.dispatch._claim(store, recipient, trigger_id=None)  # pyright: ignore[reportPrivateUsage]
    assert claim is not None
    monkeypatch.setattr(llm_bus.wake.dispatch, "wake", _requested)
    assert (
        llm_bus.wake.dispatch.dispatch_pending(Store(store.path), recipient)["status"]
        == "coalesced"
    )
    clock += timedelta(seconds=31)
    assert (
        llm_bus.wake.dispatch.dispatch_pending(Store(store.path), recipient)["status"]
        == "requested"
    )
    # An expired owner's finalization cannot replace the successor's coverage.
    assert (
        llm_bus.wake.dispatch._finish(  # pyright: ignore[reportPrivateUsage]
            store,
            recipient,
            claim,
            llm_bus.wake.dispatch.Batch(1, [(1, "claude:sender")]),
            {"status": "failed", "via": "test"},
        )
        is None
    )  # pyright: ignore[reportPrivateUsage]
    with store.transaction() as db:
        assert db.execute("SELECT status FROM wake_coverage").fetchone() == ("requested",)


def test_wake_expired_leader_cannot_enter_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code
    store, recipient, _sender = _mailbox(tmp_path)
    clock = datetime.now(UTC)
    snapshot = llm_bus.wake.dispatch._snapshot  # pyright: ignore[reportPrivateUsage]

    def expire(
        store: Store, recipient: str, claim: llm_bus.wake.dispatch.Claim
    ) -> llm_bus.wake.dispatch.Batch:
        nonlocal clock
        batch = snapshot(store, recipient, claim)
        clock += timedelta(seconds=31)
        return batch

    def forbid(*_args: object) -> llm_bus.wake.adapters.WakeResult:
        pytest.fail("expired leader must not enter the adapter")

    monkeypatch.setattr(llm_bus.wake.dispatch, "_now", lambda: clock)
    monkeypatch.setattr(llm_bus.wake.dispatch, "_snapshot", expire)
    monkeypatch.setattr(llm_bus.wake.dispatch, "wake", forbid)
    assert llm_bus.wake.dispatch.dispatch_pending(store, recipient)["status"] == "coalesced"
    monkeypatch.setattr(llm_bus.wake.dispatch, "_snapshot", snapshot)
    monkeypatch.setattr(llm_bus.wake.dispatch, "wake", _requested)
    assert llm_bus.wake.dispatch.dispatch_pending(store, recipient)["status"] == "requested"


def test_wake_release_race_sender_can_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code
    store, recipient, sender = _mailbox(tmp_path)
    finish = llm_bus.wake.dispatch._finish  # pyright: ignore[reportPrivateUsage]
    calls: list[int] = []

    def release(
        store: Store,
        recipient: str,
        claim: llm_bus.wake.dispatch.Claim,
        batch: llm_bus.wake.dispatch.Batch,
        result: llm_bus.wake.adapters.WakeResult,
    ) -> bool | None:
        arrived = finish(store, recipient, claim, batch, result)
        if len(calls) == 1:
            sent = store.send(sender, recipient, "after release")
            llm_bus.wake.dispatch.dispatch_pending(store, recipient, trigger_id=sent["id"])
        return arrived

    def native(_recipient: str, key: int, _sender: str) -> llm_bus.wake.adapters.WakeResult:
        calls.append(key)
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr(llm_bus.wake.dispatch, "_finish", release)
    monkeypatch.setattr(llm_bus.wake.dispatch, "wake", native)
    llm_bus.wake.dispatch.dispatch_pending(store, recipient)
    assert len(calls) == 2
    assert store.pending_count(recipient) == 2
