"""Recipients record fixed-code work state for local directions without waking anyone."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, cast

import pytest

from llm_bus.cli.commands import registered_command
from llm_bus.cli.guidance import guidance
from llm_bus.cli.parser import build_parser
from llm_bus.coordination import work_state
from llm_bus.coordination.cli import run
from llm_bus.coordination.delivery import outgoing
from llm_bus.diagnostics.messages import message_status
from llm_bus.errors import BusError
from llm_bus.mailbox.schema import SCHEMA_VERSION
from llm_bus.mailbox.store import Store
from llm_bus.policy.config import Config, PeerPolicy, Repository
from llm_bus.remote.outgoing import enqueue_remote
from llm_bus.reports.lifecycle import TRANSITION_CONFLICT
from llm_bus.routing import routing_scope

if TYPE_CHECKING:
    from pathlib import Path

    from llm_bus.coordination.models import Direction
    from llm_bus.mailbox.models import SenderContext

WORKER = "claude:worker"
OWNER = "codex:owner"
MILLISECOND_Z = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z"


def report(
    store: Store,
    message_id: int,
    status: str,
    reason: str | None = None,
    *,
    address: str = WORKER,
) -> object:
    args: dict[str, object] = {"message_id": message_id, "status": status, "reason": reason}
    return registered_command(store, "report", args, address, "")


def rows(store: Store) -> int:
    with store.transaction() as db:
        return cast(
            "tuple[int]", db.execute("SELECT count(*) FROM coordination_work_reports").fetchone()
        )[0]


def work_report(store: Store, message_id: int) -> object:
    return message_status(store.path, message_id)["work_report"]


def direction_id(store: Store, owner: SenderContext, direction: Direction) -> int:
    sent = store.send(owner, WORKER, "Check task", direction=direction)
    return sent["id"]


def test_recipient_records_and_status_shows(
    enrolled: tuple[Store, Direction, SenderContext],
) -> None:
    store, direction, owner = enrolled
    message_id = direction_id(store, owner, direction)
    assert work_report(store, message_id) is None
    store.acknowledge(WORKER, [message_id])
    report(store, message_id, "received")
    report(store, message_id, "proceeding", "started")
    repeated = report(store, message_id, "proceeding", "started")
    shown = cast("dict[str, object]", work_report(store, message_id))
    assert repeated == shown
    assert {k: v for k, v in shown.items() if k != "reported_at"} == {
        "message_id": message_id,
        "revision": 2,
        "status": "proceeding",
        "reason": "started",
    }
    assert re.fullmatch(MILLISECOND_Z, cast("str", shown["reported_at"]))


def test_report_refusals(enrolled: tuple[Store, Direction, SenderContext]) -> None:
    store, direction, owner = enrolled
    message_id = direction_id(store, owner, direction)
    ordinary = store.send(owner, WORKER, "plain")["id"]
    store.acknowledge(WORKER, [ordinary])
    with pytest.raises(BusError, match="Acknowledge the direction"):
        report(store, message_id, "received")
    store.acknowledge(WORKER, [message_id])
    refused = (
        (message_id, "received", None, OWNER, "requires a received local"),
        (999, "received", None, WORKER, "requires a received local"),
        (ordinary, "received", None, WORKER, "requires a received local"),
        (message_id, "bogus", None, WORKER, "Invalid built-in"),
        (message_id, "proceeding", "nonsense", WORKER, "Invalid built-in"),
    )
    for target, status, reason, address, message in refused:
        with pytest.raises(BusError, match=message):
            report(store, target, status, reason, address=address)
    assert rows(store) == 0
    report(store, message_id, "proceeding", "started")
    with pytest.raises(BusError, match=TRANSITION_CONFLICT):
        report(store, message_id, "received")
    report(store, message_id, "completed")
    with pytest.raises(BusError, match=TRANSITION_CONFLICT):
        report(store, message_id, "proceeding", "started")
    assert work_report(store, message_id) is not None
    assert cast("dict[str, object]", work_report(store, message_id))["revision"] == 2


def counts(store: Store, folder: str) -> tuple[object, ...]:
    with store.transaction() as db:
        tables = [
            cast("tuple[int]", db.execute(f"SELECT count(*) FROM {name}").fetchone())[0]  # noqa: S608
            for name in ("messages", "wake_dispatch")
        ]
    return (*tables, store.pending_count(OWNER), guidance(store, OWNER, folder, full=False))


def test_report_never_wakes_or_prompts(
    enrolled: tuple[Store, Direction, SenderContext],
) -> None:
    store, direction, owner = enrolled
    message_id = direction_id(store, owner, direction)
    store.acknowledge(WORKER, [message_id])
    before = counts(store, owner.project)
    assert '"state": "active"' in cast("str", before[-1])
    report(store, message_id, "received")
    report(store, message_id, "failed", "execution_failed")
    assert counts(store, owner.project) == before


def test_outgoing_remote_direction_is_refused(
    enrolled: tuple[Store, Direction, SenderContext],
) -> None:
    store, direction, owner = enrolled
    message_id = direction_id(store, owner, direction)
    store.acknowledge(WORKER, [message_id])
    with store.transaction() as db:
        db.execute(
            "INSERT INTO remote_outbox (id, local_id, envelope, state) VALUES (?,?,?,?)",
            ("remote-1", message_id, "{}", "pending"),
        )
    with pytest.raises(BusError, match="Report requires a received local"):
        report(store, message_id, "received")
    assert rows(store) == 0


def test_upgrade_from_14(enrolled: tuple[Store, Direction, SenderContext], tmp_path: Path) -> None:
    store, direction, owner = enrolled
    message_id = direction_id(store, owner, direction)
    with store.transaction() as db:
        db.execute("DROP TABLE coordination_work_reports")
        db.execute("PRAGMA user_version = 14")
    upgraded = Store(tmp_path / "bus.db")
    with upgraded.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 18
        assert db.execute("SELECT count(*) FROM coordination_work_reports").fetchone()[0] == 0
        assert db.execute("SELECT id FROM messages WHERE id=?", (message_id,)).fetchone()


def test_reupgrade_after_rollback(
    enrolled: tuple[Store, Direction, SenderContext], tmp_path: Path
) -> None:
    store, direction, owner = enrolled
    message_id = direction_id(store, owner, direction)
    store.acknowledge(WORKER, [message_id])
    report(store, message_id, "received")
    with store.transaction() as db:
        db.execute("PRAGMA user_version = 14")
    upgraded = Store(tmp_path / "bus.db")
    with upgraded.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert rows(upgraded) == 1


OTHER = "claude:other"
RELAY_ID = "11111111-2222-4333-8444-555555555555"


def cli(store: Store, cwd: str, *argv: str, address: str = OWNER) -> dict[str, object]:
    return run(store, vars(build_parser().parse_args(argv)), address, cwd)


def add_worker(store: Store, cwd: str) -> None:
    store.register("claude", "other", cwd)
    cli(store, cwd, "coordination", "grant", "demo", "--recipient", OTHER, "--until-revoked")
    cli(store, cwd, "join", "demo", address=OTHER)
    with store.transaction() as db:
        grant = cast(
            "tuple[str]",
            db.execute(
                "SELECT grant_id FROM coordination_grants WHERE recipient=?", (OTHER,)
            ).fetchone(),
        )[0]
    cli(store, cwd, "coordination", "target", "demo", "--recipient", OTHER, "--grant-id", grant)


def send(
    store: Store, owner: SenderContext, operation: str, recipient: str = WORKER, *, ack: bool = True
) -> int:
    with store.transaction() as db:
        direction = outgoing(
            db,
            sender=OWNER,
            scope=routing_scope(owner.project),
            label="demo",
            recipient=recipient,
            operation=operation,
        )
    message_id = store.send(owner, recipient, f"{operation} task", direction=direction)["id"]
    if ack:
        store.acknowledge(recipient, [message_id])
    return message_id


def listed(
    store: Store, owner: SenderContext, *flags: str, address: str = OWNER
) -> list[dict[str, object]]:
    result = cli(
        store, owner.project, "coordination", "direction-list", "demo", *flags, address=address
    )
    return cast("list[dict[str, object]]", result["directions"])


def shown_ids(store: Store, owner: SenderContext, address: str = OWNER) -> list[int]:
    result = cli(store, owner.project, "coordination", "show", "demo", address=address)
    return [
        cast("int", d["message_id"])
        for d in cast("list[dict[str, object]]", result.get("directions", []))
    ]


def ids(items: list[dict[str, object]]) -> list[int]:
    return [cast("int", item["message_id"]) for item in items]


def test_listing_visibility(enrolled: tuple[Store, Direction, SenderContext]) -> None:
    store, _direction, owner = enrolled
    add_worker(store, owner.project)
    mine = send(store, owner, "assign")
    theirs = send(store, owner, "assign", OTHER)
    assert ids(listed(store, owner)) == [theirs, mine]
    assert ids(listed(store, owner, address=WORKER)) == [mine]
    assert ids(listed(store, owner, address=OTHER)) == [theirs]
    assert shown_ids(store, owner, WORKER) == [mine]
    # An unenrolled address learns nothing about the project's directions.
    assert ids(listed(store, owner, address="claude:stranger")) == []


def test_enrolled_session_without_directions_sees_none(
    enrolled: tuple[Store, Direction, SenderContext],
) -> None:
    store, _direction, owner = enrolled
    add_worker(store, owner.project)
    send(store, owner, "assign")
    assert listed(store, owner, address=OTHER) == []
    assert shown_ids(store, owner, OTHER) == []


def test_item_shape(enrolled: tuple[Store, Direction, SenderContext]) -> None:
    store, _direction, owner = enrolled
    message_id = send(store, owner, "assign")
    (item,) = listed(store, owner)
    assert set(item) == {
        "message_id",
        "recipient",
        "operation",
        "sent_at",
        "acknowledged_at",
        "remote_id",
        "work_report",
        "state_changed_at",
    }
    assert item["message_id"] == message_id
    assert re.fullmatch(MILLISECOND_Z, cast("str", item["sent_at"]))
    assert item["remote_id"] is None


def test_earlier_generation_not_listed(enrolled: tuple[Store, Direction, SenderContext]) -> None:
    store, _direction, owner = enrolled
    old = send(store, owner, "assign")
    current = send(store, owner, "assign")
    with store.transaction() as db:
        db.execute(
            "UPDATE coordination_messages SET metadata_json="
            "json_set(metadata_json,'$.generation',-1) WHERE message_id=?",
            (old,),
        )
    for view in (OWNER, WORKER):
        assert ids(listed(store, owner, address=view)) == [current]


def test_open_and_show_filters(enrolled: tuple[Store, Direction, SenderContext]) -> None:
    store, _direction, owner = enrolled
    plain = send(store, owner, "assign")
    started = send(store, owner, "assign")
    report(store, started, "proceeding", "started")
    done = send(store, owner, "assign")
    report(store, done, "completed")
    reprioritized = send(store, owner, "reprioritize")
    clarify = send(store, owner, "clarify")
    pending = send(store, owner, "assign", ack=False)
    everything = {item["message_id"]: item for item in listed(store, owner)}
    assert set(everything) == {plain, started, done, reprioritized, clarify, pending}
    assert ids(listed(store, owner, "--open")) == [reprioritized, started, plain]
    assert shown_ids(store, owner) == [pending, reprioritized, started, plain]
    with store.transaction() as db:
        acked = cast(
            "tuple[str]",
            db.execute("SELECT acknowledged_at FROM messages WHERE id=?", (plain,)).fetchone(),
        )[0]
    assert everything[plain]["state_changed_at"] == acked
    assert everything[plain]["work_report"] is None
    reported = cast("dict[str, object]", everything[started]["work_report"])
    assert everything[started]["state_changed_at"] == reported["reported_at"]
    assert everything[pending]["state_changed_at"] is None
    mutation = cli(
        store,
        owner.project,
        "coordination",
        "instructions",
        "demo",
        "--audience",
        "worker",
        "--text",
        "x",
    )
    assert "directions" not in mutation


def test_cancel_closes_earlier_work_directions(
    enrolled: tuple[Store, Direction, SenderContext],
) -> None:
    store, _direction, owner = enrolled
    add_worker(store, owner.project)
    first = send(store, owner, "assign")
    report(store, first, "proceeding", "started")
    second = send(store, owner, "review")
    third = send(store, owner, "reprioritize")
    cancel = send(store, owner, "cancel", ack=False)
    for view in (OWNER, WORKER):
        assert ids(listed(store, owner, "--open", address=view)) == [third, second, first]
        assert cancel in shown_ids(store, owner, view)
    store.acknowledge(WORKER, [cancel])
    before = listed(store, owner)
    for view in (OWNER, WORKER):
        assert listed(store, owner, "--open", address=view) == []
        assert shown_ids(store, owner, view) == []
        assert ids(listed(store, owner, address=view)) == [cancel, third, second, first]
    assert before[0].keys() == before[-1].keys()
    after = send(store, owner, "assign")
    other = send(store, owner, "assign", OTHER)
    assert after in ids(listed(store, owner, "--open", address=WORKER))
    assert ids(listed(store, owner, "--open", address=OTHER)) == [other]
    assert rows(store) == 1


def test_cancel_ignores_other_grant_and_earlier_cancel(
    enrolled: tuple[Store, Direction, SenderContext],
) -> None:
    store, _direction, owner = enrolled
    add_worker(store, owner.project)
    other = send(store, owner, "assign", OTHER)
    cancelled = send(store, owner, "assign")
    send(store, owner, "cancel")
    later = send(store, owner, "assign")
    assert ids(listed(store, owner, "--open")) == [later, other]
    assert cancelled not in ids(listed(store, owner, "--open"))


def test_cap_applies_after_filter(
    enrolled: tuple[Store, Direction, SenderContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _direction, owner = enrolled
    monkeypatch.setattr(work_state, "MAX_DIRECTIONS", 2)
    opened = [send(store, owner, "assign") for _ in range(2)]
    for _ in range(3):
        settled = send(store, owner, "assign")
        report(store, settled, "completed")
    assert ids(listed(store, owner, "--open")) == opened[::-1]
    assert len(listed(store, owner)) == 2


def test_cancel_outside_cap_still_closes(
    enrolled: tuple[Store, Direction, SenderContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _direction, owner = enrolled
    monkeypatch.setattr(work_state, "MAX_DIRECTIONS", 1)
    send(store, owner, "assign")
    send(store, owner, "cancel")
    for _ in range(2):
        report(store, send(store, owner, "assign"), "completed")
    assert listed(store, owner, "--open") == []


def relay_config(tmp_path: Path) -> Config:
    token = tmp_path / "a.token"
    token.write_text("a" * 43)
    token.chmod(0o600)
    peer = PeerPolicy("b", send_messages=True, receive_messages="inbox")
    return Config(
        "https://bus.test",
        "a",
        token,
        (Repository(routing_scope(str(tmp_path)), "xyz", (peer,)),),
    )


def test_outgoing_remote_direction_uses_relay_snapshot(
    enrolled: tuple[Store, Direction, SenderContext], tmp_path: Path
) -> None:
    store, direction, owner = enrolled
    sent = enqueue_remote(
        store, relay_config(tmp_path), owner, "b/xyz/claude:worker", "assign", direction=direction
    )
    relay_id = cast("str", sent["remote_id"])
    message_id = cast("int", sent["id"])
    with store.transaction() as db:
        db.execute("UPDATE remote_outbox SET state='acknowledged' WHERE id=?", (relay_id,))
        db.execute(
            "INSERT INTO remote_reports VALUES (?,?)",
            (
                relay_id,
                json.dumps(
                    {
                        "message_id": relay_id,
                        "revision": 1,
                        "status": "proceeding",
                        "reason": "started",
                    }
                ),
            ),
        )
    (item,) = listed(store, owner)
    assert item["work_report"] == message_status(store.path, message_id)["work_report"]
    assert item["work_report"] == {
        "message_id": relay_id,
        "revision": 1,
        "status": "proceeding",
        "reason": "started",
    }
    assert item["state_changed_at"] is None
    assert item["remote_id"] == relay_id


def test_outgoing_remote_direction_without_report_keeps_ack_time(
    enrolled: tuple[Store, Direction, SenderContext], tmp_path: Path
) -> None:
    store, direction, owner = enrolled
    sent = enqueue_remote(
        store, relay_config(tmp_path), owner, "b/xyz/claude:worker", "assign", direction=direction
    )
    relay_id = cast("str", sent["remote_id"])
    with store.transaction() as db:
        db.execute("UPDATE remote_outbox SET state='acknowledged' WHERE id=?", (relay_id,))
        db.execute(
            "UPDATE messages SET acknowledged_at='2026-01-01T00:00:00+00:00' WHERE id=?",
            (sent["id"],),
        )
        db.execute(
            "INSERT INTO remote_reports VALUES (?,?)",
            (
                relay_id,
                json.dumps({"message_id": relay_id, "revision": 0, "status": None, "reason": None}),
            ),
        )
    (item,) = listed(store, owner)
    assert cast("dict[str, object]", item["work_report"])["status"] is None
    assert item["acknowledged_at"] is not None
    assert item["state_changed_at"] == item["acknowledged_at"]


def test_imported_direction_is_never_open(
    enrolled: tuple[Store, Direction, SenderContext],
) -> None:
    store, _direction, owner = enrolled
    message_id = send(store, owner, "assign")
    with store.transaction() as db:
        db.execute(
            "INSERT INTO remote_imports (id, local_id, envelope) VALUES (?,?,?)",
            (RELAY_ID, message_id, "{}"),
        )
    (item,) = listed(store, owner)
    assert item["remote_id"] == RELAY_ID
    assert item["work_report"] is None
    assert listed(store, owner, "--open") == []


def test_listing_rejects_unknown_filter(enrolled: tuple[Store, Direction, SenderContext]) -> None:
    store, _direction, _owner = enrolled
    with store.transaction() as db, pytest.raises(ValueError, match="only"):
        work_state.listing(db, {}, OWNER, coordinator=True, only="bogus")
