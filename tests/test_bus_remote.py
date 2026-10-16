"""Remote persistence and policy contracts; tests never wake real agent sessions."""

# pylint: disable=too-many-lines
# Remote transport scenarios share the isolated machine and relay fixtures.
from __future__ import annotations

import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from typing import cast, final, override
from uuid import uuid4

import pytest

from llm_bus import main
from llm_bus_policy import Config, PeerPolicy, Repository, load_config, read_token
from llm_bus_remote import (
    Bridge,
    enqueue_remote,
    run_bridge,
)
from llm_bus_remote_store import Delivery, RemoteStore
from llm_bus_reports import Reports
from llm_bus_store import BusError, SenderContext, Store, routing_scope


@pytest.fixture(autouse=True)
def isolated_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BUS_CONFIG", str(tmp_path / "config.toml"))


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
        (
            Repository(
                routing_scope(str(folder)),
                "xyz",
                (
                    PeerPolicy(
                        "b" if machine == "a" else "a", send_messages=True, receive_messages="inbox"
                    ),
                ),
            ),
        ),
    )
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(f'''policy_version = 2
[remote]
url = "https://bus.test"
machine_id = "{machine}"
credential_file = "{machine}.token"
[[repositories]]
path = "{folder.as_posix()}"
repo_id = "xyz"
[[repositories.peers]]
machine_id = "{"b" if machine == "a" else "a"}"
send_messages = true
receive_messages = "inbox"
''')
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
    disabled = replace(config, repositories=(replace(config.repositories[0], peers=()),))
    with pytest.raises(BusError, match="disabled"):
        enqueue_remote(store, disabled, sender, "b/xyz/claude:recipient", "denied")
    with pytest.raises(BusError, match="another relay"):
        RemoteStore(store, "https://different.test|a")


def test_import_deduplicates_after_restart_and_preserves_ack_ownership(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    item = delivery()
    admitted = remote.admit_message(item, config.repositories[0].scope)
    assert admitted is not None
    assert admitted[0] == "inbox"
    local_id = admitted[1]
    assert local_id is not None
    reopened = RemoteStore(Store(store.path), config.connection)
    assert reopened.admit_message(item, config.repositories[0].scope) == ("inbox", local_id)
    assert len(store.inbox("claude:recipient")) == 1
    with pytest.raises(BusError, match="different content"):
        reopened.admit_message({**item, "body": "changed"}, config.repositories[0].scope)
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
    assert remote.admit_message(delivery(), config.repositories[0].scope) is None
    assert store.inbox("claude:recipient") == []


def test_config_fails_closed_and_tokens_are_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, config = setup_machine(tmp_path, "b")
    path = tmp_path / "config.toml"
    monkeypatch.setenv("LLM_BUS_CONFIG", str(path))
    content = """policy_version = 2
[remote]
url = "https://bus.test"
machine_id = "b"
credential_file = "b.token"
[[repositories]]
path = "repo"
repo_id = "xyz"
[[repositories.peers]]
machine_id = "a"
send_messages = true
receive_messages = "deny"
"""
    path.write_text(content)
    loaded = load_config()
    assert loaded is not None
    peer = loaded.peer("xyz", "a")
    assert peer is not None
    assert peer.send_messages
    assert peer.receive_messages == "deny"
    assert read_token(config.credential_file) == "a" * 43
    path.write_text(content.replace('machine_id = "a"', 'machine_id = "b"'))
    with pytest.raises(BusError, match="Self-machine"):
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
    path.write_text(f'''policy_version = 2
[remote]
url = "https://bus.test"
machine_id = "a"
credential_file = "a.token"
[[repositories]]
path = "{(tmp_path / "repo").as_posix()}"
repo_id = "xyz"
[[repositories.peers]]
machine_id = "b"
send_messages = true
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
    uploads: list[tuple[object, str | None]] = []

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
                uploads.append((payload, self.headers.get("User-Agent")))
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
        repositories=(
            replace(config.repositories[0], peers=(PeerPolicy("b", send_messages=True),)),
        ),
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
    assert {agent for _, agent in uploads} == {"llm-bus/0.1"}
    assert states == ["queued_local", "accepted_remote"]
    assert len(uploads) == 2
    assert uploads[0] == uploads[1]
    assert cast("dict[str, object]", uploads[1][0])["id"] == sent["remote_id"]
    results = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert results == [
        {"uploaded": 0, "imported": 0, "acknowledged": 0, "errors": 1},
        {"uploaded": 1, "imported": 0, "acknowledged": 0, "errors": 0},
    ]


@pytest.mark.parametrize("mode", ["deny", "monitor"])
def test_raw_relay_cannot_admit_denied_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'receive_messages = "inbox"', f'receive_messages = "{mode}"'
        ),
        encoding="utf-8",
    )
    item = delivery()
    allowed = {**item, "id": str(uuid4()), "sender": "c/xyz/codex:other", "sequence": 2}
    path.write_text(
        path.read_text(encoding="utf-8")
        + '\n[[repositories.peers]]\nmachine_id = "c"\nreceive_messages = "inbox"\n',
        encoding="utf-8",
    )
    receipts: list[object] = []
    wakes: list[str] = []

    def request(_self: object, _method: str, route: str, data: object = None) -> object:
        if route.startswith("/v1/inbox?"):
            return [item, allowed] if "after=0" in route else []
        if route == "/v1/receipts":
            receipts.append(data)
            return {}
        return []

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)

    def record_wake(_self: object, message: Delivery, _id: int) -> None:
        wakes.append(message["id"])

    monkeypatch.setattr(Bridge, "_wake", record_wake)
    assert Bridge(store, config).sync()["errors"] == 0
    assert [entry["sender"] for entry in store.inbox("claude:recipient")] == [allowed["sender"]]
    expected_receipts = ([{"id": item["id"]}] if mode == "monitor" else []) + [
        {"id": allowed["id"]}
    ]
    assert receipts == expected_receipts
    assert len(RemoteStore.monitor_history(store, repository=None)) == (
        1 if mode == "monitor" else 0
    )
    assert wakes == [allowed["id"]]


def test_revocation_filters_pending_history_and_deduplicated_import(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    item = delivery()
    admitted = remote.admit_message(item, config.repositories[0].scope)
    assert admitted is not None
    assert admitted[0] == "inbox"
    local_id = admitted[1]
    assert local_id is not None
    assert store.pending_count("claude:recipient") == 1
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'receive_messages = "inbox"', 'receive_messages = "deny"'
        ),
        encoding="utf-8",
    )
    reopened = Store(store.path)
    assert reopened.inbox("claude:recipient") == []
    assert reopened.pending_count("claude:recipient") == 0
    assert remote.admit_message(item, config.repositories[0].scope) is None
    assert len(reopened.history(folder=None)) == 1
    assert not reopened.pending_allowed(local_id)
    path.write_text("broken = [", encoding="utf-8")
    assert reopened.inbox("claude:recipient") == []
    assert reopened.pending_count("claude:recipient") == 0


def test_pending_uses_original_identity_scope_and_current_recipient_scope(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    folder = str(tmp_path / "repo")
    store.register("claude", "recipient", folder)
    remote = RemoteStore(store, config.connection)
    item = delivery()
    admitted = remote.admit_message(item, config.repositories[0].scope)
    assert admitted is not None
    assert admitted[0] == "inbox"
    local_id = admitted[1]
    assert local_id is not None
    store.register("claude", "recipient", str(tmp_path / "other"))
    assert store.inbox("claude:recipient") == []
    assert remote.admit_message(item, config.repositories[0].scope) is None
    store.register("claude", "recipient", folder)
    assert store.pending_count("claude:recipient") == 1
    path = Path(os.environ["LLM_BUS_CONFIG"])
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace('machine_id = "a"', 'machine_id = "c"'), encoding="utf-8")
    assert store.pending_count("claude:recipient") == 0
    path.write_text(original.replace('repo_id = "xyz"', 'repo_id = "other"'), encoding="utf-8")
    assert store.pending_count("claude:recipient") == 0
    path.write_text(original.replace(folder, str(tmp_path / "other")), encoding="utf-8")
    assert store.pending_count("claude:recipient") == 0


def test_revoked_outgoing_is_held_while_other_peer_uploads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, config = setup_machine(tmp_path, "a")
    sender = SenderContext("codex", "sender", str(tmp_path / "repo"))
    enqueue_remote(store, config, sender, "b/xyz/claude:recipient", "held")
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8").replace("send_messages = true", "send_messages = false")
        + '\n[[repositories.peers]]\nmachine_id = "c"\nsend_messages = true\n',
        encoding="utf-8",
    )
    current = load_config()
    assert current is not None
    enqueue_remote(store, current, sender, "c/xyz/claude:recipient", "allowed")
    uploaded: list[object] = []

    def request(_self: object, _method: str, route: str, data: object = None) -> object:
        if route == "/v1/messages":
            uploaded.append(data)
            return {"status": "accepted_remote"}
        return []

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    assert Bridge(store, config).sync()["uploaded"] == 1
    assert len(uploaded) == 1
    rows = RemoteStore(store, config.connection).outgoing()
    assert [state for _, state in rows] == ["queued_local", "accepted_remote"]
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            str(tmp_path / "repo"), str(tmp_path / "different")
        ),
        encoding="utf-8",
    )
    assert Bridge(store, config).sync()["uploaded"] == 0
    assert len(RemoteStore(store, config.connection).outgoing()) == 2


@pytest.mark.parametrize("change", ["peer", "invalid", "connection", "scope"])
def test_policy_change_during_sender_publication_holds_body_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    store, config = setup_machine(tmp_path, "a")
    path = Path(os.environ["LLM_BUS_CONFIG"])
    original = (
        path.read_text(encoding="utf-8")
        + '\n[[repositories.peers]]\nmachine_id = "c"\nsend_messages = true\n'
    )
    path.write_text(original, encoding="utf-8")
    config = load_config()
    assert config is not None
    sender = SenderContext("codex", "sender", str(tmp_path / "repo"))
    held = enqueue_remote(store, config, sender, "b/xyz/claude:recipient", "held")
    allowed = enqueue_remote(store, config, sender, "c/xyz/claude:recipient", "allowed")
    store.register("codex", "sender", str(tmp_path / "unmapped"))
    mutations = {
        "peer": original.replace("send_messages = true", "send_messages = false", 1),
        "invalid": "broken = [",
        "connection": original.replace("https://bus.test", "https://different.test"),
        "scope": original.replace(str(tmp_path / "repo"), str(tmp_path / "different")),
    }
    uploaded: list[object] = []
    publications: list[object] = []

    def request(_self: object, _method: str, route: str, data: object = None) -> object:
        if route == "/v1/sessions":
            publications.append(data)
            path.write_text(mutations[change], encoding="utf-8")
        if route == "/v1/messages":
            uploaded.append(data)
            return {"status": "accepted_remote"}
        return []

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    other_uploads = 1 if change == "peer" else 0
    assert Bridge(store, config).sync() == {
        "uploaded": other_uploads,
        "imported": 0,
        "acknowledged": 0,
        "errors": 0,
    }
    assert len(publications) == 1
    assert [cast("dict[str, object]", item)["id"] for item in uploaded] == (
        [allowed["remote_id"]] if other_uploads else []
    )
    assert {
        envelope["id"]: state
        for envelope, state in RemoteStore(Store(store.path), config.connection).outgoing()
    } == {
        held["remote_id"]: "queued_local",
        allowed["remote_id"]: "accepted_remote" if other_uploads else "queued_local",
    }


def test_policy_reload_after_wake_claim_prevents_native_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    item = delivery()
    path = Path(os.environ["LLM_BUS_CONFIG"])
    original_claim = RemoteStore.claim_wake

    def revoke_before_dispatch(remote: RemoteStore, remote_id: str) -> bool:
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'receive_messages = "inbox"', 'receive_messages = "deny"'
            ),
            encoding="utf-8",
        )
        return original_claim(remote, remote_id)

    def request(_self: object, _method: str, route: str, _data: object = None) -> object:
        return [item] if route.startswith("/v1/inbox?") and "after=0" in route else []

    def forbid_wake(*_args: object, **_kwargs: object) -> None:
        pytest.fail("revocation must prevent native wake")

    monkeypatch.setattr(RemoteStore, "claim_wake", revoke_before_dispatch)
    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    monkeypatch.setattr("llm_bus_remote.wake", forbid_wake)
    assert Bridge(store, config).sync()["errors"] == 0
    assert store.pending_count("claude:recipient") == 0
    assert len(store.history(folder=None)) == 1


@pytest.mark.parametrize(
    "invalid", ["broken = [", 'policy_version = 2\n[remote]\nurl = "https://["']
)
def test_invalid_remote_configuration_keeps_native_commands_and_hooks_working(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    invalid: str,
) -> None:
    store, _config = setup_machine(tmp_path, "b")
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(invalid, encoding="utf-8")
    folder = str(tmp_path / "repo")
    recipient = store.register("codex", "recipient", folder)
    store.send(SenderContext("claude", "sender", folder), recipient, "local")
    assert store.pending_count(recipient) == 1
    assert store.inbox(recipient)[0]["body"] == "local"
    monkeypatch.setenv("LLM_BUS_DB", str(store.path))
    monkeypatch.setenv("CODEX_THREAD_ID", "recipient")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.chdir(folder)
    assert main(["list"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "recipient",
                    "hook_event_name": "UserPromptSubmit",
                    "cwd": folder,
                }
            )
        ),
    )
    assert main(["hook", "codex"]) == 0
    assert "1 pending message(s)" in capsys.readouterr().out


def test_reports_bind_native_recipient_and_survive_lost_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, config = setup_machine(tmp_path, "b")
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8")
        + 'send_reports = ["received", "proceeding", "completed"]\n',
        encoding="utf-8",
    )
    store.register("claude", "recipient", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    item = delivery()
    admitted = remote.admit_message(item, config.repositories[0].scope)
    assert admitted is not None
    assert admitted[0] == "inbox"
    local_id = admitted[1]
    assert local_id is not None
    reports = Reports(store, config.connection)
    with pytest.raises(BusError, match="native recipient"):
        reports.enqueue(local_id, "codex:other", "received", None)
    with pytest.raises(BusError, match="imported message"):
        reports.enqueue(local_id + 1, "claude:recipient", "received", None)
    queued = reports.enqueue(local_id, "claude:recipient", "received", None)
    later = reports.enqueue(local_id, "claude:recipient", "proceeding", "started")
    assert later["expected_revision"] == 1
    submissions: list[object] = []

    def request(_self: object, _method: str, route: str, data: object = None) -> object:
        if route == "/v1/reports":
            submissions.append(data)
            if len(submissions) == 1:
                raise BusError("response lost")
            return {"message_id": item["id"], "revision": 1, "status": "received", "reason": None}
        return []

    def forbid_wake(*_args: object, **_kwargs: object) -> None:
        pytest.fail("reports must not wake agents")

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    monkeypatch.setattr("llm_bus_remote.wake", forbid_wake)
    assert Bridge(store, config).sync()["errors"] == 1
    assert Reports(Store(store.path), config.connection).pending() == [(local_id, queued)]
    assert Bridge(store, config).sync()["errors"] == 0
    assert submissions == [queued, queued]
    assert reports.pending() == [(local_id, later)]
    assert len(store.history(folder=None)) == 1
    assert (
        reports.enqueue(local_id, "claude:recipient", "completed", None)["expected_revision"] == 2
    )
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '"received", "proceeding", "completed"', '"received"'
        ),
        encoding="utf-8",
    )
    assert Bridge(store, config).sync()["errors"] == 0
    assert len(submissions) == 2
    with pytest.raises(BusError, match="disabled"):
        reports.enqueue(local_id, "claude:recipient", "completed", None)


def test_report_recovers_lost_receipt_after_inbox_permission_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, config = setup_machine(tmp_path, "b")
    path = Path(os.environ["LLM_BUS_CONFIG"])
    original = path.read_text(encoding="utf-8") + 'send_reports = ["received"]\n'
    path.write_text(original, encoding="utf-8")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    item = delivery()
    receipt_attempts = 0
    submitted: list[object] = []
    inbox_polls: list[str] = []

    def request(_self: object, _method: str, route: str, data: object = None) -> object:
        nonlocal receipt_attempts
        if route.startswith("/v1/inbox?"):
            inbox_polls.append(route)
            return [item] if "after=0" in route else []
        if route == "/v1/receipts":
            assert data == {"id": item["id"]}
            assert len(Store(store.path).history(folder=None)) == 1
            receipt_attempts += 1
            if receipt_attempts <= 2:
                raise BusError("receipt never reached relay")
            return {"id": item["id"], "status": "delivered_local"}
        if route == "/v1/reports":
            if receipt_attempts <= 2:
                raise BusError("Message not allowed")
            submitted.append(data)
            return {"message_id": item["id"], "revision": 1, "status": "received", "reason": None}
        return []

    def record_wake(*_args: object) -> None:
        pass

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    monkeypatch.setattr(Bridge, "_wake", record_wake)
    assert Bridge(store, config).sync()["errors"] == 1
    local_id = store.history(folder=None)[0]["id"]
    assert isinstance(local_id, int)
    path.write_text(
        original.replace('receive_messages = "inbox"', 'receive_messages = "deny"'),
        encoding="utf-8",
    )
    reports = Reports(Store(store.path), config.connection)
    queued = reports.enqueue(local_id, "claude:recipient", "received", None)

    assert Bridge(Store(store.path), config).sync()["errors"] == 1
    assert receipt_attempts == 2
    assert reports.pending() == [(local_id, queued)]
    assert submitted == []
    assert Bridge(Store(store.path), config).sync()["errors"] == 0
    assert reports.pending() == []
    assert submitted == [queued]
    assert len(inbox_polls) == 1
    assert store.pending_count("claude:recipient") == 0
    assert len(store.history(folder=None)) == 1


@pytest.mark.parametrize("change", ["scope", "connection", "capability", "peer"])
def test_report_permission_revoked_during_receipt_prevents_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    store, config = setup_machine(tmp_path, "b")
    path = Path(os.environ["LLM_BUS_CONFIG"])
    original = path.read_text(encoding="utf-8") + 'send_reports = ["received"]\n'
    path.write_text(original, encoding="utf-8")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    item = delivery()
    admitted = RemoteStore(store, config.connection).admit_message(
        item, config.repositories[0].scope
    )
    assert admitted is not None
    local_id = admitted[1]
    assert local_id is not None
    reports = Reports(store, config.connection)
    queued = reports.enqueue(local_id, "claude:recipient", "received", None)
    original = original.replace('receive_messages = "inbox"', 'receive_messages = "deny"')
    path.write_text(original, encoding="utf-8")
    receipts: list[object] = []
    submissions: list[object] = []

    def request(_self: object, _method: str, route: str, data: object = None) -> object:
        if route == "/v1/receipts":
            receipts.append(data)
            replacements = {
                "scope": original.replace(str(tmp_path / "repo"), str(tmp_path / "elsewhere")),
                "connection": original.replace("https://bus.test", "https://different.test"),
                "capability": original.replace('send_reports = ["received"]', "send_reports = []"),
                "peer": original.replace('machine_id = "a"', 'machine_id = "c"'),
            }
            path.write_text(replacements[change], encoding="utf-8")
            return {"id": item["id"], "status": "delivered_local"}
        if route == "/v1/reports":
            submissions.append(data)
            return {"message_id": item["id"], "revision": 1, "status": "received", "reason": None}
        return []

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    assert Bridge(store, config).sync()["errors"] == 0
    assert receipts == [{"id": item["id"]}]
    assert submissions == []
    assert reports.pending() == [(local_id, queued)]


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
    with store.transaction() as db:
        db.execute("UPDATE messages SET sent_at='2020-01-01T00:00:00.000Z'")
    polls: list[str] = []
    terminal = False

    def request(_self: object, _method: str, route: str, _data: object = None) -> object:
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

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    monkeypatch.setattr("llm_bus_remote.wake", forbid_wake)
    assert Bridge(store, config).sync()["errors"] == 0
    assert RemoteStore(store, config.connection).outgoing() == []
    assert Bridge(Store(store.path), config).sync()["errors"] == 0
    assert len(polls) == 2
    terminal = True
    assert Bridge(store, config).sync()["errors"] == 0
    assert Bridge(store, config).sync()["errors"] == 0
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

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    assert Bridge(store, config).sync()["errors"] == (1 if deadline in ("invalid", True) else 0)
    assert Reports(store, config.connection).pollable() == []


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


def test_cli_enqueues_new_report_while_older_upload_is_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, config = setup_machine(tmp_path, "b")
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8") + 'send_reports = ["received", "completed"]\n',
        encoding="utf-8",
    )
    store.register("claude", "recipient", str(tmp_path / "repo"))
    admitted = RemoteStore(store, config.connection).admit_message(
        delivery(), config.repositories[0].scope
    )
    assert admitted is not None
    assert admitted[0] == "inbox"
    local_id = admitted[1]
    assert local_id is not None
    monkeypatch.setenv("LLM_BUS_DB", str(store.path))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "recipient")
    monkeypatch.chdir(tmp_path / "repo")
    assert main(["report", str(local_id), "--status", "received"]) == 0
    capsys.readouterr()

    def request(_self: object, _method: str, route: str, data: object = None) -> object:
        if route == "/v1/reports":
            submission = cast("dict[str, object]", data)
            assert main(["report", str(local_id), "--status", "completed"]) == 0
            return {
                "message_id": submission["message_id"],
                "revision": 1,
                "status": "received",
                "reason": None,
            }
        return []

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    assert Bridge(store, config).sync()["errors"] == 0
    pending = Reports(Store(store.path), config.connection).pending()
    assert len(pending) == 1
    assert pending[0][1]["status"] == "completed"
    assert pending[0][1]["expected_revision"] == 1


@pytest.mark.parametrize("change", ["scope", "connection", "capability"])
def test_report_upload_reloads_all_binding_policy_at_final_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    store, config = setup_machine(tmp_path, "b")
    path = Path(os.environ["LLM_BUS_CONFIG"])
    original = path.read_text(encoding="utf-8") + 'send_reports = ["received"]\n'
    path.write_text(original, encoding="utf-8")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    admitted = RemoteStore(store, config.connection).admit_message(
        delivery(), config.repositories[0].scope
    )
    assert admitted is not None
    assert admitted[0] == "inbox"
    local_id = admitted[1]
    assert local_id is not None
    Reports(store, config.connection).enqueue(local_id, "claude:recipient", "received", None)
    imported = Reports.imported

    def mutate_after_binding(reports: Reports, message: int, native: str | None = None) -> Delivery:
        item = imported(reports, message, native)
        replacements = {
            "scope": original.replace(str(tmp_path / "repo"), str(tmp_path / "elsewhere")),
            "connection": original.replace("https://bus.test", "https://different.test"),
            "capability": original.replace('send_reports = ["received"]', "send_reports = []"),
        }
        path.write_text(replacements[change], encoding="utf-8")
        return item

    def request(_self: object, _method: str, route: str, _data: object = None) -> object:
        assert route != "/v1/reports"
        return []

    monkeypatch.setattr(Reports, "imported", mutate_after_binding)
    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    assert Bridge(store, config).sync()["errors"] == 0
    assert len(Reports(store, config.connection).pending()) == 1


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

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    assert Bridge(store, config).sync()["errors"] == 1
    assert reports.snapshot(envelope["id"]) is None


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
        "body": "hostile\x1b]8;;https://bad\x07\r\u202e",
        "sent_at": "metadata\x1b[2J\u009b\u202e",
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
        raise AssertionError(route)

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)

    def unexpected_wake(*_args: object, **_kwargs: object) -> None:
        pytest.fail("monitor wake")

    monkeypatch.setattr("llm_bus_remote.wake", unexpected_wake)
    bridge = Bridge(store, config)
    assert bridge.sync() == {"uploaded": 0, "imported": 0, "acknowledged": 0, "errors": 1}
    path.write_text(original)
    bridge = Bridge(Store(store.path), config)
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


def test_inbox_admission_remains_native_when_policy_changes_to_monitor(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    item = delivery()
    admitted = remote.admit_message(item, config.repositories[0].scope)
    assert admitted is not None
    assert admitted[0] == "inbox"
    local_id = admitted[1]
    path = tmp_path / "config.toml"
    path.write_text(
        path.read_text().replace('receive_messages = "inbox"', 'receive_messages = "monitor"')
    )
    assert remote.admit_message(item, config.repositories[0].scope) == ("inbox", local_id)
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

    def admit(_index: int) -> tuple[str, int | None] | None:
        return RemoteStore(Store(store.path), config.connection).admit_message(
            item, config.repositories[0].scope
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(admit, range(2))) == [("monitor", None), ("monitor", None)]
    assert len(RemoteStore.monitor_history(store, repository=None)) == 1
    assert remote.admit_message(delivery(), config.repositories[0].scope) == ("monitor", None)
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
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    RemoteStore(store, config.connection)
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

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    monkeypatch.setenv("LLM_BUS_DB", str(store.path))
    assert main(["bridge", "--once"]) == 1
    assert RemoteStore.monitor_history(store, repository=None) == []
    assert store.inbox("claude:recipient") == []
