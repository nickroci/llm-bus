"""monitor."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

from llm_bus import main
from llm_bus.errors import BusError
from llm_bus.mailbox.store import Store
from llm_bus.remote.bridge import Bridge
from llm_bus.remote.store import RemoteStore

from .support import FakeClock, admit_delivery, delivery, monitor_only, setup_machine

if TYPE_CHECKING:
    from pathlib import Path

    from llm_bus.remote.models import Delivery


def test_monitor_is_durable_isolated_and_never_promoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    path = tmp_path / "config.toml"
    original = path.read_text()
    path.write_text(original.replace('receive_messages = "inbox"', 'receive_messages = "monitor"'))
    item: Delivery = {
        **delivery(),
        "body": "hostile\x1b]8;;https://bad\x07\r\u202e metadata\x1b[2J\u009b\u202e",
    }
    receipts: list[str] = []

    def request(_self: object, _method: str, route: str, _data: object = None) -> object:
        if route.startswith("/v1/inbox?"):
            return [item] if "after=0" in route else []
        if route == "/v1/receipts":
            # Reopening the database proves the receipt cannot precede persistence.
            assert len(RemoteStore.monitor_history(Store(store.path), repository=None)) == 1
            receipts.append(item["id"])
            if len(receipts) == 1:
                raise BusError("receipt response lost")
            return {}
        if route.startswith("/v1/sessions"):
            return []
        if route == "/v2/coordination/capabilities":
            return {"version": 0}
        if route == "/v2/coordination/availability/capabilities":
            return {"availability_version": 0}
        raise AssertionError(route)

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)

    def unexpected_wake(*_args: object, **_kwargs: object) -> None:
        pytest.fail("monitor wake")

    monkeypatch.setattr("llm_bus.wake.dispatch.wake", unexpected_wake)
    clock = FakeClock()
    bridge = Bridge(store, config, clock=clock)
    assert bridge.sync() == {"uploaded": 0, "imported": 0, "acknowledged": 0, "errors": 1}
    path.write_text(original)
    clock.advance(5)
    bridge = Bridge(Store(store.path), config, clock=clock)
    assert bridge.sync() == {"uploaded": 0, "imported": 0, "acknowledged": 0, "errors": 0}
    assert len(receipts) == 2
    assert store.inbox("claude:recipient") == []
    assert store.pending_count("claude:recipient") == 0
    assert store.history(folder=None) == []
    assert bridge.remote.acknowledgements() == []
    with pytest.raises(BusError, match="different content"):
        bridge.remote.admit_message({**item, "body": "changed"}, config.repositories[0].scope)
    path.unlink()
    monkeypatch.setenv("LLM_BUS_DB", str(store.path))
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert main(["monitor"]) == 0
    output = capsys.readouterr().out
    assert "hostile\\u001b" in output
    assert "metadata\\u001b[2J\\u009b\\u202e" in output
    assert not any(character in output for character in ("\x1b", "\x07", "\r", "\u202e"))
    assert [agent["address"] for agent in store.all_agents()] == ["claude:recipient"]


def test_monitor_rejects_invalid_age_and_keeps_original_relay_age(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    monitor_only()
    item = delivery()
    with pytest.raises(BusError, match="Invalid message retention timestamp"):
        admit_delivery(store, config, {**item, "sent_at": "private invalid timestamp"})
    assert RemoteStore.monitor_history(store, repository=None) == []
    now = datetime.now(UTC).replace(microsecond=0)
    item["sent_at"] = now.astimezone(timezone(timedelta(hours=2))).isoformat()
    assert admit_delivery(store, config, item) == ("monitor", None, True)
    with store.transaction() as db:
        assert db.execute("SELECT retention_at FROM remote_monitor").fetchone() == (
            now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        )
    assert RemoteStore.monitor_history(store, repository=None)[0][3] == item


def test_inbox_admission_remains_native_when_policy_changes_to_monitor(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    item = delivery()
    admitted = admit_delivery(store, config, item)
    assert admitted is not None
    assert admitted[0] == "inbox"
    local_id = admitted[1]
    path = tmp_path / "config.toml"
    path.write_text(
        path.read_text().replace('receive_messages = "inbox"', 'receive_messages = "monitor"')
    )
    assert remote.admit_message(item, config.repositories[0].scope) == ("inbox", local_id, False)
    assert RemoteStore.monitor_history(store, repository=None) == []


def test_monitor_rejects_unknown_recipient_and_scope_and_validates_history(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    remote = RemoteStore(store, config.connection)
    path = tmp_path / "config.toml"
    path.write_text(
        path.read_text().replace('receive_messages = "inbox"', 'receive_messages = "monitor"')
    )
    assert remote.admit_message(delivery(), config.repositories[0].scope) is None
    store.register("claude", "recipient", str(tmp_path / "other"))
    assert remote.admit_message(delivery(), config.repositories[0].scope) is None
    for options in ({"limit": 0}, {"limit": 101}, {"before": 0}):
        with pytest.raises(BusError):
            RemoteStore.monitor_history(store, repository=None, **options)


def test_monitor_concurrent_admission_and_retained_repository_pagination(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    path = tmp_path / "config.toml"
    path.write_text(
        path.read_text().replace('receive_messages = "inbox"', 'receive_messages = "monitor"')
    )
    item = delivery()

    def admit(_index: int) -> tuple[str, int | None, bool] | None:
        return RemoteStore(Store(store.path), config.connection).admit_message(
            item, config.repositories[0].scope
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        admissions = list(executor.map(admit, range(2)))
    assert all(row is not None and row[:2] == ("monitor", None) for row in admissions)
    assert sum(row[2] for row in admissions if row is not None) == 1
    assert len(RemoteStore.monitor_history(store, repository=None)) == 1
    assert remote.admit_message(delivery(), config.repositories[0].scope) == (
        "monitor",
        None,
        True,
    )
    path.write_text("broken = [")
    rows = RemoteStore.monitor_history(store, repository=str(tmp_path / "repo"), limit=1)
    assert len(rows) == 1
    older = RemoteStore.monitor_history(store, repository=str(tmp_path / "repo"), before=rows[0][0])
    assert len(older) == 1
    assert older[0][3] == item
    assert older[0][1:3] == (config.repositories[0].scope, config.connection)
    assert RemoteStore.monitor_history(store, repository=str(tmp_path / "elsewhere")) == []
    assert store.history(folder=None) == []


def test_monitor_failed_commit_never_receipts_or_wakes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    path = tmp_path / "config.toml"
    path.write_text(
        path.read_text().replace('receive_messages = "inbox"', 'receive_messages = "monitor"')
    )
    with store.transaction() as db:
        db.execute(
            "CREATE TRIGGER reject_monitor BEFORE INSERT ON remote_monitor "
            "BEGIN SELECT RAISE(ABORT, 'disk failure'); END"
        )

    def request(_self: object, _method: str, route: str, _data: object = None) -> object:
        if route.startswith("/v1/inbox?"):
            return [delivery()] if "after=0" in route else []
        if route.startswith("/v1/sessions"):
            return []
        raise AssertionError("Failed persistence must not request receipts or wakes")

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    monkeypatch.setenv("LLM_BUS_DB", str(store.path))
    assert main(["bridge", "--once"]) == 1
    assert RemoteStore.monitor_history(store, repository=None) == []
    assert store.inbox("claude:recipient") == []
