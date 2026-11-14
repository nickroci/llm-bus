"""Timer support for advisory resource locks."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from typing import TYPE_CHECKING, cast

from llm_bus.locks.lifecycle import reap
from llm_bus.locks.notices import deliver, persist

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from llm_bus.locks.models import Key
    from llm_bus.mailbox.store import Store


TIMER_POLL_SECONDS = 15 * 60
TIMER_START_SECONDS = 10


def schedule_check(key: Key) -> None:
    """Start a detached timer that asks the holder at its estimated release time.

    The child forks the timer and exits at once, so this returns without waiting for it.
    Without a timer, the next lock command or hook of any session raises the check.
    """
    spawn("lock", "timer", key.scope, key.resource)


def spawn(*command: str) -> None:
    """Run ``llm-bus COMMAND`` in a child that forks its hidden timer and exits at once."""
    spawn_checked(*command)


def spawn_checked(*command: str, database: Path | None = None) -> bool:
    """Report confirmed startup failure; optionally bind the child to an exact mailbox."""
    environment = (
        None if database is None else {**os.environ, "LLM_BUS_DB": str(database.resolve())}
    )
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        result = subprocess.run(  # noqa: S603 - fixed argument list, no shell
            [sys.executable, "-m", "llm_bus", *command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            check=False,
            timeout=TIMER_START_SECONDS,
            env=environment,
        )
        return result.returncode == 0
    return False


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
            db.execute("BEGIN IMMEDIATE")
            sent = persist(store, holder, db, reap(db))
        deliver(store, sent)
        return
