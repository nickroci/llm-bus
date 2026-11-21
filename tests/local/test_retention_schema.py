"""Retention migrations and insertion integrity on real SQLite stores."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, cast

import pytest

from llm_bus.coordination.recovery import create_tables
from llm_bus.errors import BusError
from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.retention_schema import allocate_id
from llm_bus.mailbox.store import Store

if TYPE_CHECKING:
    from pathlib import Path


def _legacy(store: Store) -> None:
    with store.transaction() as db:
        for name in ("messages_age_insert", "messages_age_update"):
            db.execute(f"DROP TRIGGER {name}")
        db.execute("DROP INDEX messages_retention")
        db.execute("ALTER TABLE messages DROP COLUMN retention_at")
        db.execute("PRAGMA user_version=18")


def test_legacy_ages_and_durable_reference_seed(tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("codex", "recipient", "/project")
    sent = store.send(SenderContext("claude", "sender", "/project"), recipient, "retained")
    _legacy(store)
    with store.transaction() as db:
        create_tables(db)
        db.execute("UPDATE messages SET sent_at='2026-11-01T02:00:00+02:00'")
        db.execute("INSERT INTO coordination_run_merges VALUES (900,'run','ticket','{}','unknown')")
        db.execute(
            "INSERT INTO remote_monitor(id,envelope,scope,connection) VALUES ('uuid',?,'s','c')",
            (json.dumps({"sent_at": "2026-11-02T04:00:00+04:00", "body": "retained"}),),
        )
    reopened = Store(store.path)
    with reopened.transaction() as db:
        assert db.execute("SELECT retention_at FROM messages").fetchone() == (
            "2026-11-01T00:00:00.000Z",
        )
        assert db.execute("SELECT retention_at FROM remote_monitor").fetchone() == (
            "2026-11-02T00:00:00.000Z",
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        plan = cast(
            "list[tuple[int, int, int, str]]",
            db.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM messages WHERE retention_at<=? "
                "ORDER BY retention_at,id LIMIT 100",
                ("2026-11-10T00:00:00.000Z",),
            ).fetchall(),
        )
        assert any("messages_retention" in row[3] for row in plan)
        assert allocate_id(db, "message") == 901
        assert allocate_id(db, "monitor") == 2
    assert reopened.history(folder=None)[0]["body"] == sent["body"]


@pytest.mark.parametrize("age", ["invalid secret body", "2026-02-30T00:00:00Z", "2026-01-01"])
def test_bad_legacy_age_rolls_back_without_content(tmp_path: Path, age: str) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("codex", "recipient", "/project")
    store.send(SenderContext("claude", "sender", "/project"), "codex:recipient", "secret body")
    _legacy(store)
    with store.transaction() as db:
        db.execute("UPDATE messages SET sent_at=?", (age,))
    with pytest.raises(BusError, match="Invalid message retention timestamp") as error:
        Store(store.path)
    assert "secret body" not in str(error.value)
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (18,)
        columns = cast(
            "list[tuple[int, str, str, int, object, int]]",
            db.execute("PRAGMA table_info(messages)").fetchall(),
        )
        assert "retention_at" not in {row[1] for row in columns}
        assert db.execute("SELECT count(*) FROM messages").fetchone() == (1,)


def test_ids_survive_empty_mailbox_and_parallel_writers(tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("codex", "recipient", "/project")
    sender = SenderContext("claude", "sender", "/project")
    first = store.send(sender, recipient, "first")
    highest = store.send(sender, recipient, "highest")
    with store.transaction() as db:
        db.execute("DELETE FROM messages WHERE id=?", (highest["id"],))
    after_highest = store.send(sender, recipient, "after highest")
    assert after_highest["id"] == highest["id"] + 1
    with store.transaction() as db:
        db.execute("DELETE FROM messages")
        assert allocate_id(db, "monitor") == 1
        db.execute(
            "INSERT INTO remote_monitor VALUES (1,'uuid','{}','s','c','2026-01-01T00:00:00.000Z')"
        )
        db.execute("DELETE FROM remote_monitor")
    with store.transaction() as db:
        assert allocate_id(db, "monitor") == 2

    def send_next(_index: int) -> int:
        return store.send(sender, recipient, "next")["id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(send_next, range(2)))
    assert first["id"] < highest["id"]
    assert sorted(ids) == [after_highest["id"] + 1, after_highest["id"] + 2]


@pytest.mark.parametrize("table", ["remote_imports", "remote_outbox", "remote_monitor"])
def test_purged_uuid_cannot_be_inserted(tmp_path: Path, table: str) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    with store.transaction() as db:
        db.execute("INSERT INTO remote_replay_guard VALUES ('purged')")
    statements = {
        "remote_monitor": "INSERT INTO remote_monitor(id,envelope,scope,connection) "
        "VALUES ('purged','{}','s','c')",
        "remote_outbox": "INSERT INTO remote_outbox(id,local_id,envelope,state) "
        "VALUES ('purged',1,'{}','queued_local')",
        "remote_imports": "INSERT INTO remote_imports(id,local_id,envelope) "
        "VALUES ('purged',1,'{}')",
    }
    with (
        store.transaction() as db,
        pytest.raises(sqlite3.IntegrityError, match="Purged remote message ID"),
    ):
        db.execute(statements[table])
