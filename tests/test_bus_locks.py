"""Resource lock ownership, queuing, hand-off, and holder wakes."""

from __future__ import annotations

import io
import json
import sqlite3
from typing import TYPE_CHECKING, cast

import pytest

from llm_bus import main
from llm_bus_locks import TIMER_POLL_SECONDS, Locks, Request, run_timer
from llm_bus_store import BusError, Store

if TYPE_CHECKING:
    from pathlib import Path


def _no_timer(_key: object) -> None:
    """Tests drive checks directly instead of spawning detached timers."""


@pytest.fixture(autouse=True)
def isolate_host(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("llm_bus_locks.schedule_check", _no_timer)


def _record_wakes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    woken: list[str] = []

    def fake_wake(recipient: str, *_args: object, **_kwargs: object) -> dict[str, str]:
        woken.append(recipient)
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr("llm_bus_dispatch.wake", fake_wake)
    return woken


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "bus.sqlite3")


def _bodies(store: Store, recipient: str) -> list[str]:
    return [message["body"] for message in store.inbox(recipient)]


def test_free_resource_is_acquired_then_renewed_by_its_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wakes = _record_wakes(monkeypatch)
    store = _store(tmp_path)
    holder = store.register("claude", "one", "/project")
    locks = Locks(store, holder, "/project")
    key = locks.key("db:orders", shared=False)

    first = locks.acquire(
        key, Request(reason="migrating", ttl_minutes=None, check="1h", describe="test resource")
    )
    assert first["status"] == "acquired"
    lock = cast("dict[str, object]", first["lock"])
    assert lock["holder"] == holder
    assert lock["reason"] == "migrating"
    assert lock["scope"] == key.scope
    assert lock["waiters"] == []
    assert first["notices"] == []

    again = locks.acquire(
        key, Request(reason=None, ttl_minutes=5, check="5m", describe="test resource")
    )
    assert again["status"] == "renewed"
    renewed = cast("dict[str, object]", again["lock"])
    assert renewed["reason"] == "migrating"
    assert cast("str", renewed["expires_at"]) < cast("str", lock["expires_at"])
    assert wakes == []


def test_contended_acquire_queues_waiter_and_wakes_holder_once_per_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wakes = _record_wakes(monkeypatch)
    store = _store(tmp_path)
    holder = store.register("claude", "one", "/project")
    waiter = store.register("codex", "two", "/project")
    key = Locks(store, holder, "/project").key("db:orders", shared=False)
    Locks(store, holder, "/project").acquire(
        key, Request(reason=None, ttl_minutes=None, check="1h", describe="test resource")
    )

    quiet = Locks(store, waiter, "/project").acquire(
        key, Request(reason="need schema", ttl_minutes=None, check="1h", describe="test resource")
    )
    assert quiet["status"] == "waiting"
    assert quiet["notified"] is False
    assert quiet["notices"] == []
    assert wakes == []

    asked = Locks(store, waiter, "/project").acquire(
        key,
        Request(reason=None, ttl_minutes=None, check="1h", notify=True, describe="test resource"),
    )
    assert asked["status"] == "waiting"
    assert asked["notified"] is True
    lock = cast("dict[str, object]", asked["lock"])
    assert lock["holder"] == holder
    assert [item["waiter"] for item in cast("list[dict[str, object]]", lock["waiters"])] == [waiter]
    notices = cast("list[dict[str, object]]", asked["notices"])
    assert [item["to"] for item in notices] == [holder]
    assert wakes == [holder]
    [body] = _bodies(store, holder)
    assert body.startswith("llm-bus lock request:")
    assert "need schema" in body
    assert "`llm-bus lock release db:orders`" in body

    repeat = Locks(store, waiter, "/project").acquire(
        key,
        Request(reason=None, ttl_minutes=None, check="1h", notify=True, describe="test resource"),
    )
    assert repeat["status"] == "waiting"
    assert repeat["notified"] is False
    assert repeat["notices"] == []
    assert wakes == [holder]
    assert len(_bodies(store, holder)) == 1


def test_release_hands_off_to_first_live_waiter_and_wakes_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wakes = _record_wakes(monkeypatch)
    store = _store(tmp_path)
    holder = store.register("claude", "one", "/project")
    gone = store.register("codex", "two", "/project")
    later = store.register("claude", "three", "/project")
    key = Locks(store, holder, "/project").key("db:orders", shared=False)
    Locks(store, holder, "/project").acquire(
        key, Request(reason=None, ttl_minutes=None, check="1h", describe="test resource")
    )
    Locks(store, gone, "/project").acquire(
        key,
        Request(reason=None, ttl_minutes=None, check="1h", notify=True, describe="test resource"),
    )
    Locks(store, later, "/project").acquire(
        key,
        Request(
            reason="reports", ttl_minutes=None, check="1h", notify=True, describe="test resource"
        ),
    )
    store.end_session("codex", "two")
    assert wakes == [holder, holder]

    with pytest.raises(BusError, match="held by"):
        Locks(store, later, "/project").release(key)
    released = Locks(store, holder, "/project").release(key)
    assert released["status"] == "released"
    assert released["granted_to"] == later
    lock = cast("dict[str, object]", released["lock"])
    assert lock["holder"] == later
    assert lock["reason"] == "reports"
    assert lock["waiters"] == []
    assert wakes == [holder, holder, later]
    [body] = _bodies(store, later)
    assert body.startswith("llm-bus lock granted:")

    final = Locks(store, later, "/project").release(key)
    assert final["granted_to"] is None
    assert final["lock"] is None
    with pytest.raises(BusError, match="not locked"):
        Locks(store, later, "/project").release(key)


def test_expired_or_ended_holders_lose_the_lock_on_any_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wakes = _record_wakes(monkeypatch)
    store = _store(tmp_path)
    holder = store.register("claude", "one", "/project")
    waiter = store.register("codex", "two", "/project")
    key = Locks(store, holder, "/project").key("db:orders", shared=False)
    Locks(store, holder, "/project").acquire(
        key, Request(reason=None, ttl_minutes=1, check="1m", describe="test resource")
    )
    Locks(store, waiter, "/project").acquire(
        key, Request(reason=None, ttl_minutes=None, check="1h", describe="test resource")
    )
    with store.transaction() as db:
        db.execute("UPDATE resource_locks SET expires_at='2000-01-01T00:00:00.000Z'")
    assert wakes == []

    status = Locks(store, holder, "/project").status(None, every_scope=False)
    [lock] = cast("list[dict[str, object]]", status["locks"])
    assert lock["holder"] == waiter
    assert [item["to"] for item in cast("list[dict[str, object]]", status["notices"])] == [waiter]
    assert wakes == [waiter]

    store.end_session("codex", "two")
    empty = Locks(store, holder, "/project").status(None, every_scope=False)
    assert empty["locks"] == []


def test_scopes_separate_folders_unless_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wakes = _record_wakes(monkeypatch)
    store = _store(tmp_path)
    first = store.register("claude", "one", str(tmp_path / "first"))
    second = store.register("codex", "two", str(tmp_path / "second"))
    one = Locks(store, first, str(tmp_path / "first"))
    two = Locks(store, second, str(tmp_path / "second"))

    assert one.acquire(
        one.key("db", shared=False),
        Request(reason=None, ttl_minutes=None, check="1h", describe="test resource"),
    )["status"] == ("acquired")
    assert two.acquire(
        two.key("db", shared=False),
        Request(reason=None, ttl_minutes=None, check="1h", describe="test resource"),
    )["status"] == ("acquired")
    assert one.acquire(
        one.key("db", shared=True),
        Request(reason=None, ttl_minutes=None, check="1h", describe="test resource"),
    )["status"] == ("acquired")
    waiting = two.acquire(
        two.key("db", shared=True),
        Request(reason=None, ttl_minutes=None, check="1h", notify=True, describe="test resource"),
    )
    assert waiting["status"] == "waiting"
    assert wakes == [first]
    assert "--shared" in _bodies(store, first)[0]

    visible = cast("list[dict[str, object]]", two.status(None, every_scope=False)["locks"])
    assert sorted((lock["scope"], lock["holder"]) for lock in visible) == sorted(
        [(one.key("db", shared=True).scope, first), (two.key("db", shared=False).scope, second)]
    )
    everything = cast("list[dict[str, object]]", two.status(None, every_scope=True)["locks"])
    assert len(everything) == 3


def test_invalid_names_reasons_and_ttls_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    holder = store.register("claude", "one", "/project")
    locks = Locks(store, holder, "/project")
    for name in ("", "   ", "a" * 201, "bad\nname"):
        with pytest.raises(BusError, match="Resource name"):
            locks.key(name, shared=False)
    key = locks.key("  db  ", shared=False)
    assert key.resource == "db"
    for ttl in (0, -1, 10081):
        with pytest.raises(BusError, match="TTL"):
            locks.acquire(
                key, Request(reason=None, ttl_minutes=ttl, check="1m", describe="test resource")
            )
    with pytest.raises(BusError, match="Reason"):
        locks.acquire(
            key, Request(reason="x" * 501, ttl_minutes=None, check="1h", describe="test resource")
        )


def test_hook_guidance_names_held_and_awaited_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wakes = _record_wakes(monkeypatch)
    store = _store(tmp_path)
    holder = store.register("claude", "one", "/project")
    waiter = store.register("codex", "two", "/project")
    key = Locks(store, holder, "/project").key("db:orders", shared=False)
    assert Locks(store, holder, "/project").summary() == ""
    Locks(store, holder, "/project").acquire(
        key, Request(reason=None, ttl_minutes=None, check="1h", describe="test resource")
    )
    Locks(store, waiter, "/project").acquire(
        key, Request(reason=None, ttl_minutes=None, check="1h", describe="test resource")
    )
    assert wakes == []

    held = Locks(store, holder, "/project").summary()
    assert held.startswith("Known lock names: db:orders = test resource (held by claude:one). ")
    assert "Locks you hold: db:orders (check due " in held
    assert "`llm-bus lock release RESOURCE`" in held
    assert "--check 30m" in held
    awaiting = Locks(store, waiter, "/project").summary()
    [lock] = cast(
        "list[dict[str, str]]",
        Locks(store, holder, "/project").status(key, every_scope=False)["locks"],
    )
    check = lock["check_at"]
    assert awaiting == (
        f"Known lock names: db:orders = test resource (held by {holder}). "
        "Reuse these names for the same resource; `llm-bus lock list` shows all. "
        f"Locks you await: db:orders (held by {holder}, check due {check}); "
        "you are woken when one passes to you. "
        "`llm-bus lock acquire RESOURCE --notify` asks the holder now. "
    )


def test_cli_lock_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wakes = _record_wakes(monkeypatch)
    monkeypatch.setenv("LLM_BUS_DB", str(tmp_path / "bus.sqlite3"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "one")
    assert (
        main(
            [
                "lock",
                "acquire",
                "db:orders",
                "--reason",
                "migrating",
                "--ttl",
                "10",
                "--check",
                "5m",
                "--describe",
                "orders database",
            ]
        )
        == 0
    )
    acquired = cast("dict[str, object]", json.loads(capsys.readouterr().out))
    assert acquired["status"] == "acquired"

    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.setenv("CODEX_THREAD_ID", "two")
    assert main(["lock", "acquire", "db:orders"]) == 0
    waiting = cast("dict[str, object]", json.loads(capsys.readouterr().out))
    assert waiting["status"] == "waiting"
    assert wakes == []
    assert main(["lock", "acquire", "db:orders", "--notify"]) == 0
    asked = cast("dict[str, object]", json.loads(capsys.readouterr().out))
    assert asked["notified"] is True
    assert wakes == ["claude:one"]
    assert main(["lock", "release", "db:orders"]) == 1
    assert "held by claude:one" in capsys.readouterr().err

    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "one")
    assert main(["lock", "release", "db:orders"]) == 0
    released = cast("dict[str, object]", json.loads(capsys.readouterr().out))
    assert released["granted_to"] == "codex:two"
    assert main(["lock", "status", "--all"]) == 0
    status = cast("dict[str, object]", json.loads(capsys.readouterr().out))
    [lock] = cast("list[dict[str, object]]", status["locks"])
    assert lock["holder"] == "codex:two"
    assert main(["lock", "status", "db:orders", "--shared"]) == 0
    assert json.loads(capsys.readouterr().out)["locks"] == []


def _overdue(store: Store) -> None:
    with store.transaction() as db:
        db.execute("UPDATE resource_locks SET check_at='2000-01-01T00:00:00.000Z'")


def test_due_batch_survives_one_wake_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    holders = [store.register("codex", name, "/project") for name in ("one", "two")]
    for holder, resource in zip(holders, ("db", "deploy"), strict=True):
        locks = Locks(store, holder, "/project")
        locks.acquire(locks.key(resource, shared=False), Request(check="1h", describe=resource))
    _overdue(store)
    attempted: list[str] = []

    def dispatch(_store: Store, recipient: str, *, trigger_id: int) -> dict[str, str]:
        assert trigger_id > 0
        attempted.append(recipient)
        assert all(len(store.inbox(holder)) == 1 for holder in holders)
        if len(attempted) == 1:
            raise OSError("host unavailable")
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr("llm_bus_locks.dispatch_pending", dispatch)
    locks = Locks(store, holders[0], "/project")
    result = locks.status(None, every_scope=False)
    deliveries = cast("list[dict[str, object]]", result["notices"])
    assert cast("dict[str, str]", deliveries[0]["wake"])["status"] == "unknown"
    assert set(attempted) == set(holders)
    assert locks.status(None, every_scope=False)["notices"] == []


@pytest.mark.parametrize("handoff", [False, True])
def test_notice_persistence_failure_rolls_back_lock_state(tmp_path: Path, *, handoff: bool) -> None:
    store = _store(tmp_path)
    holder = store.register("claude", "one", "/project")
    waiter = store.register("codex", "two", "/project")
    mine = Locks(store, holder, "/project")
    key = mine.key("db", shared=False)
    mine.acquire(key, Request(check="1h", describe="database"))
    Locks(store, waiter, "/project").acquire(key, Request())
    if not handoff:
        _overdue(store)
    with store.transaction() as db:
        db.execute(
            "CREATE TRIGGER reject_notice BEFORE INSERT ON messages "
            "BEGIN SELECT RAISE(ABORT, 'storage failure'); END"
        )
    if handoff:
        with pytest.raises(sqlite3.IntegrityError, match="storage failure"):
            mine.release(key)
    else:
        with pytest.raises(sqlite3.IntegrityError, match="storage failure"):
            mine.status(None, every_scope=False)
    with store.transaction() as db:
        assert db.execute("SELECT holder,checked_at FROM resource_locks").fetchall() == [
            (holder, None)
        ]
        assert db.execute("SELECT waiter FROM resource_lock_waiters").fetchall() == [(waiter,)]
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)


def test_summary_preserves_shared_scope_after_catalog_is_seen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    holder = store.register("claude", "one", "/project")
    waiter = store.register("codex", "two", "/project")
    mine = Locks(store, holder, "/project")
    theirs = Locks(store, waiter, "/project")
    for shared in (False, True):
        key = mine.key("db", shared=shared)
        mine.acquire(key, Request(check="1h", describe="database"))
        theirs.acquire(key, Request())
    mine.summary()
    theirs.summary()
    for summary in (mine.summary(), theirs.summary()):
        assert "Known lock names" not in summary
        assert "db (" in summary
        assert "db --shared (machine-wide)" in summary
        assert "include --shared in every release, acquire --check, and acquire --notify" in summary
        assert "`llm-bus lock release RESOURCE --shared`" in summary
        assert "`llm-bus lock acquire RESOURCE --shared --check 30m`" in summary
        assert "`llm-bus lock acquire RESOURCE --shared --notify`" in summary


def test_holder_must_estimate_release_within_its_lease(tmp_path: Path) -> None:
    store = _store(tmp_path)
    holder = store.register("claude", "one", "/project")
    locks = Locks(store, holder, "/project")
    key = locks.key("db", shared=False)
    with pytest.raises(BusError, match="--check"):
        locks.acquire(
            key, Request(check=None, reason=None, ttl_minutes=None, describe="test resource")
        )
    for bad in ("soon", "0m", "2w", "-5m"):
        with pytest.raises(BusError, match="--check"):
            locks.acquire(
                key, Request(check=bad, reason=None, ttl_minutes=None, describe="test resource")
            )
    with pytest.raises(BusError, match="beyond the lease"):
        locks.acquire(
            key, Request(check="2h", reason=None, ttl_minutes=60, describe="test resource")
        )
    acquired = locks.acquire(
        key, Request(check="90", reason=None, ttl_minutes=None, describe="test resource")
    )
    lock = cast("dict[str, object]", acquired["lock"])
    assert cast("str", lock["acquired_at"]) < cast("str", lock["check_at"])
    assert lock["checked_at"] is None


def test_due_check_asks_holder_once_until_it_answers_with_a_new_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wakes = _record_wakes(monkeypatch)
    store = _store(tmp_path)
    holder = store.register("claude", "one", "/project")
    waiter = store.register("codex", "two", "/project")
    mine = Locks(store, holder, "/project")
    key = mine.key("db:orders", shared=False)
    mine.acquire(key, Request(check="1h", reason=None, ttl_minutes=None, describe="test resource"))
    Locks(store, waiter, "/project").acquire(
        key, Request(check=None, reason=None, ttl_minutes=None, describe="test resource")
    )
    assert mine.status(None, every_scope=False)["notices"] == []
    _overdue(store)

    first = Locks(store, waiter, "/project").status(None, every_scope=False)
    assert [item["to"] for item in cast("list[dict[str, object]]", first["notices"])] == [holder]
    assert wakes == [holder]
    messages = store.inbox(holder)
    assert len(messages) == 1
    message = messages[0]
    assert message["sender"] == holder
    assert message["body"].startswith("llm-bus lock check: you estimated resource 'db:orders'")
    assert "1 session(s) are waiting" in message["body"]
    assert Locks(store, waiter, "/project").status(None, every_scope=False)["notices"] == []
    assert "is unanswered" in mine.summary()

    renewed = mine.acquire(
        key, Request(check="30m", reason=None, ttl_minutes=None, describe="test resource")
    )
    assert renewed["status"] == "renewed"
    assert cast("dict[str, object]", renewed["lock"])["checked_at"] is None
    _overdue(store)
    again = mine.status(None, every_scope=False)
    assert [item["to"] for item in cast("list[dict[str, object]]", again["notices"])] == [holder]
    assert wakes == [holder, holder]


def test_granted_holder_is_asked_for_an_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_wakes(monkeypatch)
    store = _store(tmp_path)
    holder = store.register("claude", "one", "/project")
    waiter = store.register("codex", "two", "/project")
    key = Locks(store, holder, "/project").key("db", shared=False)
    Locks(store, holder, "/project").acquire(
        key, Request(check="1h", reason=None, ttl_minutes=None, describe="test resource")
    )
    Locks(store, waiter, "/project").acquire(
        key, Request(check=None, reason=None, ttl_minutes=None, describe="test resource")
    )
    Locks(store, holder, "/project").release(key)
    messages = store.inbox(waiter)
    assert len(messages) == 1
    grant = messages[0]
    assert "--check DURATION" in grant["body"]
    assert "is unanswered" in Locks(store, waiter, "/project").summary()
    assert Locks(store, waiter, "/project").status(key, every_scope=False)["notices"] == []


def test_timer_sleeps_until_estimate_then_raises_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wakes = _record_wakes(monkeypatch)
    store = _store(tmp_path)
    holder = store.register("claude", "one", "/project")
    locks = Locks(store, holder, "/project")
    key = locks.key("db", shared=False)
    locks.acquire(key, Request(check="2h", reason=None, ttl_minutes=None, describe="test resource"))
    with store.transaction() as db:
        db.execute(
            "UPDATE agents SET last_seen=strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 hour') "
            "WHERE address=?",
            (holder,),
        )
        last_seen = cast(
            "tuple[str]",
            db.execute("SELECT last_seen FROM agents WHERE address=?", (holder,)).fetchone(),
        )
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        _overdue(store)

    run_timer(store, key, sleep=fake_sleep)
    assert len(slept) == 1
    assert 0 < slept[0] <= TIMER_POLL_SECONDS
    assert wakes == [holder]
    with store.transaction() as db:
        assert db.execute("SELECT last_seen FROM agents WHERE address=?", (holder,)).fetchone() == (
            last_seen
        )
    run_timer(store, key, sleep=fake_sleep)
    assert wakes == [holder]
    assert len(slept) == 1
    locks.release(key)
    run_timer(store, key, sleep=fake_sleep)
    assert len(slept) == 1


def test_timer_handoff_does_not_reactivate_ended_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wakes = _record_wakes(monkeypatch)
    store = _store(tmp_path)
    holder = store.register("claude", "one", "/project")
    waiter = store.register("codex", "two", "/project")
    locks = Locks(store, holder, "/project")
    key = locks.key("db", shared=False)
    locks.acquire(key, Request(check="1h", describe="database"))
    Locks(store, waiter, "/project").acquire(key, Request())

    def end_while_sleeping(_seconds: float) -> None:
        store.end_session("claude", "one")
        with store.transaction() as db:
            db.execute(
                "UPDATE agents SET ended_at='2000-01-01T00:00:00.000Z' WHERE address=?",
                (holder,),
            )
        _overdue(store)

    run_timer(store, key, sleep=end_while_sleeping)
    with store.transaction() as db:
        assert db.execute("SELECT ended_at FROM agents WHERE address=?", (holder,)).fetchone() == (
            "2000-01-01T00:00:00.000Z",
        )
        assert db.execute("SELECT holder FROM resource_locks").fetchall() == [(waiter,)]
    assert wakes == [waiter]
    [message] = store.inbox(waiter)
    assert message["sender"] == holder
    assert message["body"].startswith("llm-bus lock granted:")


def test_full_guidance_teaches_locks_and_reaches_running_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LLM_BUS_DB", str(tmp_path / "bus.sqlite3"))
    store = Store(tmp_path / "bus.sqlite3")
    address = store.register("claude", "one", str(tmp_path))
    store.guidance_needed(address, 1, reset=False)
    hook = json.dumps(
        {"session_id": "one", "hook_event_name": "UserPromptSubmit", "cwd": str(tmp_path)}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(hook))
    assert main(["hook", "claude"]) == 0
    assert "llm-bus lock acquire NAME --check 45m" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO(hook))
    assert main(["hook", "claude"]) == 0
    assert "llm-bus lock acquire NAME" not in capsys.readouterr().out


def test_new_names_need_a_purpose_and_look_alikes_need_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_wakes(monkeypatch)
    store = _store(tmp_path)
    first = store.register("claude", "one", "/project")
    second = store.register("codex", "two", "/project")
    one, two = Locks(store, first, "/project"), Locks(store, second, "/project")
    with pytest.raises(BusError, match="new lock name; add --describe"):
        one.acquire(one.key("db:orders", shared=False), Request(check="1h"))
    one.acquire(
        one.key("db:orders", shared=False),
        Request(check="1h", describe="orders Postgres database"),
    )
    one.release(one.key("db:orders", shared=False))

    for alike in ("orders-db", "DB_Orders", "orders", "db:order"):
        with pytest.raises(
            BusError, match=r"looks like existing lock name\(s\): db:orders \(orders"
        ):
            two.acquire(two.key(alike, shared=False), Request(check="1h", describe="dup"))
    distinct = two.acquire(
        two.key("orders-db", shared=False),
        Request(check="1h", describe="orders read replica", new=True),
    )
    assert distinct["status"] == "acquired"
    unrelated = two.acquire(
        two.key("deploy:staging", shared=False), Request(check="1h", describe="staging deploy")
    )
    assert unrelated["status"] == "acquired"

    reused = two.acquire(two.key("db:orders", shared=False), Request(check="1h"))
    assert reused["status"] == "acquired"
    listed = cast("list[dict[str, object]]", one.catalog(every_scope=False)["resources"])
    assert {item["resource"]: (item["description"], item["held_by"]) for item in listed} == {
        "db:orders": ("orders Postgres database", second),
        "orders-db": ("orders read replica", second),
        "deploy:staging": ("staging deploy", second),
    }
    two.release(two.key("deploy:staging", shared=False))
    free = [
        item
        for item in cast("list[dict[str, object]]", one.catalog(every_scope=False)["resources"])
        if item["resource"] == "deploy:staging"
    ]
    assert free[0]["held_by"] is None
    assert "deploy:staging = staging deploy (free)" in one.summary()


def test_idle_names_are_forgotten_after_thirty_days(tmp_path: Path) -> None:
    store = _store(tmp_path)
    holder = store.register("claude", "one", "/project")
    locks = Locks(store, holder, "/project")
    key = locks.key("db", shared=False)
    locks.acquire(key, Request(check="1h", describe="main database"))
    locks.release(key)
    with store.transaction() as db:
        db.execute("UPDATE resource_catalog SET last_used_at='2000-01-01T00:00:00.000Z'")
    assert locks.catalog(every_scope=True)["resources"] == []


def test_lock_names_reach_the_hook_only_when_they_are_news(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_wakes(monkeypatch)
    store = _store(tmp_path)
    first = store.register("claude", "one", "/project")
    other = store.register("codex", "two", "/project")
    mine, theirs = Locks(store, first, "/project"), Locks(store, other, "/project")
    assert mine.summary() == ""
    theirs.acquire(theirs.key("db", shared=False), Request(check="1h", describe="main database"))
    assert mine.summary().startswith("Known lock names: db = main database (held by codex:two)")
    assert mine.summary() == ""
    theirs.release(theirs.key("db", shared=False))
    assert mine.summary() == ""
    assert "Known lock names: db = main database (free)" in mine.summary(full=True)
    theirs.acquire(
        theirs.key("deploy", shared=False), Request(check="1h", describe="staging deploy")
    )
    news = mine.summary()
    assert "deploy = staging deploy" in news
    assert "db = main database" in news
    assert mine.summary() == ""
