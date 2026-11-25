"""Shared hook wire input and wake coverage assertions."""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm_bus.mailbox.store import Store


def hook_input(payload: object) -> io.StringIO:
    return io.StringIO(json.dumps(payload))


def assert_wake_coverage(store: Store) -> None:
    with store.transaction() as db:
        assert db.execute(
            "SELECT requested_generation,dispatched_generation,token FROM wake_dispatch"
        ).fetchone() == (2, 2, None)
        assert db.execute("SELECT COUNT(*) FROM wake_coverage").fetchone() == (2,)


def prompt_input(session_id: str, cwd: str) -> io.StringIO:
    return hook_input(
        {
            "session_id": session_id,
            "hook_event_name": "UserPromptSubmit",
            "cwd": cwd,
            "prompt": "Continue work",
        }
    )
