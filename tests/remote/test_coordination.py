"""Remote transport never converts typed directions into ordinary messages."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
from os import environ
from pathlib import Path
from sqlite3 import IntegrityError
from typing import cast
from uuid import uuid4

import pytest

from llm_bus.coordination import cli
from llm_bus.coordination.authority import authorize
from llm_bus.coordination.delivery import outgoing as direction_outgoing
from llm_bus.coordination.models import Availability, Direction
from llm_bus.errors import BusError
from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.store import Store
from llm_bus.policy.config import Config
from llm_bus.remote.bridge import Bridge
from llm_bus.remote.http import RelayError
from llm_bus.remote.incoming import receive, receive_route
from llm_bus.remote.outgoing import enqueue_remote, permits_outgoing, upload
from llm_bus.remote.protocol import parse_delivery
from llm_bus.remote.store import RemoteStore

from .support import admit_delivery, assert_inbox_admission, delivery, deny_inbox, setup_machine


@pytest.mark.parametrize(
    ("route_class", "metadata"),
    [
        ("ordinary", "availability"),
        ("direction", "availability"),
        ("availability", "coordination"),
        ("availability", "missing"),
        ("availability", "mixed"),
    ],
)
def test_inbox_route_class_rejects_wrong_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route_class: str, metadata: str
) -> None:
    store, config = setup_machine(tmp_path, "b")
    item = delivery()
    if metadata in {"availability", "mixed"}:
        item["availability"] = asdict(
            Availability(1, str(uuid4()), str(uuid4()), str(uuid4()), "joined")
        )
    if metadata in {"coordination", "mixed"}:
        item["coordination"] = direction_fixture().wire()

    def request(*_args: object) -> object:
        return [item]

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    with pytest.raises(BusError, match="class conflicts"):
        receive_route(
            Bridge(store, config),
            config.repositories[0],
            "/test",
            coordination=route_class == "direction",
            availability=route_class == "availability",
        )
    assert store.inbox("claude:recipient") == []


@pytest.mark.parametrize("explicit", [True, False])
def test_remote_join_retry_and_assignment_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, explicit: bool
) -> None:
    # One scenario traces join, transport retry, target removal and authority revocation.
    # pylint: disable=too-many-locals
    store, config = setup_machine(tmp_path, "b")
    direction = direction_fixture()
    enroll(store, config, direction)
    if not explicit:
        with store.transaction() as db:
            db.execute("DELETE FROM coordination_grants")

    def no_native_wake(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Remote join attempted native wake")

    monkeypatch.setattr(cli, "dispatch_pending", no_native_wake)
    args: dict[str, object] = {"command": "join", "project": "demo"}
    first = cli.run(store, args, "claude:recipient", str(tmp_path / "repo"))
    remote = RemoteStore(store, config.connection)
    envelope = remote.outgoing()[0][0]
    event = Availability.parse(envelope.get("availability"))
    if explicit:
        assert event.grant_id == direction.grant_id
    direction = Direction(1, event.project_id, event.generation, event.grant_id, "assign")
    assert "coordination" not in envelope
    routes: list[str] = []
    supported = False

    def request(_self: object, _method: str, route: str, _data: object = None) -> object:
        routes.append(route)
        if route == "/v2/coordination/availability/capabilities":
            return {"availability_version": 1} if supported else {}
        return {"id": envelope["id"], "status": "accepted_remote"}

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    bridge = Bridge(store, config)
    with pytest.raises(RelayError) as unsupported:
        upload(bridge, envelope)
    assert unsupported.value.code == "unsupported_availability"
    assert remote.outgoing()[0][1] == "queued_local"
    assert routes == ["/v2/coordination/availability/capabilities"]
    supported = True
    assert upload(bridge, envelope)
    assert "/v2/coordination/availability/messages" in routes
    assert "/v1/messages" not in routes
    bridge.record("upload", envelope["id"], RelayError("unsupported_availability"))
    assert (
        remote.sync_snapshot("upload", envelope["id"])["error_code"] == "unsupported_availability"
    )
    remote.set_state(envelope["id"], "acknowledged")
    with store.transaction() as db:
        db.execute("DELETE FROM coordination_targets")
    second = cli.run(store, args, "claude:recipient", str(tmp_path / "repo"))
    assert (
        cast("dict[str, object]", second["availability"])["remote_id"]
        == cast("dict[str, object]", first["availability"])["remote_id"]
    )
    assert cast("dict[str, object]", second["availability"])["status"] == "acknowledged"
    assert permits_outgoing(bridge, envelope)
    with store.transaction() as db:
        db.execute("UPDATE coordination_grants SET revoked_at='revoked'")
        assert not authorize(
            db,
            sender="a/xyz/codex:sender",
            recipient="claude:recipient",
            scope=config.repositories[0].scope,
            direction=direction,
            now=datetime.now(UTC),
        ).verified
    assert not permits_outgoing(bridge, envelope)
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM remote_outbox").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM coordination_messages").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["outbox", "dedup", "policy", "missing"])
def test_remote_join_rollback(tmp_path: Path, failure: str) -> None:
    store, _config = setup_machine(tmp_path, "b")
    enroll(store, _config, direction_fixture())
    with store.transaction() as db:
        db.execute("DELETE FROM coordination_grants")
        db.execute("DELETE FROM coordination_enrollments")
        if failure in {"outbox", "dedup"}:
            table = "remote_outbox" if failure == "outbox" else "coordination_local_availability"
            db.execute(
                f"CREATE TRIGGER reject_notice BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        if failure == "missing":
            db.execute("DELETE FROM coordination_projects")
    if failure == "policy":
        path = Path(environ["LLM_BUS_CONFIG"])
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "send_messages = true", "send_messages = false"
            ),
            encoding="utf-8",
        )
    with pytest.raises((BusError, IntegrityError)):
        cli.run(
            store,
            {"command": "join", "project": "demo"},
            "claude:recipient",
            str(tmp_path / "repo"),
        )
    with store.transaction() as db:
        for table in (
            "coordination_grants",
            "coordination_session_consents",
            "coordination_enrollments",
            "messages",
            "remote_outbox",
            "coordination_local_availability",
            "wake_dispatch",
        ):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608


@pytest.mark.parametrize(
    "capability",
    [
        {},
        {"availability_version": True},
        {"availability_version": 2},
        {"availability_version": 1, "extra": 1},
    ],
)
def test_unsupported_availability_never_uploads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capability: object
) -> None:
    store, config = setup_machine(tmp_path, "a")
    routes: list[str] = []

    def request(_self: object, _method: str, route: str, _data: object = None) -> object:
        routes.append(route)
        return capability

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    item = delivery()
    item["availability"] = asdict(
        Availability(1, str(uuid4()), str(uuid4()), str(uuid4()), "joined")
    )
    with pytest.raises(RelayError) as error:
        upload(Bridge(store, config), item)
    assert error.value.code == "unsupported_availability"
    assert routes == ["/v2/coordination/availability/capabilities"]


def test_availability_scan_skips_malformed_and_admits_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, config = setup_machine(tmp_path, "b")
    valid = dict(delivery(), sequence=2, availability=availability_fixture(store, config))
    event = Availability.parse(valid["availability"])
    invalid = {**valid, "sequence": 1, "availability": {"version": 2}}
    receipts: list[object] = []

    def request(_self: object, _method: str, route: str, data: object = None) -> object:
        if route == "/v2/coordination/availability/capabilities":
            return {"availability_version": 1}
        if route == "/v2/coordination/capabilities":
            return {"version": 1}
        if route.startswith("/v2/coordination/availability/inbox?") and "after=0" in route:
            return [invalid, valid]
        if route == "/v1/receipts":
            receipts.append(data)
            return {"id": valid["id"], "status": "delivered_local"}
        return []

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    assert receive(Bridge(store, config), config.repositories[0]) == 1
    assert receipts == [{"id": valid["id"]}]
    with store.transaction() as db:
        assert db.execute("SELECT grant_id FROM coordination_targets").fetchone() == (
            event.grant_id,
        )
        assert db.execute("SELECT count(*) FROM coordination_messages").fetchone()[0] == 0
        assignment = direction_outgoing(
            db,
            sender="claude:recipient",
            scope=config.repositories[0].scope,
            label="demo",
            recipient=str(valid["sender"]),
            operation="assign",
        )
        assert assignment.grant_id == event.grant_id


def enroll(store: Store, config: Config, direction: Direction) -> None:
    """Two machines may import identifiers, but only each owner's grant counts."""
    scope = config.repositories[0].scope
    store.register("claude", "recipient", str(store.path.parent / "repo"))
    with store.transaction() as db:
        db.execute("UPDATE agents SET scope=? WHERE address='claude:recipient'", (scope,))
        db.execute(
            "INSERT INTO coordination_projects VALUES (?,?,?,?,?,?,?,?,?,NULL)",
            (
                scope,
                direction.project_id,
                "demo",
                direction.generation,
                "a/xyz/codex:sender",
                "review",
                "/unused",
                "memory-1",
                "[]",
            ),
        )
        db.execute(
            "INSERT INTO coordination_grants VALUES (?,?,?,?,?,?,?,?,NULL)",
            (
                scope,
                direction.project_id,
                "claude:recipient",
                direction.grant_id,
                direction.generation,
                "a/xyz/codex:sender",
                '["assign"]',
                None,
            ),
        )
        db.execute(
            "INSERT INTO coordination_enrollments VALUES (?,?,?,?,?)",
            (scope, direction.project_id, "claude:recipient", "worker", direction.generation),
        )


def direction_fixture() -> Direction:
    return Direction(1, str(uuid4()), str(uuid4()), str(uuid4()), "assign")


def availability_fixture(store: Store, config: Config) -> dict[str, object]:
    direction = direction_fixture()
    enroll(store, config, direction)
    with store.transaction() as db:
        db.execute("DELETE FROM coordination_grants")
        db.execute("UPDATE coordination_projects SET coordinator='claude:recipient'")
        db.execute("UPDATE coordination_enrollments SET role='coordinator'")
    return asdict(
        Availability(1, direction.project_id, direction.generation, direction.grant_id, "joined")
    )


def test_availability_monitor_has_no_target_or_wake(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    item = delivery()
    item["availability"] = availability_fixture(store, config)
    path = Path(environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'receive_messages = "inbox"', 'receive_messages = "monitor"'
        ),
        encoding="utf-8",
    )
    assert admit_delivery(store, config, item) == ("monitor", None, True)
    with store.transaction() as db:
        for table in (
            "messages",
            "coordination_targets",
            "coordination_availability_events",
            "wake_dispatch",
        ):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608


def test_availability_target_failure_rolls_back_everything(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    item = delivery()
    item["availability"] = availability_fixture(store, config)
    with store.transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_target BEFORE INSERT ON coordination_targets BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(IntegrityError, match="injected"):
        admit_delivery(store, config, item)
    with store.transaction() as db:
        for table in (
            "messages",
            "remote_imports",
            "coordination_availability_events",
            "wake_dispatch",
        ):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608


@pytest.mark.parametrize("change", ["extra", "version", "grant_id", "kind", "mixed"])
def test_availability_strict_metadata(tmp_path: Path, change: str) -> None:
    store, config = setup_machine(tmp_path, "b")
    item = delivery()
    item["availability"] = availability_fixture(store, config)
    if change == "mixed":
        item["coordination"] = direction_fixture().wire()
    else:
        item["availability"][change] = {
            "extra": 1,
            "version": True,
            "grant_id": "bad",
            "kind": "left",
        }[change]
    with pytest.raises(BusError):
        parse_delivery(item, config.repositories[0], "b")


def test_availability_dedup_ordering_and_no_authority(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    item = delivery()
    item["availability"] = availability_fixture(store, config)
    item["sent_at"] = "2026-01-02T00:00:00Z"
    assert_inbox_admission(admit_delivery(store, config, item))
    duplicate = item.copy()
    duplicate["id"] = str(uuid4())
    assert admit_delivery(store, config, duplicate) is None
    older = item.copy()
    older.update(
        id=str(uuid4()),
        sent_at="2026-01-01T00:00:00Z",
        availability={**item["availability"], "grant_id": str(uuid4())},
    )
    assert admit_delivery(store, config, older) is None
    newer = older.copy()
    newer.update(id=str(uuid4()), sent_at="2026-01-03T00:00:00Z")
    assert_inbox_admission(admit_delivery(store, config, newer))
    with store.transaction() as db:
        assert (
            db.execute("SELECT grant_id FROM coordination_targets").fetchone()[0]
            == newer["availability"]["grant_id"]
        )
        assert db.execute("SELECT count(*) FROM coordination_grants").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM coordination_messages").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2


@pytest.mark.parametrize(
    "change",
    ["project_id", "generation", "recipient", "binding", "enrollment", "deny", "timestamp", "id"],
)
def test_availability_rejection_leaves_no_residue(tmp_path: Path, change: str) -> None:
    store, config = setup_machine(tmp_path, "b")
    item = delivery()
    item["availability"] = availability_fixture(store, config)
    if change in {"project_id", "generation"}:
        item["availability"][change] = str(uuid4())
    elif change == "recipient":
        item["recipient"] = "b/xyz/claude:unknown"
    elif change in {"binding", "enrollment"}:
        with store.transaction() as db:
            db.execute(
                "DELETE FROM coordination_projects"
                if change == "binding"
                else "DELETE FROM coordination_enrollments"
            )
    elif change == "deny":
        deny_inbox()
    else:
        item["sent_at" if change == "timestamp" else "id"] = "invalid"
    if change in {"timestamp", "id"}:
        with pytest.raises(BusError):
            admit_delivery(store, config, item)
    else:
        assert admit_delivery(store, config, item) is None
    with store.transaction() as db:
        for table in (
            "messages",
            "remote_imports",
            "coordination_targets",
            "coordination_availability_events",
            "wake_dispatch",
        ):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608


@pytest.mark.parametrize("change", ["grant_id", "generation", "operation", "sender", "recipient"])
def test_receiver_owns_exact_authority(tmp_path: Path, change: str) -> None:
    store, config = setup_machine(tmp_path, "b")
    direction = direction_fixture()
    enroll(store, config, direction)
    item = delivery()
    item["coordination"] = direction.wire()
    if change in {"grant_id", "generation"}:
        item["coordination"][change] = str(uuid4())
    elif change == "operation":
        item["coordination"][change] = "cancel"
    else:
        item[change] = "a/xyz/codex:forged" if change == "sender" else "b/xyz/claude:other"
    assert admit_delivery(store, config, item) is None
    assert store.inbox("claude:recipient") == []


def test_grant_revocation_keeps_history_suppresses_pending(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    direction = direction_fixture()
    enroll(store, config, direction)
    item = delivery()
    item["coordination"] = direction.wire()
    key = assert_inbox_admission(admit_delivery(store, config, item))
    assert store.pending_allowed(key)
    assert store.inbox("claude:recipient")[0].get("coordination", {})["verified"] is True
    with store.transaction() as db:
        db.execute("UPDATE coordination_grants SET revoked_at='revoked'")
    assert not store.pending_allowed(key)
    assert store.inbox("claude:recipient") == []
    with store.transaction() as db:
        assert db.execute("SELECT body FROM messages WHERE id=?", (key,)).fetchone() == (
            item["body"],
        )


def test_policy_intersection_and_independent_store(tmp_path: Path) -> None:
    direction = direction_fixture()
    store, config = setup_machine(tmp_path, "b")
    enroll(store, config, direction)
    item = delivery()
    item["coordination"] = direction.wire()
    other = Store(tmp_path / "other.sqlite3")
    enroll(other, config, replace(direction, grant_id=str(uuid4())))
    assert admit_delivery(other, config, item) is None
    deny_inbox()
    assert admit_delivery(store, config, item) is None


@pytest.mark.parametrize("metadata", [{}, {"version": 2}, {"version": True}, {"extra": "assign"}])
def test_parser_rejects_unknown_metadata(tmp_path: Path, metadata: dict[str, object]) -> None:
    _, config = setup_machine(tmp_path, "b")
    item = dict(delivery(), coordination=metadata)
    with pytest.raises(BusError):
        parse_delivery(item, config.repositories[0], "b")


def test_old_relay_never_gets_direction_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, config = setup_machine(tmp_path, "a")
    routes: list[str] = []

    def request(_self: object, _method: str, route: str, _data: object = None) -> object:
        routes.append(route)
        raise RelayError("not_found", http_status=404)

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    item = delivery()
    item["coordination"] = direction_fixture().wire()
    with pytest.raises(RelayError) as error:
        upload(Bridge(store, config), item)
    assert error.value.code == "unsupported_coordination"
    assert routes == ["/v2/coordination/capabilities"]


def test_denied_versions_do_not_block_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, config = setup_machine(tmp_path, "b")
    direction = direction_fixture()
    enroll(store, config, direction)
    valid = dict(delivery(), sequence=2, coordination=direction.wire())
    invalid = {**valid, "sequence": 1, "coordination": {"version": 2}}
    receipt_payloads: list[object] = []

    def request(_self: object, _method: str, route: str, data: object = None) -> object:
        if route == "/v2/coordination/capabilities":
            return {"version": 1}
        if route.startswith("/v2/coordination/inbox?") and "after=0" in route:
            return [invalid, valid]
        if route == "/v1/receipts":
            receipt_payloads.append(data)
            return {}
        return []

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    assert receive(Bridge(store, config), config.repositories[0]) == 1
    assert receipt_payloads == [{"id": valid["id"]}]
    assert len(store.inbox("claude:recipient")) == 1


def test_monitor_direction_never_enters_pending(tmp_path: Path) -> None:
    store, config = setup_machine(tmp_path, "b")
    direction = direction_fixture()
    enroll(store, config, direction)
    path = Path(environ["LLM_BUS_CONFIG"])
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'receive_messages = "inbox"', 'receive_messages = "monitor"'
        ),
        encoding="utf-8",
    )
    item = delivery()
    item["coordination"] = direction.wire()
    assert admit_delivery(store, config, item) == ("monitor", None, True)
    assert store.inbox("claude:recipient") == []
    assert store.pending_count("claude:recipient") == 0
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM coordination_messages").fetchone() == (0,)


def test_sender_queue_rechecks_enrollment_and_uses_only_versioned_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, config = setup_machine(tmp_path, "a")
    direction = direction_fixture()
    enroll(store, config, direction)
    scope = config.repositories[0].scope
    with store.transaction() as db:
        db.execute("UPDATE coordination_projects SET coordinator='codex:sender'")
        db.execute(
            "INSERT INTO coordination_enrollments VALUES (?,?,?,?,?)",
            (scope, direction.project_id, "codex:sender", "coordinator", direction.generation),
        )
    sender = SenderContext("codex", "sender", str(tmp_path / "repo"))
    enqueue_remote(store, config, sender, "b/xyz/claude:recipient", "task", direction=direction)
    envelope = RemoteStore(store, config.connection).outgoing()[0][0]
    assert envelope.get("coordination") == direction.wire()
    routes: list[str] = []

    def request(_self: object, _method: str, route: str, _data: object = None) -> object:
        routes.append(route)
        if route == "/v2/coordination/capabilities":
            return {"version": 1}
        return {"id": envelope["id"], "status": "accepted_remote"}

    monkeypatch.setattr("llm_bus.remote.http.Relay.request", request)
    bridge = Bridge(store, config)
    assert permits_outgoing(bridge, envelope)
    assert upload(bridge, envelope)
    assert "/v2/coordination/messages" in routes
    assert "/v1/messages" not in routes
    with store.transaction() as db:
        db.execute("DELETE FROM coordination_enrollments WHERE address='codex:sender'")
    assert not permits_outgoing(bridge, envelope)
    with pytest.raises(BusError, match="coordinator enrollment"):
        enqueue_remote(store, config, sender, "b/xyz/claude:recipient", "task", direction=direction)
