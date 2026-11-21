"""Main-agent routing, including retained registrations created by older clients."""

import io
import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from llm_bus.cli.app import main
from llm_bus.errors import BusError
from llm_bus.hosts.sessions import is_subagent
from llm_bus.hosts.settings import configure_hosts
from llm_bus.locks.models import Request
from llm_bus.locks.service import Locks
from llm_bus.locks.timer import run_timer
from llm_bus.mailbox import connection
from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.store import Store
from llm_bus.remote.bridge import Bridge
from llm_bus.remote.discovery import publish_sessions
from llm_bus.remote.outgoing import permits_outgoing
from llm_bus.remote.store import RemoteStore
from llm_bus.reports.store import Reports
from llm_bus.reports.sync import submit_reports
from llm_bus.wake.adapters import wake
from llm_bus.wake.dispatch import dispatch_pending
from tests.locks.support import no_timer, overdue, record_wakes
from tests.remote.support import delivery, setup_machine


@pytest.fixture(name="native_state")
def native_state_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    def machine_home() -> Path:
        return tmp_path

    monkeypatch.setattr(Path, "home", machine_home)
    home = tmp_path / "codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    path = home / "state_5.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE thread_spawn_edges(parent_thread_id, child_thread_id, status)")
    return path


def mark_child(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO thread_spawn_edges VALUES ('parent','child','open')")


def test_installed_native_home_survives_other_observers_and_reinstallation(
    native_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    child = store.register("codex", "child", str(tmp_path))
    parent = store.register("codex", "parent", str(tmp_path))
    sender = SenderContext("codex", "parent", str(tmp_path))
    store.send(sender, child, "retained pending work")
    mark_child(native_state)
    configure_hosts(tmp_path, install=True)
    monkeypatch.delenv("CODEX_HOME")
    assert is_subagent(child)
    assert not is_subagent(parent)
    assert [a["address"] for a in store.agents()] == [parent]
    with pytest.raises(BusError, match="Sub-agents"):
        store.send(sender, child, "mixed-host attempt")
    assert store.pending_count(child) == 0
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "second-codex-home"))
    configure_hosts(tmp_path, install=True)
    monkeypatch.delenv("CODEX_HOME")
    assert is_subagent(child)
    assert store.inbox(child) == []


def test_retained_child_cannot_receive_or_send(
    native_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    child = store.register("codex", "child", str(tmp_path))
    parent = store.register("codex", "parent", str(tmp_path))
    sender = SenderContext("codex", "parent", str(tmp_path))
    stored = store.send(sender, child, "retained pending work")
    mark_child(native_state)

    assert is_subagent(child)
    assert not is_subagent(parent)
    assert [a["address"] for a in store.agents()] == [parent]
    assert len(store.all_agents()) == 2
    with pytest.raises(BusError, match="Sub-agents"):
        store.send(sender, child, "new work")
    with pytest.raises(BusError, match="Sub-agents"):
        store.send(SenderContext("codex", "child", str(tmp_path)), parent, "child report")
    with pytest.raises(BusError, match="Sub-agents"):
        store.deliver_collab("collab:peer", child, "collaborator work")
    assert store.inbox(child) == []
    assert store.pending_count(child) == 0
    assert not store.pending_allowed(stored["id"])
    assert len(store.history(folder=None)) == 1

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("A child must never reach native wake")

    monkeypatch.setattr("llm_bus.wake.dispatch.wake", forbidden)
    dispatch_pending(store, child)
    assert wake(child, stored["id"], parent)["status"] == "unsupported"


def test_remote_child_admission_denied_and_advertisement_withdrawn(
    native_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BUS_CONFIG", str(tmp_path / "config.toml"))
    store, config = setup_machine(tmp_path, "b")
    folder = str(tmp_path / "repo")
    store.register("codex", "child", folder)
    store.register("codex", "parent", folder)
    bridge = Bridge(store, config)
    publications: list[dict[str, object]] = []

    def request(_method: str, _path: str, payload: object = None) -> object:
        assert isinstance(payload, dict)
        publications.append(cast("dict[str, object]", payload))
        return {}

    monkeypatch.setattr(bridge.relay, "request", request)
    assert publish_sessions(bridge) == 0
    assert all(not p["ended"] for p in publications)
    publications.clear()
    mark_child(native_state)
    assert publish_sessions(bridge) == 0
    assert len(publications) == 1
    assert publications[0]["native_address"] == "codex:child"
    assert publications[0]["ended"] is True
    remote = RemoteStore(store, config.connection)
    item = delivery()
    item["recipient"] = "b/xyz/codex:child"
    assert remote.admit_message(item, config.repositories[0].scope) is None
    assert store.history(folder=None) == []
    item["recipient"] = "b/xyz/codex:parent"
    assert remote.admit_message(item, config.repositories[0].scope) is not None
    assert len(store.inbox("codex:parent")) == 1
    with pytest.raises(BusError, match="Sub-agents"):
        remote.enqueue(
            SenderContext("codex", "child", folder), "a/xyz/codex:parent", "xyz", "report"
        )


def test_idle_roster_poll_bounds_database_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BUS_CONFIG", str(tmp_path / "config.toml"))
    store, config = setup_machine(tmp_path, "b")
    folder = str(tmp_path / "repo")
    for number in range(12):
        session = f"historical-{number}"
        store.register("claude", session, folder)
        store.end_session("claude", session)
    bridge = Bridge(store, config)

    def accept_request(*_args: object) -> dict[str, object]:
        return {}

    monkeypatch.setattr(bridge.relay, "request", accept_request)
    assert publish_sessions(bridge) == 0

    def unexpected_request(*_args: object) -> object:
        pytest.fail("An unchanged roster must not be republished")

    original_connect = connection.connect
    connections = 0

    def count_connection(path: Path) -> sqlite3.Connection:
        nonlocal connections
        connections += 1
        return original_connect(path)

    monkeypatch.setattr(bridge.relay, "request", unexpected_request)
    monkeypatch.setattr(connection, "connect", count_connection)
    assert publish_sessions(bridge) == 0
    assert connections <= 4


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit", "SessionEnd"])
@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_child_hook_cannot_change_parent_lifecycle(
    kind: str,
    event: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "bus.sqlite3"
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    store = Store(path)
    parent = store.register(kind, "parent", str(tmp_path))
    turns: list[str] = []

    def record_turn(_store: Store, address: str, **_options: bool) -> None:
        turns.append(address)

    def not_subagent(_address: str) -> bool:
        return False

    monkeypatch.setattr("llm_bus.cli.hooks.record_turn", record_turn)
    monkeypatch.setattr("llm_bus.cli.hooks.is_subagent", not_subagent)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps({"session_id": "parent", "agent_id": "child", "hook_event_name": event})
        ),
    )
    assert main(["hook", kind]) == 0
    assert turns == []
    output = capsys.readouterr().out
    if event == "SessionEnd":
        assert output == ""
    else:
        assert "Report only to your parent" in output
    assert not store.is_ended(parent)
    assert len(store.all_agents()) == 1


def test_queued_child_outgoing_is_held(
    native_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BUS_CONFIG", str(tmp_path / "config.toml"))
    store, config = setup_machine(tmp_path, "b")
    remote = RemoteStore(store, config.connection)
    remote.enqueue(
        SenderContext("codex", "child", str(tmp_path / "repo")),
        "a/xyz/codex:parent",
        "xyz",
        "old queued report",
    )
    envelope, _state = remote.outgoing()[0]
    bridge = Bridge(store, config)
    assert permits_outgoing(bridge, envelope)
    mark_child(native_state)
    assert not permits_outgoing(bridge, envelope)
    assert len(store.history(folder=None)) == 1


def test_codex_child_hook_and_cli_do_not_register(
    native_state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mark_child(native_state)
    monkeypatch.setenv("LLM_BUS_DB", str(tmp_path / "bus.sqlite3"))
    monkeypatch.setenv("CODEX_THREAD_ID", "child")
    assert main(["whoami"]) == 1
    assert "only main agents" in capsys.readouterr().err
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"session_id": "child", "hook_event_name": "SessionStart"})),
    )
    assert main(["hook", "codex"]) == 0
    result = cast("dict[str, dict[str, str]]", json.loads(capsys.readouterr().out))
    assert "Report only to your parent" in result["hookSpecificOutput"]["additionalContext"]
    assert Store(tmp_path / "bus.sqlite3").all_agents() == []


def test_legacy_child_locks_do_not_block_parent_hooks_or_handoff(
    native_state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("llm_bus.locks.service.schedule_check", no_timer)
    wakes = record_wakes(monkeypatch)
    store = Store(tmp_path / "bus.sqlite3")
    monkeypatch.setenv("LLM_BUS_DB", str(store.path))
    child = store.register("codex", "child", str(tmp_path))
    waiter = store.register("claude", "waiter", str(tmp_path))
    children = Locks(store, child, str(tmp_path))
    parents = Locks(store, store.register("codex", "parent", str(tmp_path)), str(tmp_path))
    legacy = children.key("legacy-db", shared=False)
    deploy = parents.key("deploy", shared=False)
    children.acquire(legacy, Request(check="1h", describe="existing child work"))
    parents.acquire(deploy, Request(check="1h", describe="deploy"))
    children.acquire(deploy, Request(check="1h"))
    Locks(store, waiter, str(tmp_path)).acquire(deploy, Request(check="1h"))
    with store.transaction() as db:
        expiry = cast(
            "tuple[str]",
            db.execute(
                "SELECT expires_at FROM resource_locks WHERE resource='legacy-db'"
            ).fetchone(),
        )
    overdue(store)
    mark_child(native_state)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {"session_id": "parent", "hook_event_name": "SessionStart", "cwd": str(tmp_path)}
            )
        ),
    )
    assert main(["hook", "codex"]) == 0
    assert "Local agent bus address" in capsys.readouterr().out
    parents.status(None, every_scope=True)
    queued = parents.acquire(legacy, Request(check="1h", notify=True))
    assert queued["status"] == "waiting"
    assert queued["notified"] is False
    with store.transaction() as db:
        assert db.execute(
            "SELECT holder,expires_at FROM resource_locks WHERE resource='legacy-db'"
        ).fetchone() == (child, expiry[0])
        assert (
            db.execute("SELECT 1 FROM resource_lock_waiters WHERE waiter=?", (child,)).fetchone()
            is None
        )
        assert db.execute("SELECT 1 FROM messages WHERE recipient=?", (child,)).fetchone() is None
    assert parents.release(deploy)["granted_to"] == waiter
    assert child not in wakes


def test_legacy_child_timer_grants_main_waiter_only_after_expiry(
    native_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("llm_bus.locks.service.schedule_check", no_timer)
    record_wakes(monkeypatch)
    store = Store(tmp_path / "bus.sqlite3")
    child = store.register("codex", "child", str(tmp_path))
    parent = store.register("codex", "parent", str(tmp_path))
    children = Locks(store, child, str(tmp_path))
    parents = Locks(store, parent, str(tmp_path))
    key = children.key("legacy-db", shared=False)
    children.acquire(key, Request(check="1h", describe="existing child work"))
    parents.acquire(key, Request(check="1h"))
    mark_child(native_state)
    with store.transaction() as db:
        db.execute(
            "UPDATE resource_locks SET expires_at='2000-01-01T00:00:00Z',check_at='2000-01-01T00:00:00Z'"
        )
    run_timer(store, key)
    with store.transaction() as db:
        holder = cast("tuple[str]", db.execute("SELECT holder FROM resource_locks").fetchone())
    assert holder[0] == parent
    messages = store.inbox(parent)
    assert len(messages) == 1
    assert "lock granted" in messages[0]["body"]
    assert messages[0]["sender"] == parent


def test_queued_child_reports_are_retained_without_upload(
    native_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("LLM_BUS_CONFIG", str(config_path))
    store, config = setup_machine(tmp_path, "b")
    with config_path.open("a") as stream:
        stream.write('send_reports = ["received"]\n')
    store.register("codex", "child", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    item = delivery()
    item["recipient"] = "b/xyz/codex:child"
    admission = remote.admit_message(item, config.repositories[0].scope)
    assert admission is not None
    assert admission[1] is not None
    reports = Reports(store, config.connection)
    reports.enqueue(admission[1], "codex:child", "received", None)
    mark_child(native_state)
    bridge = Bridge(store, config)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("Neither a receipt nor a report may upload for a child")

    monkeypatch.setattr(bridge.relay, "request", forbidden)
    assert submit_reports(bridge) == 0
    assert len(reports.pending()) == 1
    assert len(store.history(folder=None)) == 1


def test_codex_child_session_end_retires_legacy_registration(
    native_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bus.sqlite3"
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    store = Store(path)
    child = store.register("codex", "child", str(tmp_path))
    mark_child(native_state)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"session_id": "child", "hook_event_name": "SessionEnd"})),
    )
    assert main(["hook", "codex"]) == 0
    assert store.is_ended(child)
    assert len(store.all_agents()) == 1
