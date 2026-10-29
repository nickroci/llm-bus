"""Resource lock ownership, queuing, hand-off, and holder wakes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import pytest

from llm_bus import main
from llm_bus_locks import TIMER_POLL_SECONDS, Locks, run_timer
from llm_bus_store import BusError, Store

if TYPE_CHECKING:
    from pathlib import Path


def _no_timer(_key: object) -> None:
    """Tests drive checks directly instead of spawning detached timers."""


@pytest.fixture(autouse=True)
def isolate_host(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET", "CODEX_THREAD_ID"):
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

    first = locks.acquire(key, reason="migrating", ttl_minutes=None, check="1h")
    assert first["status"] == "acquired"
    lock = cast("dict[str, object]", first["lock"])
    assert lock["holder"] == holder
    assert lock["reason"] == "migrating"
    assert lock["scope"] == key.scope
    assert lock["waiters"] == []
    assert first["notices"] == []

    again = locks.acquire(key, reason=None, ttl_minutes=5, check="5m")
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
    Locks(store, holder, "/project").acquire(key, reason=None, ttl_minutes=None, check="1h")

    quiet = Locks(store, waiter, "/project").acquire(
        key, reason="need schema", ttl_minutes=None, check="1h"
    )
    assert quiet["status"] == "waiting"
    assert quiet["notified"] is False
    assert quiet["notices"] == []
    assert wakes == []

    asked = Locks(store, waiter, "/project").acquire(
        key, reason=None, ttl_minutes=None, check="1h", notify=True
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
        key, reason=None, ttl_minutes=None, check="1h", notify=True
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
    Locks(store, holder, "/project").acquire(key, reason=None, ttl_minutes=None, check="1h")
    Locks(store, gone, "/project").acquire(
        key, reason=None, ttl_minutes=None, check="1h", notify=True
    )
    Locks(store, later, "/project").acquire(
        key, reason="reports", ttl_minutes=None, check="1h", notify=True
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
    Locks(store, holder, "/project").acquire(key, reason=None, ttl_minutes=1, check="1m")
    Locks(store, waiter, "/project").acquire(key, reason=None, ttl_minutes=None, check="1h")
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

    assert one.acquire(one.key("db", shared=False), reason=None, ttl_minutes=None, check="1h")[
        "status"
    ] == ("acquired")
    assert two.acquire(two.key("db", shared=False), reason=None, ttl_minutes=None, check="1h")[
        "status"
    ] == ("acquired")
    assert one.acquire(one.key("db", shared=True), reason=None, ttl_minutes=None, check="1h")[
        "status"
    ] == ("acquired")
    waiting = two.acquire(
        two.key("db", shared=True), reason=None, ttl_minutes=None, check="1h", notify=True
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
            locks.acquire(key, reason=None, ttl_minutes=ttl, check="1m")
    with pytest.raises(BusError, match="Reason"):
        locks.acquire(key, reason="x" * 501, ttl_minutes=None, check="1h")


def test_hook_guidance_names_held_and_awaited_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wakes = _record_wakes(monkeypatch)
    store = _store(tmp_path)
    holder = store.register("claude", "one", "/project")
    waiter = store.register("codex", "two", "/project")
    key = Locks(store, holder, "/project").key("db:orders", shared=False)
    assert Locks(store, holder, "/project").summary() == ""
    Locks(store, holder, "/project").acquire(key, reason=None, ttl_minutes=None, check="1h")
    Locks(store, waiter, "/project").acquire(key, reason=None, ttl_minutes=None, check="1h")
    assert wakes == []

    held = Locks(store, holder, "/project").summary()
    assert held.startswith("Locks you hold: db:orders (check due ")
    assert "`llm-bus lock release RESOURCE`" in held
    assert "--check 30m" in held
    awaiting = Locks(store, waiter, "/project").summary()
    [lock] = cast(
        "list[dict[str, str]]",
        Locks(store, holder, "/project").status(key, every_scope=False)["locks"],
    )
    check = lock["check_at"]
    assert awaiting == (
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


def test_holder_must_estimate_release_within_its_lease(tmp_path: Path) -> None:
    store = _store(tmp_path)
    holder = store.register("claude", "one", "/project")
    locks = Locks(store, holder, "/project")
    key = locks.key("db", shared=False)
    with pytest.raises(BusError, match="--check"):
        locks.acquire(key, check=None, reason=None, ttl_minutes=None)
    for bad in ("soon", "0m", "2w", "-5m"):
        with pytest.raises(BusError, match="--check"):
            locks.acquire(key, check=bad, reason=None, ttl_minutes=None)
    with pytest.raises(BusError, match="beyond the lease"):
        locks.acquire(key, check="2h", reason=None, ttl_minutes=60)
    acquired = locks.acquire(key, check="90", reason=None, ttl_minutes=None)
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
    mine.acquire(key, check="1h", reason=None, ttl_minutes=None)
    Locks(store, waiter, "/project").acquire(key, check=None, reason=None, ttl_minutes=None)
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

    renewed = mine.acquire(key, check="30m", reason=None, ttl_minutes=None)
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
    Locks(store, holder, "/project").acquire(key, check="1h", reason=None, ttl_minutes=None)
    Locks(store, waiter, "/project").acquire(key, check=None, reason=None, ttl_minutes=None)
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
    locks.acquire(key, check="2h", reason=None, ttl_minutes=None)
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        _overdue(store)

    run_timer(store, key, sleep=fake_sleep)
    assert len(slept) == 1
    assert 0 < slept[0] <= TIMER_POLL_SECONDS
    assert wakes == [holder]
    run_timer(store, key, sleep=fake_sleep)
    assert wakes == [holder]
    assert len(slept) == 1
    locks.release(key)
    run_timer(store, key, sleep=fake_sleep)
    assert len(slept) == 1
