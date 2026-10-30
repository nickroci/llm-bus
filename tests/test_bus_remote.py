"""Remote persistence and policy contracts; tests never wake real agent sessions."""

# pylint: disable=too-many-lines
# Remote transport scenarios share the isolated machine and relay fixtures.
from __future__ import annotations

import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from typing import cast, final, override
from uuid import uuid4

import pytest

import llm_bus_dispatch
import llm_bus_wake
from llm_bus import main
from llm_bus_policy import (
    Config,
    MessageIdentity,
    PeerPolicy,
    Repository,
    load_config,
    permits_inbox,
    read_token,
)
from llm_bus_remote import (
    Bridge,
    RelayError,
    enqueue_remote,
    parse_delivery,
    run_bridge,
)
from llm_bus_remote_store import Delivery, RemoteStore
from llm_bus_reports import Reports
from llm_bus_store import BusError, SenderContext, Store, routing_scope


@pytest.fixture(autouse=True)
def isolated_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BUS_CONFIG", str(tmp_path / "config.toml"))

    def requested(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr("llm_bus_dispatch.wake", requested)


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


def assert_config_scope(folder: Path) -> None:
    current = load_config()
    assert current is not None
    assert current.repositories[0].scope == routing_scope(str(folder))


@final
class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 23, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def test_retry_schedule_survives_reopen(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "a")
    remote = RemoteStore(store, config.connection)
    now = datetime(2026, 10, 20, tzinfo=UTC)
    remote.record_sync_result("upload", "one", now, error_code="unavailable")
    reopened = RemoteStore(Store(store.path), config.connection)
    assert not reopened.work_due("upload", "one", now + timedelta(seconds=4))
    assert reopened.work_due("upload", "one", now + timedelta(seconds=5))
    for seconds in (5, 10, 20, 40, 80, 160, 300, 300):
        reopened.record_sync_result("status", "two", now, error_code="unavailable")
        assert not reopened.work_due("status", "two", now + timedelta(seconds=seconds - 1))
        assert reopened.work_due("status", "two", now + timedelta(seconds=seconds))
        now += timedelta(seconds=seconds)
    reopened.record_sync_result("status", "two", now, error_code=None)
    assert reopened.sync_snapshot("status", "two")["attempts"] == 0
    assert not reopened.work_due("status", "two", now + timedelta(seconds=29))
    assert reopened.work_due("status", "two", now + timedelta(seconds=30))
    reopened.record_sync_result("upload", "one", now + timedelta(seconds=5), error_code=None)
    assert reopened.work_due("upload", "one", now + timedelta(seconds=5))


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


@pytest.mark.parametrize("status", [404, 405, 501, 401, 403, 429, 503])
def test_batch_fallback_only_for_unsupported_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code,clean-code-redundant-comment
    store, config = setup_machine(tmp_path, "a")
    bridge = Bridge(store, config)
    remote_id = str(uuid4())
    calls: list[str] = []

    def request(_self: object, _method: str, path: str, _data: object = None) -> object:
        calls.append(path)
        if path.endswith("/batch"):
            raise RelayError("rejected", http_status=status)
        return {"id": remote_id, "status": "accepted_remote"}

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    if status in {404, 405, 501}:
        values = bridge._read_batch("status", [remote_id])  # pyright: ignore[reportPrivateUsage]
        assert values[remote_id][0] == {"id": remote_id, "status": "accepted_remote"}
        assert len(calls) == 2
    else:
        with pytest.raises(RelayError):
            bridge._read_batch("status", [remote_id])  # pyright: ignore[reportPrivateUsage]
        assert len(calls) == 1


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "extra", "wrong_id", "bad_error", "bad_report"]
)
def test_batch_rejects_malformed_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code,clean-code-redundant-comment
    store, config = setup_machine(tmp_path, "a")
    bridge = Bridge(store, config)
    remote_id = str(uuid4())

    def request(*_args: object) -> object:
        valid: dict[str, object] = {
            "id": remote_id,
            "value": {"id": remote_id, "status": "accepted_remote"},
            "error": None,
        }
        if fault == "missing":
            return {"results": []}
        if fault == "duplicate":
            return {"results": [valid, valid]}
        if fault == "extra":
            return {"results": [valid], "extra": True}
        if fault == "wrong_id":
            return {"results": [{**valid, "id": str(uuid4())}]}
        if fault == "bad_error":
            return {"results": [{**valid, "value": None, "error": {"code": {"injected": "text"}}}]}
        return {"results": [{**valid, "value": {"message_id": remote_id, "status": "custom"}}]}

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    with pytest.raises(BusError):
        bridge._read_batch("report" if fault == "bad_report" else "status", [remote_id])  # pyright: ignore[reportPrivateUsage]


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


def test_batch_capability_negative_cache_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code,clean-code-redundant-comment
    store, config = setup_machine(tmp_path, "a")
    clock = FakeClock()
    bridge = Bridge(store, config, clock=clock)
    key = str(uuid4())
    batch_attempts: list[str] = []

    def request(_self: object, _method: str, path: str, _data: object = None) -> object:
        if path.endswith("/batch"):
            batch_attempts.append(path)
            raise RelayError("rejected", http_status=404)
        return {"id": key, "status": "accepted_remote"}

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    bridge._read_batch("status", [key])  # pyright: ignore[reportPrivateUsage]
    clock.advance(299)
    bridge._read_batch("status", [key])  # pyright: ignore[reportPrivateUsage]
    assert len(batch_attempts) == 1
    clock.advance(1)
    bridge._read_batch("status", [key])  # pyright: ignore[reportPrivateUsage]
    assert len(batch_attempts) == 2


@pytest.mark.parametrize("malformed", [False, True])
def test_batch_status_applies_authorized_neighbors_after_full_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, malformed: bool
) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code,clean-code-redundant-comment
    store, config = setup_machine(tmp_path, "a")
    clock = FakeClock()
    bridge = Bridge(store, config, clock=clock)
    for body in ("one", "two"):
        bridge.remote.enqueue(
            SenderContext("codex", "s", str(tmp_path / "repo")), "b/xyz/claude:r", "xyz", body
        )
    envelopes = [e for e, _state in bridge.remote.outgoing()]
    for envelope in envelopes:
        bridge.remote.set_state(envelope["id"], "accepted_remote")

    def request(_self: object, _method: str, _path: str, _data: object = None) -> object:
        return {
            "results": [
                {
                    "id": envelopes[0]["id"],
                    "value": {"id": envelopes[0]["id"], "status": "delivered_local"},
                    "error": None,
                },
                {
                    "id": envelopes[1]["id"],
                    "value": {"id": str(uuid4()), "status": "acknowledged"} if malformed else None,
                    "error": None if malformed else {"code": "not_allowed"},
                },
            ]
        }

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    assert bridge._poll_page("status", envelopes) == (2 if malformed else 1)  # pyright: ignore[reportPrivateUsage]
    by_id = {row["remote_id"]: row for row in bridge.remote.statuses()}
    assert by_id[envelopes[0]["id"]]["status"] == (
        "accepted_remote" if malformed else "delivered_local"
    )
    assert by_id[envelopes[1]["id"]]["status"] == "accepted_remote"


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

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    assert bridge._sync_outgoing() == (0, 0)  # pyright: ignore[reportPrivateUsage]
    assert calls == []
    forced = Bridge(Store(store.path), config, clock=clock, refresh_status=True)
    assert forced._sync_outgoing() == (1, 0)  # pyright: ignore[reportPrivateUsage]
    assert calls == ["/v1/status/batch"]
    assert forced.remote.statuses()[0]["status"] == "acknowledged"


def test_batch_pages_cover_large_outbox_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code,clean-code-redundant-comment
    store, config = setup_machine(tmp_path, "a")
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8") + 'receive_reports = ["received"]\n', encoding="utf-8"
    )
    clock = FakeClock()
    bridge = Bridge(store, config, clock=clock)
    reports = Reports(store, config.connection)
    for index in range(205):
        sent = bridge.remote.enqueue(
            SenderContext("codex", "s", str(tmp_path / "repo")), "b/xyz/claude:r", "xyz", str(index)
        )
        key = str(sent["remote_id"])
        bridge.remote.set_state(key, "accepted_remote")
        reports.deadline(key, "2099-01-01T00:00:00.000Z")
    batches: dict[str, list[int]] = {"status": [], "reports": []}

    def request(_self: object, _method: str, route: str, data: object = None) -> object:
        kind = "status" if route == "/v1/status/batch" else "reports"
        assert route in {"/v1/status/batch", "/v1/reports/batch"}
        assert isinstance(data, dict)
        ids = cast(
            "list[str]",
            cast("dict[str, object]", data)["ids" if kind == "status" else "message_ids"],
        )
        batches[kind].append(len(ids))
        return {
            "results": [
                {
                    "id": key,
                    "value": {"id": key, "status": "delivered_local"}
                    if kind == "status"
                    else {"message_id": key, "revision": 0, "status": None, "reason": None},
                    "error": None,
                }
                for key in ids
            ]
        }

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    assert bridge._sync_outgoing() == (205, 0)  # pyright: ignore[reportPrivateUsage]
    assert bridge._poll_reports() == 0  # pyright: ignore[reportPrivateUsage]
    assert batches == {"status": [100, 100, 5], "reports": [100, 100, 5]}


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


def test_remote_reply_binds_uuid_across_local_ids(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    native = SenderContext("claude", "recipient", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    store.register("codex", "other", native.project)
    store.send(native, "codex:other", "local")
    parent = remote.enqueue(native, "a/xyz/codex:sender", "xyz", "question")
    child = delivery()
    child["reply_to"] = cast("str", parent["remote_id"])
    admitted = remote.admit_message(child, config.repositories[0].scope)
    assert admitted is not None
    assert store.inbox("claude:recipient")[0]["reply_to"] == parent["id"]
    answer = enqueue_remote(
        store, config, native, "a/xyz/codex:sender", "answer", reply_to=admitted[1]
    )
    envelopes = {item["id"]: item for item, _ in remote.outgoing()}
    assert envelopes[cast("str", answer["remote_id"])].get("reply_to") == child["id"]
    child["reply_to"] = str(uuid4())
    with pytest.raises(BusError, match="reused"):
        remote.admit_message(child, config.repositories[0].scope)
    with pytest.raises(BusError):
        enqueue_remote(store, config, native, "a/xyz/codex:sender", "bad", reply_to=1)
    forged = delivery()
    forged["reply_to"] = cast("str", parent["remote_id"])
    forged["sender"] = "a/xyz/codex:other"
    with pytest.raises(BusError, match="participant"):
        remote.admit_message(forged, config.repositories[0].scope)
    collision = delivery(cast("str", parent["remote_id"]))
    with pytest.raises(BusError, match="reused"):
        remote.admit_message(collision, config.repositories[0].scope)


def test_remote_reply_waits_for_parent(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    parent = delivery()
    child = delivery()
    child["reply_to"] = parent["id"]
    assert remote.admit_message(child, config.repositories[0].scope) is None
    assert store.inbox("claude:recipient") == []
    assert remote.admit_message(parent, config.repositories[0].scope) is not None
    assert remote.admit_message(child, config.repositories[0].scope) is not None
    assert store.inbox("claude:recipient")[1]["reply_to"] == 1


def test_reply_waiting_parent_does_not_block_unrelated_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    child = delivery()
    child["reply_to"] = str(uuid4())
    other = delivery()
    other["sequence"] = 2
    receipts: list[object] = []

    def request(_self: object, _method: str, path: str, data: object = None) -> object:
        if path.startswith("/v1/inbox?"):
            return [child, other] if "after=0" in path else []
        if path == "/v1/receipts":
            receipts.append(data)
        return []

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    assert Bridge(store, config).sync()["imported"] == 1
    assert receipts == [{"id": other["id"]}]
    assert len(store.inbox("claude:recipient")) == 1


def test_reply_revocation_retains_history_without_pending_exposure(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    parent, child = delivery(), delivery()
    child["reply_to"] = parent["id"]
    remote.admit_message(parent, config.repositories[0].scope)
    remote.admit_message(child, config.repositories[0].scope)
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'receive_messages = "inbox"', 'receive_messages = "deny"'
        ),
        encoding="utf-8",
    )
    assert store.inbox("claude:recipient") == []
    assert store.pending_count("claude:recipient") == 0
    assert remote.admit_message(child, config.repositories[0].scope) is None
    assert len(store.history(folder=None, conversation=1)) == 2


def test_reply_wire_requires_canonical_uuid_and_accepts_legacy_root(tmp_path: Path) -> None:
    _store, config = setup_machine(tmp_path, "b")
    item = delivery()
    assert "reply_to" not in parse_delivery(item, config.repositories[0], "b")
    for parent in (None, 1, True, "invalid", "12345678-1234-1234-1234-123456789012"):
        with pytest.raises(BusError):
            parse_delivery({**item, "reply_to": parent}, config.repositories[0], "b")


def test_reply_monitor_parent_never_promotes_into_native_history(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    path = Path(os.environ["LLM_BUS_CONFIG"])
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original.replace('receive_messages = "inbox"', 'receive_messages = "monitor"'),
        encoding="utf-8",
    )
    remote = RemoteStore(store, config.connection)
    parent = delivery()
    assert remote.admit_message(parent, config.repositories[0].scope) == ("monitor", None, True)
    child = delivery()
    child["reply_to"] = parent["id"]
    assert remote.admit_message(child, config.repositories[0].scope) is None
    path.write_text(original, encoding="utf-8")
    assert remote.admit_message(parent, config.repositories[0].scope) == ("monitor", None, False)
    assert remote.admit_message(child, config.repositories[0].scope) is None
    assert store.history(folder=None) == []
    assert store.inbox("claude:recipient") == []
    assert len(RemoteStore.monitor_history(store, repository=None)) == 1


@pytest.mark.parametrize("status", [404, 405, 501])
def test_legacy_relay_holds_linked_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code
    store, config = setup_machine(tmp_path, "a")
    native = SenderContext("codex", "sender", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    parent = remote.enqueue(native, "b/xyz/claude:recipient", "xyz", "question")
    remote.set_state(cast("str", parent["remote_id"]), "acknowledged")
    child = enqueue_remote(
        store, config, native, "b/xyz/claude:recipient", "reply", reply_to=cast("int", parent["id"])
    )
    calls: list[str] = []

    def request(_self: object, _method: str, path: str, _data: object = None) -> object:
        calls.append(path)
        if path == "/v1/capabilities":
            raise RelayError("rejected", http_status=status)
        return {}

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    bridge = Bridge(store, config)
    assert bridge._sync_outgoing() == (0, 1)  # pyright: ignore[reportPrivateUsage]
    assert "/v1/messages" not in calls
    assert (
        remote.sync_snapshot("upload", cast("str", child["remote_id"]))["error_code"]
        == "unsupported_reply_link"
    )
    assert remote.outgoing()[0][1] == "queued_local"


@pytest.mark.parametrize(
    ("capabilities", "code"),
    [
        ({}, "unsupported_reply_link"),
        ({"reply_to": False}, "unsupported_reply_link"),
        ({"reply_to": "true"}, "protocol_error"),
        ({"reply_to": True, "unknown": True}, "protocol_error"),
        ({"reply_to": True, "batch_status": 1}, "protocol_error"),
    ],
)
def test_reply_capability_requires_explicit_boolean_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capabilities: object, code: str
) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code
    store, config = setup_machine(tmp_path, "a")
    native = SenderContext("codex", "sender", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    parent = remote.enqueue(native, "b/xyz/claude:recipient", "xyz", "question")
    remote.set_state(cast("str", parent["remote_id"]), "acknowledged")
    child = enqueue_remote(
        store, config, native, "b/xyz/claude:recipient", "reply", reply_to=cast("int", parent["id"])
    )

    def request(_self: object, _method: str, path: str, _data: object = None) -> object:
        assert path == "/v1/capabilities"
        return capabilities

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    assert Bridge(store, config)._sync_outgoing() == (0, 1)  # pyright: ignore[reportPrivateUsage]
    assert remote.sync_snapshot("upload", cast("str", child["remote_id"]))["error_code"] == code


def test_transport_progress_rejects_stale_response(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "a")
    remote = RemoteStore(store, config.connection)
    sent = remote.enqueue(
        SenderContext("codex", "sender", str(tmp_path / "repo")),
        "b/xyz/claude:recipient",
        "xyz",
        "hello",
    )
    remote_id = str(sent["remote_id"])
    remote.set_state(remote_id, "acknowledged")
    timestamp = store.history(folder=None)[0]["acknowledged_at"]
    remote.set_state(remote_id, "accepted_remote")
    assert remote.statuses()[0]["status"] == "acknowledged"
    remote.set_state(remote_id, "acknowledged")
    assert store.history(folder=None)[0]["acknowledged_at"] == timestamp
    with pytest.raises(BusError):
        remote.set_state(remote_id, "unknown")
    with pytest.raises(BusError):
        remote.set_state(str(uuid4()), "accepted_remote")


def test_message_status_distinguishes_transport_wake_ack_report(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "a")
    remote = RemoteStore(store, config.connection)
    sent = remote.enqueue(
        SenderContext("codex", "sender", str(tmp_path / "repo")),
        "b/xyz/claude:recipient",
        "xyz",
        "private body",
    )
    key = str(sent["remote_id"])
    now = datetime.now(UTC)
    remote.set_state(key, "acknowledged")
    remote.set_state(key, "accepted_remote")
    remote.record_sync_result("status", key, now, error_code=None)
    remote.record_sync_result("report_read", key, now, error_code="not_allowed")
    report = {"message_id": key, "revision": 1, "status": "proceeding", "reason": "started"}
    with store.transaction() as db:
        db.execute("INSERT INTO remote_reports VALUES (?,?)", (key, json.dumps(report)))
    Path(os.environ["LLM_BUS_CONFIG"]).unlink()
    before = store.path.read_bytes()
    result = store.message_status(cast("int", sent["id"]))
    assert result["transport"] == {
        "status": "acknowledged",
        "source": "relay_snapshot",
        "observed_at": now.isoformat(),
    }
    assert cast("dict[str, object]", result["wake"])["status"] is None
    assert result["acknowledged_at"] is not None
    assert result["work_report"] == report
    sync = cast("dict[str, object]", result["synchronization"])
    assert sync["stale"] is False
    operations = cast("list[dict[str, object]]", sync["operations"])
    assert {row["kind"]: row["error_code"] for row in operations} == {
        "status": None,
        "report_read": "not_allowed",
    }
    assert store.path.read_bytes() == before


def test_remote_status_preserves_existing_keys(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "a")
    remote = RemoteStore(store, config.connection)
    sent = remote.enqueue(
        SenderContext("codex", "sender", str(tmp_path / "repo")),
        "b/xyz/claude:recipient",
        "xyz",
        "hello",
    )
    row = Reports(store, config.connection).statuses()[0]
    assert {key: row[key] for key in ("id", "remote_id", "recipient", "status", "work_report")} == {
        "id": sent["id"],
        "remote_id": sent["remote_id"],
        "recipient": sent["recipient"],
        "status": "queued_local",
        "work_report": None,
    }
    assert row["wake"] == store.message_status(cast("int", sent["id"]))["wake"]
    assert "upload_synchronization" in row
    assert "report_synchronization" in row
    assert cast("dict[str, object]", row["synchronization"])["first_accepted_at"] is None


def test_message_status_import_is_local_admission_not_queued_report(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    envelope = delivery()
    envelope["sent_at"] = "2020-01-01T00:00:00.000Z"
    imported = RemoteStore(store, config.connection).admit_message(
        envelope, config.repositories[0].scope
    )
    assert imported is not None
    local_id = imported[1]
    assert local_id is not None
    with store.transaction() as db:
        db.execute(
            "INSERT INTO remote_report_submissions VALUES (?,0,?,0)",
            (envelope["id"], json.dumps({"status": "proceeding", "reason": "started"})),
        )
    Path(os.environ["LLM_BUS_CONFIG"]).unlink()
    result = store.message_status(local_id)
    transport = cast("dict[str, object]", result["transport"])
    assert transport["status"] == "delivered_local"
    assert transport["source"] == "local"
    assert transport["observed_at"] is None
    assert result["work_report"] is None
    assert store.inbox("claude:recipient") == []
    RemoteStore(store, config.connection).record_sync_result(
        "receipt", envelope["id"], datetime.now(UTC), error_code=None
    )
    result = store.message_status(local_id)
    assert cast("dict[str, object]", result["transport"])["observed_at"] is None
    assert cast("dict[str, object]", result["synchronization"])["last_success_at"] is not None


def test_message_status_tracks_real_report_submission_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each retained revision has an independent retry and acceptance lifecycle.
    # pylint: disable=too-many-locals
    store, config = setup_machine(tmp_path, "b")
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8") + 'send_reports = ["received", "completed"]\n',
        encoding="utf-8",
    )
    store.register("claude", "recipient", str(tmp_path / "repo"))
    item = delivery()
    admitted = RemoteStore(store, config.connection).admit_message(
        item, config.repositories[0].scope
    )
    assert admitted is not None
    local_id = admitted[1]
    assert local_id is not None
    reports = Reports(store, config.connection)
    reports.enqueue(local_id, "claude:recipient", "received", None)
    reports.enqueue(local_id, "claude:recipient", "completed", None)
    clock = FakeClock()
    bridge = Bridge(store, config, clock=clock)
    responses: list[object] = [
        RelayError("unavailable"),
        {"message_id": item["id"], "revision": 1, "status": "received", "reason": None},
        RelayError("not_allowed"),
        {"message_id": item["id"], "revision": 2, "status": "completed", "reason": None},
    ]

    def request(_self: object, _method: str, route: str, _data: object = None) -> object:
        if route == "/v1/reports":
            response = responses.pop(0)
            if isinstance(response, RelayError):
                raise response
            return response
        return []

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    assert bridge.sync()["errors"] == 1
    before = store.path.read_bytes()
    result = Store.read_only(store.path).message_status(local_id)
    assert store.path.read_bytes() == before
    assert result["work_report"] is None
    sync = cast("dict[str, object]", result["synchronization"])
    assert sync["error_code"] is None  # Receipt success is independent of report failure.
    operations = cast("list[dict[str, object]]", sync["operations"])
    failed = next(operation for operation in operations if operation["kind"] == "report_submit")
    assert (failed["expected_revision"], failed["error_code"], failed["last_success_at"]) == (
        0,
        "unavailable",
        None,
    )
    assert failed["next_attempt_at"] == (clock() + timedelta(seconds=5)).isoformat()
    assert failed["stale"] is True
    assert bridge.sync()["errors"] == 0  # Retry is not due; revision 1 remains blocked.
    clock.advance(5)
    assert bridge.sync()["errors"] == 0
    assert bridge.sync()["errors"] == 1
    sync = cast("dict[str, object]", store.message_status(local_id)["synchronization"])
    operations = cast("list[dict[str, object]]", sync["operations"])
    submissions = [operation for operation in operations if operation["kind"] == "report_submit"]
    assert [
        (operation["expected_revision"], operation["error_code"], operation["last_success_at"])
        for operation in submissions
    ] == [(0, None, clock().isoformat()), (1, "not_allowed", None)]
    assert submissions[1]["next_attempt_at"] == (clock() + timedelta(seconds=5)).isoformat()
    clock.advance(5)
    assert bridge.sync()["errors"] == 0
    assert reports.pending() == []
    assert responses == []
    assert store.message_status(local_id)["work_report"] is None


def test_message_status_legacy_errors_and_report_validation(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "a")
    remote = RemoteStore(store, config.connection)
    sent = remote.enqueue(
        SenderContext("codex", "sender", str(tmp_path / "repo")),
        "b/xyz/claude:recipient",
        "xyz",
        "hello",
    )
    key = str(sent["remote_id"])
    remote.record_sync_result(
        "status", key, datetime.now(UTC) - timedelta(seconds=31), error_code=None
    )
    assert (
        cast("dict[str, object]", store.message_status(cast("int", sent["id"]))["synchronization"])[
            "stale"
        ]
        is True
    )
    with store.transaction() as db:
        db.execute("UPDATE remote_sync_work SET error_code=?", ("\x1b secret credential.token",))
    result = store.message_status(cast("int", sent["id"]))
    assert cast("dict[str, object]", result["synchronization"])["error_code"] == "protocol_error"
    assert "secret" not in json.dumps(result)
    with store.transaction() as db:
        db.execute("INSERT INTO remote_reports VALUES (?,?)", (key, json.dumps({"text": "unsafe"})))
    with pytest.raises(BusError, match="Invalid work report"):
        store.message_status(cast("int", sent["id"]))


def test_transport_concurrent_writers_preserve_ack(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "a")
    first = RemoteStore(store, config.connection)
    second = RemoteStore(Store(store.path), config.connection)
    sent = first.enqueue(
        SenderContext("codex", "sender", str(tmp_path / "repo")),
        "b/xyz/claude:recipient",
        "xyz",
        "hello",
    )
    remote_id = str(sent["remote_id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(remote.set_state, remote_id, state)
            for remote, state in ((first, "acknowledged"), (second, "accepted_remote"))
        ]
        for future in futures:
            future.result()
    second.set_state(remote_id, "expired")
    assert first.statuses()[0]["status"] == "acknowledged"
    assert store.history(folder=None)[0]["acknowledged_at"] is not None


def test_import_lookup_uses_local_id_index(tmp_path: Path) -> None:
    store, _config = setup_machine(tmp_path, "a")
    with store.transaction() as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT envelope FROM remote_imports WHERE local_id=?", (1,)
        ).fetchall()
    assert "remote_imports_local_id" in str(plan)


def test_transport_progress_binds_response_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code
    # Exercise status response binding without unrelated bridge network operations.
    store, config = setup_machine(tmp_path, "a")
    bridge = Bridge(store, config)
    sent = bridge.remote.enqueue(
        SenderContext("codex", "sender", str(tmp_path / "repo")),
        "b/xyz/claude:recipient",
        "xyz",
        "hello",
    )
    envelope = bridge.remote.outgoing()[0][0]

    def request(_self: object, _method: str, _path: str, _body: object = None) -> object:
        return {"id": str(uuid4()), "status": "acknowledged"}

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    with pytest.raises(BusError, match="ID conflicts"):
        bridge._upload(envelope)  # pyright: ignore[reportPrivateUsage]
    assert bridge.remote.statuses()[0]["id"] == sent["id"]
    assert bridge.remote.statuses()[0]["status"] == "queued_local"
    assert store.history(folder=None)[0]["acknowledged_at"] is None


def test_pending_page_malformed_metadata_fails_closed(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    recipient = store.register("claude", "recipient", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    assert remote.admit_message(delivery(), config.repositories[0].scope) is not None
    with store.transaction() as db:
        db.execute("UPDATE remote_imports SET envelope='null'")
    assert store.inbox(recipient) == []
    assert store.pending_count(recipient) == 0
    assert not store.pending_allowed(1)
    assert len(store.history(folder=None)) == 1


def test_pending_page_skips_denied_prefix_and_replays(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    recipient = store.register("claude", "recipient", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    for _ in range(105):
        assert remote.admit_message(delivery(), config.repositories[0].scope) is not None
    local = store.send(SenderContext("codex", "local", str(tmp_path / "repo")), recipient, "local")
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'receive_messages = "inbox"', 'receive_messages = "deny"'
        ),
        encoding="utf-8",
    )
    assert [item["id"] for item in store.inbox(recipient, 1)] == [local["id"]]
    assert store.inbox(recipient, 1) == store.inbox(recipient, 1)
    assert store.pending_count(recipient, limit=1) == 1
    assert len(store.history(folder=None, limit=100)) == 100


@pytest.mark.parametrize("operation", ["inbox", "count"])
def test_pending_page_rechecks_recipient_scope_between_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    store, config = setup_machine(tmp_path, "b")
    recipient = store.register("claude", "recipient", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    for _ in range(2):
        assert remote.admit_message(delivery(), config.repositories[0].scope) is not None
    mover = Store(store.path)
    checked = 0

    def move_after_check(
        envelope: MessageIdentity, stored_scope: str, current_scope: str, connection: str
    ) -> bool:
        nonlocal checked
        allowed = permits_inbox(envelope, stored_scope, current_scope, connection)
        checked += 1
        if checked == 1:
            mover.register("claude", "recipient", str(tmp_path / "other"))
        return allowed

    monkeypatch.setattr("llm_bus_policy.permits_inbox", move_after_check)
    if operation == "inbox":
        assert [item["id"] for item in store.inbox(recipient)] == [1]
    else:
        assert store.pending_count(recipient, limit=2) == 1
    assert not store.pending_allowed(2)
    assert len(store.history(folder=None)) == 2


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
    assert reopened.admit_message(item, config.repositories[0].scope) == ("inbox", local_id, False)
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
    wake_calls: list[int] = []

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

    def record_wake(
        _recipient: str, message_id: int, _sender: str, **_kwargs: object
    ) -> dict[str, str]:
        wake_calls.append(message_id)
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    monkeypatch.setattr("llm_bus_dispatch.wake", record_wake)
    clock = FakeClock()
    assert Bridge(store, config, clock=clock).sync()["errors"] == 1
    assert len(store.inbox("claude:recipient")) == 1
    clock.advance(5)
    Bridge(Store(store.path), config, clock=clock).sync()
    assert wake_calls == [store.inbox("claude:recipient")[0]["id"]]
    assert len(store.inbox("claude:recipient")) == 1
    store.acknowledge("claude:recipient", [store.inbox("claude:recipient")[0]["id"]])
    Bridge(Store(store.path), config, clock=clock).sync()
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
    # Explicit fake time keeps HTTP retry behavior deterministic.
    # pylint: disable=too-many-locals
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
    clock = FakeClock()

    def next_pass(_seconds: float) -> None:
        clock.advance(_seconds)
        states.append(RemoteStore(Store(store.path), config.connection).outgoing()[0][1])
        if len(states) == 2:
            raise KeyboardInterrupt

    def forbid_wake(*_args: object, **_kwargs: object) -> None:
        pytest.fail("HTTP retries must not wake a real agent")

    monkeypatch.setattr("llm_bus_remote.load_config", lambda: config)
    # Subprocess timeout waits need the real clock.
    monkeypatch.setattr("llm_bus_remote.time", SimpleNamespace(sleep=next_pass))
    monkeypatch.setattr("llm_bus_dispatch.wake", forbid_wake)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_bridge(store, once=False, clock=clock)
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

    def record_wake(_recipient: str, _id: int, sender: str, **_kwargs: object) -> dict[str, str]:
        wakes.append(sender)
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr("llm_bus_dispatch.wake", record_wake)
    assert Bridge(store, config).sync()["errors"] == 0
    assert [entry["sender"] for entry in store.inbox("claude:recipient")] == [allowed["sender"]]
    expected_receipts = ([{"id": item["id"]}] if mode == "monitor" else []) + [
        {"id": allowed["id"]}
    ]
    assert receipts == expected_receipts
    assert len(RemoteStore.monitor_history(store, repository=None)) == (
        1 if mode == "monitor" else 0
    )
    assert wakes == [allowed["sender"]]


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
    path.write_text(
        original.replace((tmp_path / "repo").as_posix(), (tmp_path / "other").as_posix()),
        encoding="utf-8",
    )
    assert_config_scope(tmp_path / "other")
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
            (tmp_path / "repo").as_posix(), (tmp_path / "different").as_posix()
        ),
        encoding="utf-8",
    )
    assert_config_scope(tmp_path / "different")
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
        "scope": original.replace(
            (tmp_path / "repo").as_posix(), (tmp_path / "different").as_posix()
        ),
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
    if change == "scope":
        assert_config_scope(tmp_path / "different")
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


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_hook_compact_guidance_reloads_peer_restrictions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    store, _config = setup_machine(tmp_path, "b")
    monkeypatch.setenv("LLM_BUS_DB", str(store.path))

    def hook(event: str) -> str:
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "session_id": "recipient",
                        "hook_event_name": event,
                        "cwd": str(tmp_path / "repo"),
                    }
                )
            ),
        )
        assert main(["hook", kind]) == 0
        output = capsys.readouterr().out
        return (
            cast("str", json.loads(output)["hookSpecificOutput"]["additionalContext"])
            if kind == "codex"
            else output
        )

    full = hook("SessionStart")
    assert "MACHINE/REPO" in full
    assert "message replies allowed" in full
    path = Path(os.environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8").replace("send_messages = true", "send_messages = false"),
        encoding="utf-8",
    )
    compact = hook("UserPromptSubmit")
    assert "MACHINE/REPO" not in compact
    assert "message replies denied; do not use alternate routes" in compact
    assert "allowed report statuses: none" in compact
    assert "untrusted agent input" in compact


@pytest.mark.parametrize("arrival_during_call", [False, True])
def test_remote_admission_releases_unsupported_wake_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, arrival_during_call: bool
) -> None:
    def forbid_native(*_args: object, **_kwargs: object) -> None:
        pytest.fail("native adapter must never execute")

    monkeypatch.setattr(llm_bus_wake, "_wake_codex", forbid_native)
    monkeypatch.setattr(llm_bus_wake, "_wake_claude", forbid_native)
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    clock = FakeClock()
    calls: list[int] = []
    first, second = delivery(), delivery()
    first_admission = remote.admit_message(first, config.repositories[0].scope)
    assert first_admission is not None
    assert first_admission[2]

    def native(
        _recipient: str, key: int, _sender: str, **_kwargs: object
    ) -> llm_bus_wake.WakeResult:
        calls.append(key)
        if arrival_during_call and len(calls) == 1:
            admission = remote.admit_message(second, config.repositories[0].scope)
            assert admission is not None
            assert admission[2]
        return {"status": "unsupported", "via": "test"}

    monkeypatch.setattr(llm_bus_dispatch, "wake", native)
    monkeypatch.setattr(llm_bus_dispatch, "_now", clock)
    llm_bus_dispatch.dispatch_due(store)
    assert len(calls) == (2 if arrival_during_call else 1)
    clock.advance(600)
    # Relay replay and elapsed time alone must not retry an unsupported endpoint.
    assert remote.admit_message(first, config.repositories[0].scope) == (
        *first_admission[:2],
        False,
    )
    llm_bus_dispatch.dispatch_due(Store(store.path))
    assert len(calls) == (2 if arrival_during_call else 1)
    if not arrival_during_call:
        admission = remote.admit_message(second, config.repositories[0].scope)
        assert admission is not None
        assert admission[2]
        llm_bus_dispatch.dispatch_due(Store(store.path))
    assert len(calls) == 2
    clock.advance(600)
    llm_bus_dispatch.dispatch_due(Store(store.path))
    assert len(calls) == 2
    with store.transaction() as db:
        assert db.execute(
            "SELECT requested_generation,dispatched_generation,token FROM wake_dispatch"
        ).fetchone() == (2, 2, None)
        assert db.execute("SELECT COUNT(*) FROM wake_coverage").fetchone() == (2,)
    assert store.pending_count("claude:recipient") == 2


def test_wake_coverage_excludes_revoked_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code
    store, config = setup_machine(tmp_path, "b")
    native = store.register("claude", "recipient", str(tmp_path / "repo"))
    item = delivery()
    admitted = RemoteStore(store, config.connection).admit_message(
        item, config.repositories[0].scope
    )
    assert admitted is not None
    local = store.send(
        SenderContext("codex", "local", str(tmp_path / "repo")), native, "local body"
    )
    snapshot = llm_bus_dispatch._snapshot  # pyright: ignore[reportPrivateUsage]
    path = Path(os.environ["LLM_BUS_CONFIG"])

    def revoke_after_snapshot(
        store: Store, recipient: str, claim: llm_bus_dispatch.Claim
    ) -> llm_bus_dispatch.Batch:
        batch = snapshot(store, recipient, claim)
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'receive_messages = "inbox"', 'receive_messages = "deny"'
            ),
            encoding="utf-8",
        )
        return batch

    calls: list[int] = []

    def native_wake(_recipient: str, key: int, _sender: str, **_kwargs: object) -> dict[str, str]:
        calls.append(key)
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr(llm_bus_dispatch, "_snapshot", revoke_after_snapshot)
    monkeypatch.setattr(llm_bus_dispatch, "wake", native_wake)
    llm_bus_dispatch.dispatch_pending(store, native)
    assert calls == [local["id"]]
    with store.transaction() as db:
        assert db.execute("SELECT message_id FROM wake_coverage").fetchall() == [(local["id"],)]
    assert len(store.history(folder=None)) == 2


def test_bridge_recovers_receipted_legacy_wake_claim_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    item = delivery()
    admitted = RemoteStore(store, config.connection).admit_message(
        item, config.repositories[0].scope
    )
    assert admitted is not None
    with store.transaction() as db:
        db.execute("UPDATE remote_imports SET wake_claimed=1")
        db.execute("DROP TABLE wake_coverage")
        db.execute("DROP TABLE wake_dispatch")
        db.execute("DROP TABLE hook_guidance")
        db.execute("PRAGMA user_version=2")
    store = Store(store.path)

    def request(*_args: object, **_kwargs: object) -> list[object]:
        return []

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    assert Bridge(store, config).sync()["errors"] == 0
    with store.transaction() as db:
        assert db.execute("SELECT status FROM wake_coverage").fetchone() == ("requested",)
        assert db.execute("SELECT wake_claimed FROM remote_imports").fetchone() == (1,)


def test_policy_reload_after_wake_claim_prevents_native_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code
    store, config = setup_machine(tmp_path, "b")
    store.register("claude", "recipient", str(tmp_path / "repo"))
    item = delivery()
    path = Path(os.environ["LLM_BUS_CONFIG"])
    original_claim = llm_bus_dispatch._claim  # pyright: ignore[reportPrivateUsage]

    def revoke_before_dispatch(
        store: Store, recipient: str, *, trigger_id: int | None
    ) -> llm_bus_dispatch.Claim | None:
        claim = original_claim(store, recipient, trigger_id=trigger_id)
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'receive_messages = "inbox"', 'receive_messages = "deny"'
            ),
            encoding="utf-8",
        )
        return claim

    def request(_self: object, _method: str, route: str, _data: object = None) -> object:
        return [item] if route.startswith("/v1/inbox?") and "after=0" in route else []

    def forbid_wake(*_args: object, **_kwargs: object) -> None:
        pytest.fail("revocation must prevent native wake")

    monkeypatch.setattr("llm_bus_dispatch._claim", revoke_before_dispatch)
    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    monkeypatch.setattr("llm_bus_dispatch.wake", forbid_wake)
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
    # The retry clock is independent of report and recipient state.
    # pylint: disable=too-many-locals
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
    monkeypatch.setattr("llm_bus_dispatch.wake", forbid_wake)
    clock = FakeClock()
    # Only report retry is under test; the imported inbox request predates this bridge pass.
    with store.transaction() as db:
        db.execute("UPDATE wake_dispatch SET next_attempt_at='9999-01-01T00:00:00+00:00'")
    assert Bridge(store, config, clock=clock).sync()["errors"] == 1
    assert Reports(Store(store.path), config.connection).pending() == [(local_id, queued)]
    clock.advance(5)
    assert Bridge(store, config, clock=clock).sync()["errors"] == 0
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
    # Separate clock, receipt, report, and policy observations exercise recovery.
    # pylint: disable=too-many-locals
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

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    clock = FakeClock()
    assert Bridge(store, config, clock=clock).sync()["errors"] == 1
    local_id = store.history(folder=None)[0]["id"]
    assert isinstance(local_id, int)
    path.write_text(
        original.replace('receive_messages = "inbox"', 'receive_messages = "deny"'),
        encoding="utf-8",
    )
    reports = Reports(Store(store.path), config.connection)
    queued = reports.enqueue(local_id, "claude:recipient", "received", None)

    clock.advance(5)
    assert Bridge(Store(store.path), config, clock=clock).sync()["errors"] == 1
    assert receipt_attempts == 2
    assert reports.pending() == [(local_id, queued)]
    assert submitted == []
    clock.advance(10)
    assert Bridge(Store(store.path), config, clock=clock).sync()["errors"] == 0
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
                "scope": original.replace(
                    (tmp_path / "repo").as_posix(), (tmp_path / "elsewhere").as_posix()
                ),
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
    if change == "scope":
        assert_config_scope(tmp_path / "elsewhere")
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

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
    monkeypatch.setattr("llm_bus_dispatch.wake", forbid_wake)
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

    monkeypatch.setattr("llm_bus_remote.Relay.request", request)
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
            "scope": original.replace(
                (tmp_path / "repo").as_posix(), (tmp_path / "elsewhere").as_posix()
            ),
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
    if change == "scope":
        assert_config_scope(tmp_path / "elsewhere")
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

    monkeypatch.setattr("llm_bus_dispatch.wake", unexpected_wake)
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
