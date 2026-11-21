"""send."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, cast

import pytest

from llm_bus.cli.app import main
from llm_bus.errors import BusError
from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.store import Store

if TYPE_CHECKING:
    from pathlib import Path


def test_folder_scope_requires_explicit_cross_folder_send(tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    sender = store.register("codex", "one", "/one")
    same = store.register("claude", "two", "/one")
    other = store.register("claude", "three", "/other")
    assert {peer["address"] for peer in store.agents("/one")} == {sender, same}
    assert len(store.agents()) == 3
    # A concurrent hook can register the same sender in another folder before send begins.
    store.register("codex", "one", "/other")
    with pytest.raises(BusError, match="--cross-folder"):
        store.send(SenderContext("codex", "one", "/one"), other, "hello")
    sent = store.send(SenderContext("codex", "one", "/one"), other, "hello", cross_folder=True)
    assert sent["sender_cwd"] == "/one"
    assert sent["recipient_cwd"] == "/other"
    store.register("codex", "one", "/new-location")
    store.register("claude", "three", "/new-location")
    assert store.inbox(other)[0]["sender_cwd"] == "/one"
    assert store.inbox(other)[0]["recipient_cwd"] == "/other"


def test_existing_database_gets_nullable_sender_folder(tmp_path: Path) -> None:
    path = tmp_path / "bus.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE agents (address TEXT PRIMARY KEY, kind TEXT, project TEXT, last_seen TEXT);"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, sender TEXT, recipient TEXT, "
            "body TEXT, sent_at TEXT, acknowledged_at TEXT);"
            "INSERT INTO agents VALUES ('codex:one', 'codex', '/one', 'now');"
            "INSERT INTO agents VALUES ('claude:two', 'claude', '/one', 'now');"
            "INSERT INTO messages VALUES (1, 'codex:one', 'claude:two', 'old', "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), NULL);"
        )
    migrated = Store(path)
    assert migrated.inbox("claude:two")[0]["sender_cwd"] is None
    assert migrated.inbox("claude:two")[0]["recipient_cwd"] is None
    assert migrated.history(folder=None)[0]["wake_status"] is None
    assert migrated.all_agents()[0]["ended_at"] is None
    assert len(migrated.agents("/one")) == 2


def test_history_preserves_status_and_send_time_folders(tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("claude", "two", "/other")
    first = store.send(
        SenderContext("codex", "one", "/first"), recipient, "first", cross_folder=True
    )
    second = store.send(
        SenderContext("codex", "one", "/first"), recipient, "second", cross_folder=True
    )
    unrelated = store.register("codex", "three", "/third")
    third = store.send(SenderContext("claude", "four", "/third"), unrelated, "third")
    store.acknowledge(recipient, [first["id"]])
    store.register("claude", "two", "/moved")

    assert [item["id"] for item in store.history(folder="/first")] == [second["id"], first["id"]]
    assert [item["id"] for item in store.history(folder="/other")] == [second["id"], first["id"]]
    assert store.history(folder="/moved") == []
    assert [item["id"] for item in store.history(folder=None, repository="/first")] == [
        second["id"],
        first["id"],
    ]
    assert [item["id"] for item in store.history(folder=None, limit=2)] == [
        third["id"],
        second["id"],
    ]
    assert [item["id"] for item in store.history(folder=None, before=third["id"])] == [
        second["id"],
        first["id"],
    ]
    assert [item["id"] for item in store.history(folder=None, after=first["id"])] == [
        second["id"],
        third["id"],
    ]
    assert store.history(folder="/first")[1]["acknowledged_at"] is not None
    assert store.history(folder="/first")[0]["acknowledged_at"] is None
    assert store.history(folder="/first")[0]["wake_status"] is None
    assert store.inbox(recipient) == [second]


def test_send_cli_reply_uses_local_parent_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bus.sqlite3"
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    monkeypatch.setenv("CODEX_THREAD_ID", "one")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.chdir(tmp_path)

    def isolated_dispatch(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr("llm_bus.cli.commands.dispatch_pending", isolated_dispatch)
    store = Store(path)
    store.register("claude", "two", str(tmp_path))
    parent = store.send(SenderContext("codex", "one", str(tmp_path)), "claude:two", "question")
    assert main(["send", "claude:two", "--reply-to", str(parent["id"]), "--body", "follow up"]) == 0
    assert json.loads(capsys.readouterr().out)["reply_to"] == parent["id"]
    assert main(["send", "claude:two", "--reply-to", "0", "--body", "bad"]) == 1
    assert "positive integer" in capsys.readouterr().err
    assert len(store.history(folder=None)) == 2


def test_send_keeps_message_when_wake_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    path = tmp_path / "bus.sqlite3"
    recipient = Store(path).register("codex", "recipient", str(folder))
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    monkeypatch.setenv("CODEX_THREAD_ID", "sender")
    monkeypatch.chdir(folder)
    called: list[tuple[str, int]] = []

    def fail_wake(address: str, message_id: int, _sender: str) -> dict[str, str]:
        called.append((address, message_id))
        return {"status": "failed", "via": "codex queue", "reason": "not reachable"}

    monkeypatch.setattr("llm_bus.wake.dispatch.wake", fail_wake)
    assert main(["send", recipient, "--body", "Please reply"]) == 0
    output = capsys.readouterr()
    sent = cast("dict[str, object]", json.loads(output.out))
    assert sent["wake"] == {"status": "failed", "via": "codex queue", "reason": "not reachable"}
    assert "stored; wake failed" in output.err
    assert called == [(recipient, sent["id"])]
    pending = Store(path).inbox(recipient)
    assert len(pending) == 1
    assert pending[0]["id"] == sent["id"]
    history = Store(path).history(folder=str(folder))[0]
    assert history["wake_status"] == "failed"
    assert history["wake_via"] == "codex queue"
    assert history["wake_reason"] == "wake_failed:codex_queue"
    assert history["wake_checked_at"] is not None


def test_send_to_ended_session_stores_message_without_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    path = tmp_path / "bus.sqlite3"
    store = Store(path)
    recipient = store.register("codex", "recipient", str(folder))
    store.end_session("codex", "recipient")
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    monkeypatch.setenv("CODEX_THREAD_ID", "sender")
    monkeypatch.chdir(folder)

    def unexpected_wake(_address: str, _message_id: int, _sender: str) -> dict[str, str]:
        pytest.fail("ended session must not be woken")

    monkeypatch.setattr("llm_bus.wake.dispatch.wake", unexpected_wake)
    assert main(["send", recipient, "--body", "Please reply"]) == 0
    sent = cast("dict[str, object]", json.loads(capsys.readouterr().out))
    assert cast("dict[str, str]", sent["wake"])["status"] == "failed"
    assert len(Store(path).inbox(recipient)) == 1
    assert Store(path).history(folder=str(folder))[0]["wake_status"] == "failed"


def test_send_reports_message_id_when_wake_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    path = tmp_path / "bus.sqlite3"
    recipient = Store(path).register("codex", "recipient", str(folder))
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    monkeypatch.setenv("CODEX_THREAD_ID", "sender")
    monkeypatch.chdir(folder)

    def broken_wake(_address: str, _message_id: int, _sender: str) -> dict[str, str]:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")

    monkeypatch.setattr("llm_bus.wake.dispatch.wake", broken_wake)
    assert main(["send", recipient, "--body", "Please reply"]) == 0
    output = capsys.readouterr()
    sent = cast("dict[str, object]", json.loads(output.out))
    assert sent["id"] == 1
    assert sent["wake"] == {
        "status": "unknown",
        "via": "host wake",
        "reason": "wake adapter raised UnicodeDecodeError",
    }
    assert "message #1 stored; wake unknown" in output.err
    assert len(Store(path).inbox(recipient)) == 1
    assert Store(path).history(folder=str(folder))[0]["wake_status"] == "unknown"


def test_send_reports_message_id_when_wake_recording_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    path = tmp_path / "bus.sqlite3"
    recipient = Store(path).register("codex", "recipient", str(folder))
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    monkeypatch.setenv("CODEX_THREAD_ID", "sender")
    monkeypatch.chdir(folder)

    def requested_wake(_address: str, _id: int, _sender: str) -> dict[str, str]:
        return {"status": "requested", "via": "codex queue"}

    monkeypatch.setattr("llm_bus.wake.dispatch.wake", requested_wake)

    def fail_record(*_args: object) -> None:
        raise sqlite3.OperationalError("database locked")

    monkeypatch.setattr("llm_bus.wake.dispatch._finish", fail_record)
    assert main(["send", recipient, "--body", "Please reply"]) == 0
    output = capsys.readouterr()
    sent = cast("dict[str, object]", json.loads(output.out))
    assert sent["id"] == 1
    assert "could not record wake result" in output.err
    assert Store(path).inbox(recipient)[0]["id"] == 1
    assert Store(path).history(folder=str(folder))[0]["wake_status"] is None
