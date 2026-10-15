"""Remote persistence and policy contracts; tests never wake real agent sessions."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast, final, override
from uuid import uuid4

import pytest

from llm_bus import main
from llm_bus_remote import (
    Bridge,
    Config,
    Repository,
    enqueue_remote,
    load_config,
    read_token,
    run_bridge,
)
from llm_bus_remote_store import Delivery, RemoteStore
from llm_bus_store import BusError, SenderContext, Store, routing_scope

if TYPE_CHECKING:
    from pathlib import Path


def setup_machine(tmp_path: Path, machine: str) -> tuple[Store, Config]:
    token = tmp_path / f"{machine}.token"
    token.write_text("a" * 43)
    token.chmod(0o600)
    folder = tmp_path / "repo"
    folder.mkdir(exist_ok=True)
    config = Config(
        "https://bus.test",
        machine,
        token,
        (Repository(routing_scope(str(folder)), "xyz", send=True, receive=True),),
    )
    return Store(tmp_path / f"{machine}.sqlite3"), config


def delivery(remote_id: str | None = None) -> Delivery:
    return Delivery(
        id=remote_id or str(uuid4()),
        sequence=1,
        sender="a/xyz/codex:sender",
        recipient="b/xyz/claude:recipient",
        repo_id="xyz",
        body="review migration",
        sent_at="2026-10-15T00:00:00.000Z",
    )


def test_remote_send_survives_reopen_and_cannot_bypass_repo_policy(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "a")
    sender = SenderContext("codex", "sender", str(tmp_path / "repo"))
    sent = enqueue_remote(store, config, sender, "b/xyz/claude:recipient", "hello")
    reopened = RemoteStore(Store(store.path), config.connection)
    assert reopened.outgoing()[0][0]["id"] == sent["remote_id"]
    assert reopened.outgoing()[0][1] == "queued_local"
    assert store.inbox("b/xyz/claude:recipient")[0]["body"] == "hello"
    assert [peer["address"] for peer in store.agents()] == ["codex:sender"]
    for recipient in ("b/other/claude:recipient", "a/xyz/claude:recipient"):
        with pytest.raises(BusError, match="same repository"):
            enqueue_remote(store, config, sender, recipient, "denied")
    disabled = replace(config, repositories=(replace(config.repositories[0], send=False),))
    with pytest.raises(BusError, match="disabled"):
        enqueue_remote(store, disabled, sender, "b/xyz/claude:recipient", "denied")
    with pytest.raises(BusError, match="another relay"):
        RemoteStore(store, "https://different.test|a")


def test_import_deduplicates_after_restart_and_preserves_ack_ownership(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    item = delivery()
    local_id = remote.import_message(item, config.repositories[0].scope)
    assert local_id is not None
    reopened = RemoteStore(Store(store.path), config.connection)
    assert reopened.import_message(item, config.repositories[0].scope) == local_id
    assert len(store.inbox("claude:recipient")) == 1
    with pytest.raises(BusError, match="different content"):
        reopened.import_message({**item, "body": "changed"}, config.repositories[0].scope)
    with pytest.raises(BusError, match="do not belong"):
        store.acknowledge("codex:other", [local_id])
    store.acknowledge("claude:recipient", [local_id])
    assert reopened.acknowledgements() == [(item["id"], "xyz")]
    reopened.mark_acknowledged(item["id"])
    assert reopened.acknowledgements() == []


def test_session_moving_repository_does_not_import_old_repo_messages(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "elsewhere"))
    remote = RemoteStore(store, config.connection)
    assert remote.import_message(delivery(), config.repositories[0].scope) is None
    assert store.inbox("claude:recipient") == []


def test_config_fails_closed_and_tokens_are_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, config = setup_machine(tmp_path, "b")
    path = tmp_path / "config.toml"
    monkeypatch.setenv("LLM_BUS_CONFIG", str(path))
    content = """[remote]
url = "https://bus.test"
machine_id = "b"
credential_file = "b.token"
[[repositories]]
path = "repo"
repo_id = "xyz"
send_to = ["xyz"]
receive_from = []
"""
    path.write_text(content)
    loaded = load_config()
    assert loaded is not None
    assert loaded.repositories[0].send
    assert not loaded.repositories[0].receive
    assert read_token(config.credential_file) == "a" * 43
    path.write_text(content.replace('send_to = ["xyz"]', 'send_to = ["other"]'))
    with pytest.raises(BusError, match="same-repository"):
        load_config()
    path.write_text(content.replace("https://bus.test", "http://bus.test"))
    with pytest.raises(BusError, match="HTTPS"):
        load_config()
    if config.credential_file.stat().st_mode & 0o077 == 0:
        config.credential_file.chmod(0o644)
        if os.name == "posix":
            with pytest.raises(BusError, match="mode 600"):
                read_token(config.credential_file)
    link = tmp_path / "link"
    try:
        link.symlink_to(config.credential_file)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(BusError, match="symlink"):
        read_token(link)


def test_bridge_lost_receipt_replays_once_then_syncs_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    item = delivery()
    pending = True
    lose_receipt = True
    acknowledged = False
    wake_calls: list[str] = []

    def request(_self: object, method: str, path: str, data: object = None) -> object:
        nonlocal pending, lose_receipt, acknowledged
        if path == "/v1/sessions":
            return {"address": "b/xyz/claude:recipient"}
        if path.startswith("/v1/sessions?"):
            return []
        if path.startswith("/v1/inbox?"):
            if pending and "after=0" in path:
                return [item]
            return []
        if path == "/v1/receipts":
            if lose_receipt:
                lose_receipt = False
                raise BusError("response lost")
            pending = False
            return {"status": "delivered_local"}
        if path == "/v1/ack":
            acknowledged = True
            return {"status": "acknowledged"}
        raise AssertionError((method, path, data))

    def record_wake(_self: object, message: Delivery, _local_id: int) -> None:
        wake_calls.append(message["id"])

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    monkeypatch.setattr(Bridge, "_wake", record_wake)
    assert Bridge(store, config).sync()["errors"] == 1
    assert len(store.inbox("claude:recipient")) == 1
    Bridge(Store(store.path), config).sync()
    assert wake_calls == [item["id"]]
    assert len(store.inbox("claude:recipient")) == 1
    store.acknowledge("claude:recipient", [store.inbox("claude:recipient")[0]["id"]])
    Bridge(Store(store.path), config).sync()
    assert acknowledged
    assert RemoteStore(store, config.connection).acknowledgements() == []


def test_cli_remote_send_queues_offline_and_native_inbox_reads_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, config = setup_machine(tmp_path, "a")
    path = tmp_path / "config.toml"
    path.write_text(f'''[remote]
url = "https://bus.test"
machine_id = "a"
credential_file = "a.token"
[[repositories]]
path = "{(tmp_path / "repo").as_posix()}"
repo_id = "xyz"
''')
    monkeypatch.setenv("LLM_BUS_CONFIG", str(path))
    monkeypatch.setenv("LLM_BUS_DB", str(store.path))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", "sender")
    monkeypatch.chdir(tmp_path / "repo")
    assert main(["send", "b/xyz/claude:recipient", "--body", "offline"]) == 0
    result = cast("dict[str, object]", json.loads(capsys.readouterr().out))
    assert result["status"] == "queued_local"
    assert main(["send", "b/other/claude:recipient", "--cross-folder", "--body", "deny"]) == 1
    assert len(RemoteStore(store, config.connection).outgoing()) == 1


def test_continuous_bridge_retries_truncated_http_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, config = setup_machine(tmp_path, "a")
    uploads: list[object] = []

    @final
    class Handler(BaseHTTPRequestHandler):
        # These method names, parameter names, and connection state belong to the stdlib API.
        # pylint: disable=invalid-name,redefined-builtin,attribute-defined-outside-init
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"[]")

        def do_POST(self) -> None:
            payload = cast(
                "object", json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            self.send_response(200)
            if self.path == "/v1/messages":
                uploads.append(payload)
                if len(uploads) == 1:
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(b'20\r\n{"status":')
                    self.close_connection = True
                    return
            self.end_headers()
            self.wfile.write(b'{"status":"accepted_remote"}')

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = replace(
        config,
        url=f"http://127.0.0.1:{server.server_port}",
        repositories=(replace(config.repositories[0], receive=False),),
    )
    sender = SenderContext("codex", "sender", str(tmp_path / "repo"))
    sent = enqueue_remote(store, config, sender, "b/xyz/claude:recipient", "retry me")
    states: list[str] = []

    def next_pass(_seconds: float) -> None:
        states.append(RemoteStore(Store(store.path), config.connection).outgoing()[0][1])
        if len(states) == 2:
            raise KeyboardInterrupt

    def forbid_wake(*_args: object, **_kwargs: object) -> None:
        pytest.fail("HTTP retries must not wake a real agent")

    monkeypatch.setattr("llm_bus_remote.load_config", lambda: config)
    # Subprocess timeout waits need the real clock.
    monkeypatch.setattr("llm_bus_remote.time", SimpleNamespace(sleep=next_pass))
    monkeypatch.setattr("llm_bus_remote.wake", forbid_wake)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_bridge(store, once=False)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert states == ["queued_local", "accepted_remote"]
    assert len(uploads) == 2
    assert uploads[0] == uploads[1]
    assert cast("dict[str, object]", uploads[1])["id"] == sent["remote_id"]
    results = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert results == [
        {"uploaded": 0, "imported": 0, "acknowledged": 0, "errors": 1},
        {"uploaded": 1, "imported": 0, "acknowledged": 0, "errors": 0},
    ]
