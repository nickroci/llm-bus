"""reports."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from llm_bus.errors import BusError
from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.store import Store
from llm_bus.policy.config import load_config
from llm_bus.remote.bridge import Bridge
from llm_bus.remote.http import RelayError
from llm_bus.remote.outgoing import enqueue_remote
from llm_bus.remote.store import RemoteStore
from llm_bus.reports.store import Reports

from .support import FakeClock, setup_machine


def test_report_polling_survives_ack_and_uses_relay_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, config = setup_machine(tmp_path, "a")
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8") + 'receive_reports = ["received", "completed"]\n',
        encoding="utf-8",
    )
    sender = SenderContext("codex", "sender", str(tmp_path / "repo"))
    sent = enqueue_remote(store, config, sender, "b/xyz/claude:recipient", "offline")
    polls: list[str] = []
    terminal = False
    clock = FakeClock()

    def request(_self: object, _method: str, route: str, _data: object = None) -> object:
        if route.endswith("/batch"):
            raise RelayError("rejected", http_status=404)
        if route == "/v1/messages":
            return {"status": "acknowledged", "report_expires_at": "2099-01-01T00:00:00.000Z"}
        if route.startswith("/v1/reports?"):
            polls.append(route)
            return {
                "message_id": sent["remote_id"],
                "revision": 2 if terminal else 1,
                "status": "completed" if terminal else "received",
                "reason": None,
            }
        return []

    def forbid_wake(*_args: object, **_kwargs: object) -> None:
        pytest.fail("reports must not wake agents")

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    monkeypatch.setattr("llm_bus.wake.dispatch.wake", forbid_wake)
    assert Bridge(store, config, clock=clock).sync()["errors"] == 0
    assert RemoteStore(store, config.connection).outgoing() == []
    clock.advance(30)
    assert Bridge(Store(store.path), config, clock=clock).sync()["errors"] == 0
    assert len(polls) == 2
    terminal = True
    clock.advance(30)
    assert Bridge(store, config, clock=clock).sync()["errors"] == 0
    clock.advance(30)
    assert Bridge(store, config, clock=clock).sync()["errors"] == 0
    assert len(polls) == 3
    assert Reports(store, config.connection).statuses()[0]["work_report"] == {
        "message_id": sent["remote_id"],
        "revision": 2,
        "status": "completed",
        "reason": None,
    }
    assert len(store.history(folder=None)) == 1


@pytest.mark.parametrize("deadline", [None, "2020-01-01T00:00:00.000Z", "invalid", True])
def test_report_polling_requires_valid_live_relay_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deadline: object
) -> None:
    store, config = setup_machine(tmp_path, "a")
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8") + 'receive_reports = ["received"]\n', encoding="utf-8"
    )
    enqueue_remote(
        store,
        config,
        SenderContext("codex", "sender", str(tmp_path / "repo")),
        "b/xyz/claude:recipient",
        "pending",
    )

    def request(_self: object, _method: str, route: str, _data: object = None) -> object:
        assert not route.startswith("/v1/reports?")
        if route == "/v1/messages":
            return {
                "status": "acknowledged",
                **({"report_expires_at": deadline} if deadline is not None else {}),
            }
        return []

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    assert Bridge(store, config).sync()["errors"] == (1 if deadline in ("invalid", True) else 0)
    assert not any(Reports(store, config.connection).pollable_pages())


def test_report_snapshot_rejects_replay_and_disallowed_status(tmp_path: Path) -> None:
    store, _config = setup_machine(tmp_path, "a")
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8") + 'receive_reports = ["received", "completed"]\n',
        encoding="utf-8",
    )
    current = load_config()
    assert current is not None
    enqueue_remote(
        store,
        current,
        SenderContext("codex", "sender", str(tmp_path / "repo")),
        "b/xyz/claude:recipient",
        "pending",
    )
    envelope = RemoteStore(store, current.connection).outgoing()[0][0]
    reports = Reports(store, current.connection)
    initial = {"message_id": envelope["id"], "revision": 1, "status": "received", "reason": None}
    reports.receive(envelope, initial, current)
    for invalid in [
        dict(initial, revision=True),
        dict(initial, extra="text"),
        dict(initial, revision=0),
        dict(initial, revision=2, status="failed", reason="unavailable"),
    ]:
        with pytest.raises(BusError):
            reports.receive(envelope, invalid, current)
        assert reports.snapshot(envelope["id"]) == initial
    with pytest.raises(BusError, match="binding"):
        reports.receive({**envelope, "recipient": "c/xyz/claude:other"}, initial, current)
    reports.deadline(envelope["id"], "2099-01-01T00:00:00.000Z")
    with pytest.raises(BusError, match="changed"):
        reports.deadline(envelope["id"], "2099-02-01T00:00:00.000Z")


def test_report_receive_revocation_during_poll_rejects_initial_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, config = setup_machine(tmp_path, "a")
    path = Path(os.environ["LLM_BUS_CONFIG"])
    original = path.read_text(encoding="utf-8") + 'receive_reports = ["received"]\n'
    path.write_text(original, encoding="utf-8")
    enqueue_remote(
        store,
        config,
        SenderContext("codex", "sender", str(tmp_path / "repo")),
        "b/xyz/claude:recipient",
        "task",
    )
    envelope = RemoteStore(store, config.connection).outgoing()[0][0]
    reports = Reports(store, config.connection)
    reports.deadline(envelope["id"], "2099-01-01T00:00:00.000Z")
    reports.remote.set_state(envelope["id"], "acknowledged")

    def request(_self: object, _method: str, route: str, _data: object = None) -> object:
        if route.startswith("/v1/reports?"):
            path.write_text(
                original.replace('receive_reports = ["received"]', "receive_reports = []"),
                encoding="utf-8",
            )
            return {"message_id": envelope["id"], "revision": 0, "status": None, "reason": None}
        return []

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    assert Bridge(store, config).sync()["errors"] == 1
    assert reports.snapshot(envelope["id"]) is None
