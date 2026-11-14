"""Schema 13 adds the delegation tables additively and preserves existing rows."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.schema import SCHEMA_VERSION
from llm_bus.mailbox.store import Store
from tests.coordination.test_enrollment import create, invoke

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

NEW_TABLES = (
    "coordination_delegations",
    "coordination_grant_delegations",
    "coordination_claims",
    "coordination_approvals",
    "coordination_gate_records",
    "coordination_amendments",
    "coordination_instructions",
    "coordination_owner_turns",
    "coordination_decisions",
)
KEPT = (
    "SELECT * FROM coordination_projects",
    "SELECT * FROM coordination_grants",
    "SELECT * FROM messages",
    "SELECT * FROM resource_locks",
)


def rows(db: sqlite3.Connection) -> list[list[tuple[object, ...]]]:
    return [db.execute(query).fetchall() for query in KEPT]


def count(db: sqlite3.Connection, table: str) -> int:
    row = cast("tuple[int]", db.execute(f"SELECT count(*) FROM {table}").fetchone())  # noqa: S608
    return row[0]


def test_version_is_15() -> None:
    assert SCHEMA_VERSION == 16


def test_fresh_store_has_empty_tables_after_base_commands(tmp_path: Path) -> None:
    mailbox = Store(tmp_path / "bus.db")
    mailbox.register("codex", "one", str(tmp_path))
    mailbox.register("claude", "two", str(tmp_path))
    mailbox.send(SenderContext("codex", "one", str(tmp_path)), "claude:two", "hello")
    with mailbox.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert {name: count(db, name) for name in NEW_TABLES} == dict.fromkeys(NEW_TABLES, 0)


@pytest.mark.parametrize("version", [12, 11])
def test_upgrade_creates_tables_and_preserves_rows(tmp_path: Path, version: int) -> None:
    mailbox = Store(tmp_path / "bus.db")
    create(mailbox, tmp_path)
    invoke(mailbox, tmp_path, "join", "demo", address="claude:worker")
    mailbox.send(SenderContext("codex", "owner", str(tmp_path)), "claude:worker", "kept")
    with mailbox.transaction() as db:
        db.execute(
            "INSERT INTO resource_locks VALUES ('s','db','codex:owner','r','t','2099-01-01','t',NULL)"
        )
        before = rows(db)
        for table in NEW_TABLES:
            db.execute(f"DROP TABLE {table}")
        db.execute(f"PRAGMA user_version={version}")
    assert all(before)

    reopened = Store(mailbox.path)
    with reopened.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert {name: count(db, name) for name in NEW_TABLES} == dict.fromkeys(NEW_TABLES, 0)
        assert rows(db) == before
