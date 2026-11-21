"""Offline inventories cannot activate maintenance or disclose content."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from llm_bus.cli.app import main
from llm_bus.errors import BusError
from llm_bus.mailbox.preview import preview
from llm_bus.mailbox.store import Store

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 11, 21, 12, tzinfo=UTC)


@pytest.mark.parametrize("legacy", [False, True])
def test_preview_snapshot_boundaries_and_no_body_reads(tmp_path: Path, *, legacy: bool) -> None:
    path = tmp_path / "mailbox.sqlite3"
    # Minimal historical schema also proves preview does not require current metadata.
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE messages(sent_at TEXT, body TEXT)")
        db.executemany(
            "INSERT INTO messages VALUES (?, 'private body')",
            [
                ("2026-11-07T14:00:00+02:00",),
                ("2026-11-07T11:59:59.999Z",),
                ("2026-11-07T12:00:00.001Z",),
            ],
        )
        if not legacy:
            db.execute("CREATE TABLE remote_monitor(envelope TEXT)")
            db.execute(
                "INSERT INTO remote_monitor VALUES (?)",
                (json.dumps({"sent_at": "2026-11-06T12:00:00Z", "body": "monitor secret"}),),
            )
        before = list(db.iterdump())
    result = preview(path, now=NOW)
    assert result == {
        "as_of": "2026-11-21T12:00:00.000Z",
        "retention_cutoff": "2026-11-07T12:00:00.000Z",
        "messages": {"candidates": 2, "oldest_eligible_at": "2026-11-07T11:59:59.999Z"},
        "monitors": {
            "candidates": 0 if legacy else 1,
            "oldest_eligible_at": None if legacy else "2026-11-06T12:00:00.000Z",
        },
    }
    assert "private body" not in json.dumps(result)
    assert "monitor secret" not in json.dumps(result)
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("age", ["invalid secret", "2026-02-30T00:00:00Z", "2026-11-21"])
def test_preview_invalid_age_stops_without_content(tmp_path: Path, age: str) -> None:
    path = tmp_path / "mailbox.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE messages(sent_at TEXT)")
        db.execute("INSERT INTO messages VALUES (?)", (age,))
    with pytest.raises(BusError, match="Invalid message retention timestamp") as error:
        preview(path, now=NOW)
    assert age not in str(error.value)


def test_preview_cli_no_identity_migration_or_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path / "mailbox.sqlite3")
    monkeypatch.setenv("LLM_BUS_DB", str(store.path))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Preview attempted identity, migration, or maintenance")

    monkeypatch.setattr("llm_bus.cli.app.host_identity", forbidden)
    monkeypatch.setattr("llm_bus.mailbox.connection.ensure_schema", forbidden)
    monkeypatch.setattr("llm_bus.mailbox.retention.maintain", forbidden)
    assert main(["retention-preview"]) == 0
    assert json.loads(capsys.readouterr().out)["messages"]["candidates"] == 0
    missing = tmp_path / "absent" / "mailbox.sqlite3"
    monkeypatch.setenv("LLM_BUS_DB", str(missing))
    assert main(["retention-preview"]) == 1
    assert not missing.parent.exists()
