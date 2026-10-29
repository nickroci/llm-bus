"""Advisory leased locks on named resources, with scheduled and requested holder checks."""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
import time
from typing import TYPE_CHECKING, NamedTuple, TypedDict, cast

from llm_bus_dispatch import dispatch_pending
from llm_bus_store import BusError, SenderContext, expire_idle_agents, routing_scope

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from llm_bus_store import Store
    from llm_bus_wake import WakeResult

DEFAULT_TTL_MINUTES = 24 * 60
MAX_TTL_MINUTES = 7 * 24 * 60
NUDGE_INTERVAL_MINUTES = 5
MAX_RESOURCE_LENGTH = 200
MAX_REASON_LENGTH = 500
SHARED_SCOPE = "shared"
TIMER_POLL_SECONDS = 15 * 60
TIMER_START_SECONDS = 10
CHECK_UNITS = {"": 1, "m": 1, "h": 60, "d": 24 * 60}
CHECK_PATTERN = re.compile(r"^(\d{1,6})\s*([mhd]?)$")
CHECK_HELP = "a duration such as 45m, 2h, or 1d"


class Waiter(TypedDict):
    """Session queued for a held resource, in request order."""

    waiter: str
    reason: str | None
    requested_at: str
    nudged_at: str | None


class Lock(TypedDict):
    """Current holder of a resource, its lease and check times, and the queue behind it."""

    resource: str
    scope: str
    holder: str
    reason: str | None
    acquired_at: str
    expires_at: str
    check_at: str
    checked_at: str | None
    waiters: list[Waiter]


class Delivery(TypedDict):
    """Bus message sent because a lock changed hands, was requested, or came up for a check."""

    to: str
    message_id: int
    wake: WakeResult


class Notice(NamedTuple):
    recipient: str
    body: str
    sender: str | None = None


class Key(NamedTuple):
    scope: str
    resource: str


class Locks:
    """Lock commands for one registered session; every call reaps lapsed holders first."""

    def __init__(self, store: Store, agent_address: str, project: str) -> None:
        self.store: Store = store
        self.agent_address: str = agent_address
        self.project: str = project

    def key(self, resource: str, *, shared: bool) -> Key:
        """Validate a resource name and bind it to this folder, repository, or the shared scope."""
        name = resource.strip()
        if not name or len(name) > MAX_RESOURCE_LENGTH or not name.isprintable():
            raise BusError("Resource name must be 1-200 printable characters")
        return Key(SHARED_SCOPE if shared else routing_scope(self.project), name)

    def acquire(
        self,
        key: Key,
        *,
        check: str | None,
        reason: str | None,
        ttl_minutes: int | None,
        notify: bool = False,
    ) -> dict[str, object]:
        """Take a free resource or renew an owned one with a new check time, else queue.

        The check time is the holder's estimate of when it can release; the bus wakes the
        holder then and asks. With notify, a queued caller also wakes the holder now.
        """
        reason = _clean_reason(reason)
        ttl = _ttl(ttl_minutes)
        with self.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            notices = _reap(db)
            asked: list[Notice] = []
            row = cast(
                "tuple[str] | None",
                db.execute(
                    "SELECT holder FROM resource_locks WHERE scope=? AND resource=?", key
                ).fetchone(),
            )
            if row is None:
                minutes = _check_minutes(check, ttl)
                db.execute(
                    "INSERT INTO resource_locks(scope,resource,holder,reason,acquired_at,"
                    "expires_at,check_at) VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),"
                    "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?),"
                    "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?))",
                    (*key, self.agent_address, reason, f"+{ttl} minutes", f"+{minutes} minutes"),
                )
                db.execute(
                    "DELETE FROM resource_lock_waiters WHERE scope=? AND resource=? AND waiter=?",
                    (*key, self.agent_address),
                )
                status = "acquired"
            elif row[0] == self.agent_address:
                minutes = _check_minutes(check, ttl)
                db.execute(
                    "UPDATE resource_locks SET "
                    "expires_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?), "
                    "check_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?), checked_at=NULL, "
                    "reason=COALESCE(?,reason) WHERE scope=? AND resource=?",
                    (f"+{ttl} minutes", f"+{minutes} minutes", reason, *key),
                )
                status = "renewed"
            else:
                asked = self._enqueue(db, key, row[0], reason, notify=notify)
                notices.extend(asked)
                status = "waiting"
            lock = _read_lock(db, key)
        result: dict[str, object] = {"status": status, "lock": lock}
        if status == "waiting":
            result["notified"] = bool(asked)
        else:
            schedule_check(key)
        return {**result, "notices": self._deliver(notices)}

    def _enqueue(
        self, db: sqlite3.Connection, key: Key, holder: str, reason: str | None, *, notify: bool
    ) -> list[Notice]:
        db.execute(
            "INSERT INTO resource_lock_waiters(scope,resource,waiter,reason,requested_at) "
            "VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) "
            "ON CONFLICT(scope,resource,waiter) DO UPDATE SET "
            "reason=COALESCE(excluded.reason,reason)",
            (*key, self.agent_address, reason),
        )
        if not notify:
            return []
        due = cast(
            "tuple[int]",
            db.execute(
                "SELECT nudged_at IS NULL OR nudged_at <= "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?) FROM resource_lock_waiters "
                "WHERE scope=? AND resource=? AND waiter=?",
                (f"-{NUDGE_INTERVAL_MINUTES} minutes", *key, self.agent_address),
            ).fetchone(),
        )[0]
        if not due:
            return []
        db.execute(
            "UPDATE resource_lock_waiters SET nudged_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE scope=? AND resource=? AND waiter=?",
            (*key, self.agent_address),
        )
        lock = _read_lock(db, key)
        if lock is None:
            raise RuntimeError("Contended lock could not be read")
        stored = next(
            (item["reason"] for item in lock["waiters"] if item["waiter"] == self.agent_address),
            reason,
        )
        return [Notice(holder, _request_body(lock, self.agent_address, stored))]

    def release(self, key: Key) -> dict[str, object]:
        """Give up an owned resource; the first live waiter takes it and is woken."""
        with self.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            notices = _reap(db)
            row = cast(
                "tuple[str] | None",
                db.execute(
                    "SELECT holder FROM resource_locks WHERE scope=? AND resource=?", key
                ).fetchone(),
            )
            if row is None:
                raise BusError(f"Resource {key.resource!r} is not locked")
            if row[0] != self.agent_address:
                raise BusError(
                    f"Resource {key.resource!r} is held by {row[0]}; "
                    "`llm-bus lock acquire --notify` queues you and asks them"
                )
            notices.extend(_hand_off(db, key, row[0]))
            lock = _read_lock(db, key)
        return {
            "status": "released",
            "granted_to": lock["holder"] if lock else None,
            "lock": lock,
            "notices": self._deliver(notices),
        }

    def status(self, key: Key | None, *, every_scope: bool) -> dict[str, object]:
        """Read locks visible from this folder, plus shared ones, without changing ownership."""
        with self.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            notices = _reap(db)
            clauses: list[str] = []
            parameters: list[str] = []
            if key is not None:
                clauses.append("scope=? AND resource=?")
                parameters.extend(key)
            elif not every_scope:
                clauses.append("scope IN (?,?)")
                parameters.extend((routing_scope(self.project), SHARED_SCOPE))
            query = "SELECT scope,resource FROM resource_locks"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            keys = [
                Key(*row)
                for row in cast(
                    "list[tuple[str, str]]",
                    db.execute(query + " ORDER BY scope,resource", parameters).fetchall(),
                )
            ]
            locks = [lock for lock in (_read_lock(db, item) for item in keys) if lock]
        return {"locks": locks, "notices": self._deliver(notices)}

    def summary(self) -> str:
        """One hook sentence naming held and awaited resources, or empty when there are none."""
        with self.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            notices = _reap(db)
            held = cast(
                "list[tuple[str, str, str, str | None]]",
                db.execute(
                    "SELECT resource,expires_at,check_at,checked_at FROM resource_locks "
                    "WHERE holder=? ORDER BY check_at",
                    (self.agent_address,),
                ).fetchall(),
            )
            waiting = cast(
                "list[tuple[str, str, str]]",
                db.execute(
                    "SELECT w.resource,l.holder,l.check_at FROM resource_lock_waiters w "
                    "JOIN resource_locks l ON l.scope=w.scope AND l.resource=w.resource "
                    "WHERE w.waiter=? ORDER BY w.requested_at",
                    (self.agent_address,),
                ).fetchall(),
            )
        self._deliver(notices)
        parts: list[str] = []
        if held:
            names = ", ".join(_held_phrase(*item) for item in held)
            parts.append(
                f"Locks you hold: {names}. Run `llm-bus lock release RESOURCE` when done, "
                "or `llm-bus lock acquire RESOURCE --check 30m` with a new estimate. "
            )
        if waiting:
            names = ", ".join(
                f"{resource} (held by {holder}, check due {check})"
                for resource, holder, check in waiting
            )
            parts.append(
                f"Locks you await: {names}; you are woken when one passes to you. "
                "`llm-bus lock acquire RESOURCE --notify` asks the holder now. "
            )
        return "".join(parts)

    def _deliver(self, notices: list[Notice]) -> list[Delivery]:
        deliveries: list[Delivery] = []
        for notice in notices:
            if notice.sender is None and notice.recipient == self.agent_address:
                continue
            sender = self._sender(notice.sender or self.agent_address)
            sent = self.store.send(sender, notice.recipient, notice.body, cross_folder=True)
            wake = dispatch_pending(self.store, notice.recipient, trigger_id=sent["id"])
            deliveries.append(Delivery(to=notice.recipient, message_id=sent["id"], wake=wake))
        return deliveries

    def _sender(self, sender_address: str) -> SenderContext:
        kind, session_id = sender_address.split(":", 1)
        project = self.project
        if sender_address != self.agent_address:
            with self.store.transaction() as db:
                row = cast(
                    "tuple[str] | None",
                    db.execute(
                        "SELECT project FROM agents WHERE address=?", (sender_address,)
                    ).fetchone(),
                )
            if row is not None:
                project = row[0]
        return SenderContext(kind, session_id, project)


def _held_phrase(resource: str, until: str, check: str, checked: str | None) -> str:
    if checked is not None and checked >= check:
        return f"{resource} (check at {check} is unanswered; lease until {until})"
    return f"{resource} (check due {check}; lease until {until})"


def _clean_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    text = " ".join(reason.split())
    if len(text) > MAX_REASON_LENGTH:
        raise BusError("Reason must be at most 500 characters")
    return text or None


def _ttl(minutes: int | None) -> int:
    if minutes is None:
        return DEFAULT_TTL_MINUTES
    if isinstance(minutes, bool) or not 1 <= minutes <= MAX_TTL_MINUTES:
        raise BusError("TTL must be 1-10080 minutes")
    return minutes


def _check_minutes(check: str | None, ttl: int) -> int:
    """Parse the holder's release estimate; it must fall within the lease."""
    if check is None:
        raise BusError(f"Estimate when you can release with --check, {CHECK_HELP}")
    match = CHECK_PATTERN.match(check.strip().lower())
    if match is None:
        raise BusError(f"--check must be {CHECK_HELP}")
    minutes = int(match.group(1)) * CHECK_UNITS[match.group(2)]
    if minutes < 1:
        raise BusError("--check must be at least one minute ahead")
    if minutes > ttl:
        raise BusError(f"--check of {minutes} minutes is beyond the lease; raise --ttl")
    return minutes


def _read_lock(db: sqlite3.Connection, key: Key) -> Lock | None:
    row = cast(
        "tuple[str, str | None, str, str, str, str | None] | None",
        db.execute(
            "SELECT holder,reason,acquired_at,expires_at,check_at,checked_at FROM resource_locks "
            "WHERE scope=? AND resource=?",
            key,
        ).fetchone(),
    )
    if row is None:
        return None
    waiters = [
        Waiter(waiter=item[0], reason=item[1], requested_at=item[2], nudged_at=item[3])
        for item in cast(
            "list[tuple[str, str | None, str, str | None]]",
            db.execute(
                "SELECT waiter,reason,requested_at,nudged_at FROM resource_lock_waiters "
                "WHERE scope=? AND resource=? ORDER BY requested_at,rowid",
                key,
            ).fetchall(),
        )
    ]
    return Lock(
        resource=key.resource,
        scope=key.scope,
        holder=row[0],
        reason=row[1],
        acquired_at=row[2],
        expires_at=row[3],
        check_at=row[4],
        checked_at=row[5],
        waiters=waiters,
    )


def _reap(db: sqlite3.Connection) -> list[Notice]:
    """Pass lapsed or abandoned locks to their first waiter, then raise any due checks."""
    expire_idle_agents(db)
    db.execute(
        "DELETE FROM resource_lock_waiters WHERE waiter IN "
        "(SELECT address FROM agents WHERE ended_at IS NOT NULL)"
    )
    stale = [
        (Key(row[0], row[1]), row[2])
        for row in cast(
            "list[tuple[str, str, str]]",
            db.execute(
                "SELECT scope,resource,holder FROM resource_locks "
                "WHERE expires_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "OR holder IN (SELECT address FROM agents WHERE ended_at IS NOT NULL)"
            ).fetchall(),
        )
    ]
    notices: list[Notice] = []
    for key, holder in stale:
        notices.extend(_hand_off(db, key, holder))
    notices.extend(_due_checks(db))
    return notices


def _due_checks(db: sqlite3.Connection) -> list[Notice]:
    """Ask each holder whose estimate has passed, once per estimate, in its own name."""
    due = [
        Key(row[0], row[1])
        for row in cast(
            "list[tuple[str, str]]",
            db.execute(
                "SELECT scope,resource FROM resource_locks "
                "WHERE check_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "AND (checked_at IS NULL OR checked_at < check_at)"
            ).fetchall(),
        )
    ]
    notices: list[Notice] = []
    for key in due:
        db.execute(
            "UPDATE resource_locks SET checked_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE scope=? AND resource=?",
            key,
        )
        lock = _read_lock(db, key)
        if lock is not None:
            notices.append(Notice(lock["holder"], _check_body(lock), sender=lock["holder"]))
    return notices


def _hand_off(db: sqlite3.Connection, key: Key, previous: str) -> list[Notice]:
    """Grant the resource to the earliest live waiter, which must then set its own estimate."""
    first = cast(
        "tuple[str, str | None] | None",
        db.execute(
            "SELECT w.waiter,w.reason FROM resource_lock_waiters w JOIN agents a "
            "ON a.address=w.waiter WHERE w.scope=? AND w.resource=? AND a.ended_at IS NULL "
            "ORDER BY w.requested_at,w.rowid LIMIT 1",
            key,
        ).fetchone(),
    )
    if first is None:
        db.execute("DELETE FROM resource_locks WHERE scope=? AND resource=?", key)
        return []
    # Until the new holder sets --check, the grant message itself is the first ask.
    db.execute(
        "UPDATE resource_locks SET holder=?,reason=?,"
        "acquired_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),"
        "expires_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?),"
        "check_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),"
        "checked_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
        "WHERE scope=? AND resource=?",
        (first[0], first[1], f"+{DEFAULT_TTL_MINUTES} minutes", *key),
    )
    db.execute(
        "DELETE FROM resource_lock_waiters WHERE scope=? AND resource=? AND waiter=?",
        (*key, first[0]),
    )
    lock = _read_lock(db, key)
    if lock is None:
        raise RuntimeError("Handed-off lock could not be read")
    return [Notice(first[0], _grant_body(lock, previous))]


def schedule_check(key: Key) -> None:
    """Start a detached timer that asks the holder at its estimated release time.

    The child forks the timer and exits at once, so this returns without waiting for it.
    Without a timer, the next lock command or hook of any session raises the check.
    """
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # noqa: S603 - fixed argument list, no shell
            [sys.executable, "-m", "llm_bus", "lock", "timer", key.scope, key.resource],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            check=False,
            timeout=TIMER_START_SECONDS,
        )


def detach() -> bool:
    """Fork into a new session; return True in the parent, which should exit immediately."""
    if os.fork():
        return True
    os.setsid()
    return False


def run_timer(store: Store, key: Key, *, sleep: Callable[[float], None] = time.sleep) -> None:
    """Sleep until the lock's current estimate, re-reading it, then raise the check once."""
    while True:
        with store.transaction() as db:
            row = cast(
                "tuple[str, str | None, str, float] | None",
                db.execute(
                    "SELECT holder,checked_at,check_at,"
                    "(julianday(check_at)-julianday('now'))*86400 FROM resource_locks "
                    "WHERE scope=? AND resource=?",
                    key,
                ).fetchone(),
            )
        if row is None:
            return
        holder, checked_at, check_at, remaining = row
        if checked_at is not None and checked_at >= check_at:
            return
        if remaining > 0:
            sleep(min(remaining, TIMER_POLL_SECONDS))
            continue
        with store.transaction() as db:
            project = cast(
                "tuple[str] | None",
                db.execute("SELECT project FROM agents WHERE address=?", (holder,)).fetchone(),
            )
        Locks(store, holder, project[0] if project else "/").status(key, every_scope=True)
        return


def _flag(lock: Lock) -> str:
    return " --shared" if lock["scope"] == SHARED_SCOPE else ""


def _request_body(lock: Lock, waiter: str, reason: str | None) -> str:
    why = f" Their reason: {reason}." if reason else ""
    return (
        f"llm-bus lock request: {waiter} is waiting for resource {lock['resource']!r} "
        f"that you hold.{why} If you no longer need it, run "
        f"`llm-bus lock release {lock['resource']}{_flag(lock)}` and it passes to them. "
        "If you still need it, run "
        f"`llm-bus lock acquire {lock['resource']}{_flag(lock)} --check DURATION` with your "
        "best estimate of when you can release, such as 30m; you will be asked again then."
    )


def _check_body(lock: Lock) -> str:
    queue = (
        f" {len(lock['waiters'])} session(s) are waiting for it."
        if lock["waiters"]
        else " Nobody is waiting yet."
    )
    return (
        f"llm-bus lock check: you estimated resource {lock['resource']!r} would be releasable "
        f"by {lock['check_at']}.{queue} If your task no longer needs it, run "
        f"`llm-bus lock release {lock['resource']}{_flag(lock)}`. If you still need it, run "
        f"`llm-bus lock acquire {lock['resource']}{_flag(lock)} --check DURATION` with a new "
        f"estimate, such as 30m; you will be asked again then. Your lease ends at "
        f"{lock['expires_at']}."
    )


def _grant_body(lock: Lock, previous: str) -> str:
    return (
        f"llm-bus lock granted: you now hold resource {lock['resource']!r} after {previous} "
        "released it or its lease ended. Set when you expect to release by running "
        f"`llm-bus lock acquire {lock['resource']}{_flag(lock)} --check DURATION`, such as "
        "30m; you will be asked then. Run "
        f"`llm-bus lock release {lock['resource']}{_flag(lock)}` when done. "
        f"Your lease ends at {lock['expires_at']}."
    )
