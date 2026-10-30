"""Host wake adapters never undo already-stored messages."""

import json
import os
import shutil
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from typing import cast

import pytest

import llm_bus_dispatch
import llm_bus_wake
from llm_bus_store import SenderContext, Store


def _requested(*_args: object, **_kwargs: object) -> llm_bus_wake.WakeResult:
    return {"status": "requested", "via": "test"}


def _mailbox(tmp_path: Path) -> tuple[Store, str, SenderContext]:
    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("codex", "recipient", str(tmp_path))
    sender = SenderContext("claude", "sender", str(tmp_path))
    store.send(sender, recipient, "first")
    return store, recipient, sender


def test_wake_arrival_after_snapshot_gets_followup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, recipient, sender = _mailbox(tmp_path)
    calls: list[int] = []

    def native(_recipient: str, key: int, _sender: str) -> llm_bus_wake.WakeResult:
        calls.append(key)
        # This arrival occurs inside the external call, after the coverage snapshot.
        if len(calls) == 1:
            store.acknowledge(recipient, [key])
            sent = store.send(sender, recipient, "arrived during wake")
            assert (
                llm_bus_dispatch.dispatch_pending(store, recipient, trigger_id=sent["id"])["status"]
                == "coalesced"
            )
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr(llm_bus_dispatch, "wake", native)
    assert llm_bus_dispatch.dispatch_pending(store, recipient)["status"] == "requested"
    assert calls == [1, 2]
    with store.transaction() as db:
        assert db.execute(
            "SELECT requested_generation,dispatched_generation,token FROM wake_dispatch"
        ).fetchone() == (2, 2, None)
        assert db.execute("SELECT COUNT(*) FROM wake_coverage").fetchone() == (2,)


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

    def native(_recipient: str, key: int, _sender: str) -> llm_bus_wake.WakeResult:
        calls.append(key)
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr(llm_bus_dispatch, "time", SimpleNamespace(sleep=gather))
    monkeypatch.setattr(llm_bus_dispatch, "wake", native)
    with ThreadPoolExecutor(max_workers=1) as pool:
        leader = pool.submit(llm_bus_dispatch.dispatch_pending, store, recipient)
        try:
            assert claimed.wait(5)
            sent = store.send(sender, recipient, "concurrent")
            assert (
                llm_bus_dispatch.dispatch_pending(store, recipient, trigger_id=sent["id"])["status"]
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
    clock = datetime(2030, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(llm_bus_dispatch, "_now", lambda: clock)
    claim = llm_bus_dispatch._claim(store, recipient, trigger_id=None)  # pyright: ignore[reportPrivateUsage]
    assert claim is not None
    monkeypatch.setattr(llm_bus_dispatch, "wake", _requested)
    assert llm_bus_dispatch.dispatch_pending(Store(store.path), recipient)["status"] == "coalesced"
    clock += timedelta(seconds=31)
    assert llm_bus_dispatch.dispatch_pending(Store(store.path), recipient)["status"] == "requested"
    # An expired owner's finalization cannot replace the successor's coverage.
    assert (
        llm_bus_dispatch._finish(  # pyright: ignore[reportPrivateUsage]
            store,
            recipient,
            claim,
            llm_bus_dispatch.Batch(1, [(1, "claude:sender")]),
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
    clock = datetime(2030, 1, 1, tzinfo=UTC)
    snapshot = llm_bus_dispatch._snapshot  # pyright: ignore[reportPrivateUsage]

    def expire(
        store: Store, recipient: str, claim: llm_bus_dispatch.Claim
    ) -> llm_bus_dispatch.Batch:
        nonlocal clock
        batch = snapshot(store, recipient, claim)
        clock += timedelta(seconds=31)
        return batch

    def forbid(*_args: object) -> llm_bus_wake.WakeResult:
        pytest.fail("expired leader must not enter the adapter")

    monkeypatch.setattr(llm_bus_dispatch, "_now", lambda: clock)
    monkeypatch.setattr(llm_bus_dispatch, "_snapshot", expire)
    monkeypatch.setattr(llm_bus_dispatch, "wake", forbid)
    assert llm_bus_dispatch.dispatch_pending(store, recipient)["status"] == "coalesced"
    monkeypatch.setattr(llm_bus_dispatch, "_snapshot", snapshot)
    monkeypatch.setattr(llm_bus_dispatch, "wake", _requested)
    assert llm_bus_dispatch.dispatch_pending(store, recipient)["status"] == "requested"


@pytest.mark.parametrize("status", ["failed", "unknown"])
def test_wake_failed_or_unknown_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    store, recipient, _sender = _mailbox(tmp_path)
    clock = datetime(2030, 1, 1, tzinfo=UTC)
    calls: list[int] = []

    def native(_recipient: str, key: int, _sender: str) -> llm_bus_wake.WakeResult:
        calls.append(key)
        return {"status": status, "via": "test"}

    monkeypatch.setattr(llm_bus_dispatch, "_now", lambda: clock)
    monkeypatch.setattr(llm_bus_dispatch, "wake", native)
    for delay in [5, 10, 20, 40, 80, 160, 300, 300]:
        assert llm_bus_dispatch.dispatch_pending(Store(store.path), recipient)["status"] == status
        clock += timedelta(seconds=delay - 1)
        assert llm_bus_dispatch.dispatch_pending(store, recipient)["status"] == "coalesced"
        clock += timedelta(seconds=1)
    assert len(calls) == 8


@pytest.mark.parametrize("status", ["failed", "unknown"])
def test_wake_retry_excludes_previously_successful_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    store, recipient, sender = _mailbox(tmp_path)
    clock = datetime(2030, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(llm_bus_dispatch, "_now", lambda: clock)
    monkeypatch.setattr(llm_bus_dispatch, "wake", _requested)
    assert llm_bus_dispatch.dispatch_pending(store, recipient)["status"] == "requested"
    later = store.send(sender, recipient, "later")
    calls: list[int] = []

    def native(_recipient: str, key: int, _sender: str) -> llm_bus_wake.WakeResult:
        calls.append(key)
        return {"status": status, "via": "test"}

    monkeypatch.setattr(llm_bus_dispatch, "wake", native)
    assert llm_bus_dispatch.dispatch_pending(store, recipient)["status"] == status
    clock += timedelta(seconds=5)
    llm_bus_dispatch.dispatch_due(Store(store.path))
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

    def native(_recipient: str, key: int, _sender: str) -> llm_bus_wake.WakeResult:
        calls.append(key)
        store.send(sender, recipient, "another generation")
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr(llm_bus_dispatch, "wake", native)
    llm_bus_dispatch.dispatch_pending(store, recipient)
    assert len(calls) == 2
    with store.transaction() as db:
        assert db.execute(
            "SELECT requested_generation,dispatched_generation,token,next_attempt_at FROM wake_dispatch"
        ).fetchone() == (3, 2, None, None)
        assert db.execute("SELECT COUNT(*) FROM wake_coverage").fetchone() == (2,)
    # A bridge pass recovers the third generation without any relay replay.
    monkeypatch.setattr(llm_bus_dispatch, "wake", _requested)
    llm_bus_dispatch.dispatch_due(Store(store.path))
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM wake_coverage").fetchone() == (3,)


def test_wake_requested_coverage_persists_but_never_covers_later_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, recipient, sender = _mailbox(tmp_path)
    clock = datetime(2030, 1, 1, tzinfo=UTC)
    calls: list[int] = []

    def native(_recipient: str, key: int, _sender: str) -> llm_bus_wake.WakeResult:
        calls.append(key)
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr(llm_bus_dispatch, "_now", lambda: clock)
    monkeypatch.setattr(llm_bus_dispatch, "wake", native)
    llm_bus_dispatch.dispatch_pending(store, recipient)
    later = store.send(sender, recipient, "later")
    llm_bus_dispatch.dispatch_pending(store, recipient)
    assert calls == [1, later["id"]]
    for seconds in (31, 300, 3600):
        clock += timedelta(seconds=seconds)
        llm_bus_dispatch.dispatch_due(Store(store.path))
        assert llm_bus_dispatch.dispatch_pending(store, recipient)["status"] == "coalesced"
    assert calls == [1, later["id"]]
    assert store.pending_count(recipient) == 2
    with store.transaction() as db:
        assert db.execute("SELECT next_attempt_at FROM wake_dispatch").fetchone() == (None,)


def test_wake_legacy_due_time_does_not_repeat_successful_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, recipient, _sender = _mailbox(tmp_path)
    clock = datetime(2030, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(llm_bus_dispatch, "_now", lambda: clock)
    monkeypatch.setattr(llm_bus_dispatch, "wake", _requested)
    assert llm_bus_dispatch.dispatch_pending(store, recipient)["status"] == "requested"
    with store.transaction() as db:
        db.execute(
            "UPDATE wake_dispatch SET next_attempt_at=?",
            ((clock + timedelta(seconds=30)).isoformat(),),
        )

    def forbid(*_args: object) -> llm_bus_wake.WakeResult:
        pytest.fail("successful wake must not repeat")

    monkeypatch.setattr(llm_bus_dispatch, "wake", forbid)
    clock += timedelta(seconds=29)
    assert llm_bus_dispatch.dispatch_pending(store, recipient)["status"] == "coalesced"
    clock += timedelta(seconds=1)
    assert llm_bus_dispatch.dispatch_pending(Store(store.path), recipient)["status"] == "coalesced"


def test_wake_release_race_sender_can_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code
    store, recipient, sender = _mailbox(tmp_path)
    finish = llm_bus_dispatch._finish  # pyright: ignore[reportPrivateUsage]
    calls: list[int] = []

    def release(
        store: Store,
        recipient: str,
        claim: llm_bus_dispatch.Claim,
        batch: llm_bus_dispatch.Batch,
        result: llm_bus_wake.WakeResult,
    ) -> bool | None:
        arrived = finish(store, recipient, claim, batch, result)
        if len(calls) == 1:
            sent = store.send(sender, recipient, "after release")
            llm_bus_dispatch.dispatch_pending(store, recipient, trigger_id=sent["id"])
        return arrived

    def native(_recipient: str, key: int, _sender: str) -> llm_bus_wake.WakeResult:
        calls.append(key)
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr(llm_bus_dispatch, "_finish", release)
    monkeypatch.setattr(llm_bus_dispatch, "wake", native)
    llm_bus_dispatch.dispatch_pending(store, recipient)
    assert len(calls) == 2
    assert store.pending_count(recipient) == 2


def test_wake_unsupported_waits_for_capability_or_new_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("claude", "recipient", str(tmp_path))
    sender = SenderContext("codex", "sender", str(tmp_path))
    store.send(sender, recipient, "first")
    clock = datetime(2030, 1, 1, tzinfo=UTC)
    calls: list[int] = []

    def native(
        _recipient: str, key: int, _sender: str, **_kwargs: object
    ) -> llm_bus_wake.WakeResult:
        calls.append(key)
        return {"status": "unsupported", "via": "test"}

    monkeypatch.setattr(llm_bus_dispatch, "_now", lambda: clock)
    monkeypatch.setattr(llm_bus_dispatch, "wake", native)
    llm_bus_dispatch.dispatch_due(store)
    clock += timedelta(seconds=600)
    llm_bus_dispatch.dispatch_due(Store(store.path))
    assert calls == [1]
    store.register("claude", "recipient", str(tmp_path), messaging_socket="/test/new.sock")
    llm_bus_dispatch.dispatch_due(store)
    assert calls == [1, 1]
    sent = store.send(sender, recipient, "later send")
    llm_bus_dispatch.dispatch_pending(store, recipient, trigger_id=sent["id"])
    assert calls == [1, 1, 1]


def test_wake_burst_has_explicit_coverage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("codex", "recipient", str(tmp_path))
    sender = SenderContext("claude", "sender", str(tmp_path))
    messages = [store.send(sender, recipient, f"message {index}") for index in range(150)]
    calls: list[int] = []

    def native(_recipient: str, message_id: int, _sender: str) -> llm_bus_wake.WakeResult:
        calls.append(message_id)
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr(llm_bus_dispatch, "wake", native)
    assert llm_bus_dispatch.dispatch_pending(store, recipient)["status"] == "requested"
    assert llm_bus_dispatch.dispatch_pending(store, recipient)["status"] == "coalesced"
    assert calls == [messages[0]["id"]]
    with store.transaction() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM wake_coverage WHERE status='requested'"
        ).fetchone() == (150,)
    assert store.pending_count(recipient) == 150


def test_claude_wake_never_sends_terminal_input(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def herdr_path(_name: str) -> str:
        return "/bin/herdr"

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        agents = {
            "result": {
                "agents": [
                    {
                        "agent": "claude",
                        "pane_id": "w1:p1",
                        "agent_session": {"kind": "id", "value": "target"},
                    }
                ]
            }
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(agents), "")

    monkeypatch.setattr(shutil, "which", herdr_path)
    monkeypatch.setattr(llm_bus_wake, "_run", run)
    result = llm_bus_wake.wake("claude:target", 7, "codex:source")
    assert commands == []
    assert result["status"] == "unsupported"
    assert result["via"] == "none"
    assert "next turn hook" in result.get("reason", "")


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(socket, "AF_UNIX"), reason="Unix sockets required"
)
def test_claude_native_wake_uses_socket_and_targets_exact_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid_terminal(_command: list[str]) -> subprocess.CompletedProcess[str]:
        pytest.fail("Claude wake must never write terminal input")

    monkeypatch.setattr(llm_bus_wake, "_run", forbid_terminal)
    with (
        TemporaryDirectory(prefix="bus-", dir="/tmp") as folder,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server,
    ):
        endpoint = Path(folder) / "inbox.sock"
        server.bind(str(endpoint))
        endpoint.chmod(0o600)
        server.listen(1)
        server.settimeout(2)
        assert llm_bus_wake.wake(
            "claude:target", 7, "codex:source", claude_socket=str(endpoint)
        ) == {
            "status": "requested",
            "via": "Claude inbox socket",
        }
        connection = server.accept()[0]
        with connection:
            connection.settimeout(2)
            with connection.makefile("rb") as reader:
                frame = cast("object", json.loads(reader.readline()))
        assert frame == {
            "type": "user",
            "session_id": "target",
            "message": {
                "role": "user",
                "content": "llm-bus message #7 from codex:source. Read and handle it with `llm-bus inbox`.",
            },
            "priority": "next",
        }
    assert (
        llm_bus_wake.wake("claude:target", 8, "codex:source", claude_socket=str(endpoint))["status"]
        == "failed"
    )


@pytest.mark.skipif(os.name == "nt", reason="Unix sockets required")
def test_claude_wake_refuses_non_socket_and_symlink(tmp_path: Path) -> None:
    endpoint = tmp_path / "ordinary-file"
    endpoint.write_text("user draft")
    link = tmp_path / "link"
    link.symlink_to(endpoint)
    for path in (endpoint, link):
        assert (
            llm_bus_wake.wake("claude:target", 7, "codex:source", claude_socket=str(path))["status"]
            == "failed"
        )
    assert endpoint.read_text() == "user draft"


@pytest.mark.parametrize("sender", ["claude:source", "codex:source"])
def test_codex_wake_queues_message_id_and_sender(
    monkeypatch: pytest.MonkeyPatch, sender: str
) -> None:
    commands: list[list[str]] = []

    def codex_path(_name: str) -> str:
        return "/bin/codex"

    monkeypatch.setattr(shutil, "which", codex_path)

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "Queued", "")

    monkeypatch.setattr(llm_bus_wake, "_run", run)
    assert llm_bus_wake.wake("codex:session-123", 42, sender) == {
        "status": "requested",
        "via": "codex queue",
    }
    assert commands == [
        [
            "/bin/codex",
            "queue",
            "--thread",
            "session-123",
            "--message",
            f"llm-bus message #42 from {sender}. Read and handle it with `llm-bus inbox`.",
        ]
    ]


def test_missing_host_and_timeout_report_wake_state(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_path(_name: str) -> None:
        return None

    monkeypatch.setattr(shutil, "which", missing_path)
    assert llm_bus_wake.wake("codex:one", 1, "codex:source")["status"] == "failed"
    assert llm_bus_wake.wake("claude:two", 2, "codex:source")["status"] == "unsupported"

    def codex_path(_name: str) -> str:
        return "/bin/codex"

    monkeypatch.setattr(shutil, "which", codex_path)

    def timeout(command: list[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 10)

    monkeypatch.setattr(llm_bus_wake, "_run", timeout)
    assert llm_bus_wake.wake("codex:one", 1, "codex:source")["status"] == "unknown"
