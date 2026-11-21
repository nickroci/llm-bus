"""mailbox."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest

from llm_bus.cli.app import main
from llm_bus.errors import BusError
from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.store import Store

from .support import linked_worktree

if TYPE_CHECKING:
    from pathlib import Path


def test_messages_survive_reopen_until_recipient_acknowledges(tmp_path: Path) -> None:
    """Both providers share mailbox; reads never consume messages."""
    path = tmp_path / "bus.sqlite3"
    store = Store(path)
    codex = store.register("codex", "codex-session", "/project")
    claude = store.register("claude", "claude-session", "/project")

    sent = store.send(
        SenderContext("codex", "codex-session", "/project"), claude, "Review migration"
    )
    assert sent["sender_cwd"] == "/project"
    assert sent["recipient_cwd"] == "/project"
    reopened = Store(path)
    assert reopened.inbox(claude) == [sent]
    assert reopened.inbox(claude) == [sent]
    assert reopened.inbox(codex) == []
    with pytest.raises(BusError, match="do not belong"):
        reopened.acknowledge(codex, [sent["id"]])
    assert reopened.acknowledge(claude, [sent["id"]]) == 1
    assert reopened.acknowledge(claude, [sent["id"]]) == 0
    assert Store(path).inbox(claude) == []


def test_unknown_recipient_and_oversized_message_are_rejected(tmp_path: Path) -> None:
    """Typos and unbounded payloads cannot silently fill mailbox."""
    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("codex", "two", "/project")
    with pytest.raises(BusError, match="Unknown recipient"):
        store.send(SenderContext("claude", "one", "/project"), "codex:missing", "hello")
    with pytest.raises(BusError, match="64 KiB"):
        store.send(SenderContext("claude", "one", "/project"), recipient, "x" * 65537)
    assert store.inbox(recipient) == []


def test_reply_requires_participants_and_repository(tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    one = SenderContext("codex", "one", "/project")
    two = SenderContext("claude", "two", "/project")
    store.register(*two)
    store.register("claude", "other", "/project")
    parent = store.send(one, "claude:two", "question")
    reply = store.send(two, "codex:one", "answer", reply_to=parent["id"])
    assert reply["reply_to"] == parent["id"]
    assert store.inbox("codex:one")[0]["reply_to"] == parent["id"]
    for parent_id in (0, True, 999):
        with pytest.raises(BusError):
            store.send(two, "codex:one", "bad", reply_to=parent_id)
    with pytest.raises(BusError, match="participant"):
        store.send(one, "claude:other", "bad", reply_to=parent["id"])
    store.register("codex", "one", "/moved")
    with pytest.raises(BusError, match="scope"):
        store.send(two, "codex:one", "bad", cross_folder=True, reply_to=parent["id"])
    with store.transaction() as db:
        db.execute("UPDATE messages SET sender_scope=NULL WHERE id=?", (parent["id"],))
    with pytest.raises(BusError, match="scope"):
        store.send(two, "codex:one", "bad", cross_folder=True, reply_to=parent["id"])


def test_linked_worktrees_share_routing_scope_but_keep_their_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pylint: disable=too-many-locals
    # Separate paths and agents make the routing and historical-folder assertions explicit.
    repo, worktree, git = linked_worktree(tmp_path)
    unrelated = tmp_path / "unrelated"
    subprocess.run([git, "init", "-q", str(unrelated)], check=True)
    nested = worktree / "nested"
    nested.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(worktree))

    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("claude", "worktree", str(nested))
    foreign = store.register("claude", "foreign", str(unrelated))
    assert recipient in {agent["address"] for agent in store.agents(str(repo))}
    assert foreign not in {agent["address"] for agent in store.agents(str(repo))}

    sender = SenderContext("codex", "main", str(repo))
    sent = store.send(sender, recipient, "same repository")
    assert sent["sender_cwd"] == str(repo)
    assert sent["recipient_cwd"] == str(nested)
    with pytest.raises(BusError, match="--cross-folder"):
        store.send(sender, foreign, "other repository")

    worktree_sender = SenderContext("codex", "worktree-sender", str(nested))
    sent_within_worktree = store.send(worktree_sender, recipient, "worktree-only")
    assert sent_within_worktree["id"] in {
        item["id"] for item in store.history(folder=None, repository=str(repo))
    }
    assert sent_within_worktree["id"] not in {
        item["id"] for item in store.history(folder=str(repo))
    }
    legacy_path = tmp_path / "legacy-messages.sqlite3"
    with sqlite3.connect(legacy_path) as db:
        db.executescript(
            "CREATE TABLE agents (address TEXT PRIMARY KEY, kind TEXT, project TEXT, last_seen TEXT);"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, sender TEXT, recipient TEXT, "
            "body TEXT, sender_cwd TEXT, recipient_cwd TEXT, sent_at TEXT, acknowledged_at TEXT);"
        )
        db.executemany(
            "INSERT INTO agents VALUES (?, 'codex', ?, 'now')",
            [("codex:old-sender", str(nested)), ("codex:old-recipient", str(nested))],
        )
        db.execute(
            "INSERT INTO messages VALUES (1, 'codex:old-sender', 'codex:old-recipient', "
            "'old worktree message', ?, ?, ?, NULL)",
            (str(nested), str(nested), datetime.now(UTC).isoformat()),
        )
    migrated = Store(legacy_path)
    subprocess.run(
        [git, "-C", str(repo), "worktree", "remove", "--force", str(worktree)], check=True
    )
    assert sent_within_worktree["id"] in {
        item["id"] for item in store.history(folder=None, repository=str(repo))
    }
    assert [item["id"] for item in migrated.history(folder=None, repository=str(repo))] == [1]


def test_mixed_acknowledgement_batch_rolls_back(tmp_path: Path) -> None:
    """A foreign message ID cannot consume an earlier valid item in the same batch."""
    store = Store(tmp_path / "bus.sqlite3")
    first = store.register("codex", "first", "/project")
    second = store.register("codex", "second", "/project")
    own_message = store.send(SenderContext("claude", "one", "/project"), first, "one")
    foreign_message = store.send(SenderContext("claude", "one", "/project"), second, "two")
    with pytest.raises(BusError, match="do not belong"):
        store.acknowledge(first, [own_message["id"], foreign_message["id"]])
    assert store.inbox(first) == [own_message]


def test_cli_filters_peers_and_requires_cross_folder_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("LLM_BUS_DB", str(tmp_path / "bus.sqlite3"))
    monkeypatch.setenv("CODEX_THREAD_ID", "codex-one")

    wakes: list[tuple[str, int, str]] = []

    def fake_wake(_recipient: str, _message_id: int, sender: str) -> dict[str, str]:
        wakes.append((_recipient, _message_id, sender))
        return {"status": "requested"}

    monkeypatch.setattr("llm_bus.wake.dispatch.wake", fake_wake)
    monkeypatch.chdir(first)
    assert main(["whoami"]) == 0
    capsys.readouterr()
    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-two")
    monkeypatch.chdir(second)
    assert main(["whoami"]) == 0
    capsys.readouterr()
    assert main(["list"]) == 0
    assert len(cast("list[object]", json.loads(capsys.readouterr().out))) == 1
    assert main(["list", "--all"]) == 0
    assert len(cast("list[object]", json.loads(capsys.readouterr().out))) == 2
    assert main(["send", "codex:codex-one", "--body", "hello"]) == 1
    assert "--cross-folder" in capsys.readouterr().err
    assert wakes == []
    assert main(["send", "codex:codex-one", "--body", "hello", "--cross-folder"]) == 0
    sent = cast("dict[str, object]", json.loads(capsys.readouterr().out))
    assert sent["sender_cwd"] == str(second)
    assert wakes == [("codex:codex-one", sent["id"], "claude:claude-two")]


def test_monitor_without_native_identity_validates_and_does_not_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bus.sqlite3"
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    monkeypatch.setenv("LLM_BUS_CONFIG", str(tmp_path / "absent.toml"))
    for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    assert main(["monitor", "--all"]) == 0
    assert capsys.readouterr().out == ""
    for options in (["--limit", "0"], ["--limit", "101"], ["--before", "0"]):
        assert main(["monitor", *options]) == 1
        assert "Monitor" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["monitor", "--repository", str(tmp_path), "--all"])
    assert Store(path).all_agents() == []


def test_pending_page_stops_after_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code
    # The connection seam measures actual SQLite rows fetched, independently of filtering.
    store = Store(tmp_path / "bounded.sqlite3")
    recipient = store.register("codex", "recipient", "/project")
    sender = store.register("claude", "sender", "/project")
    with store.transaction() as db:
        db.executemany(
            "INSERT INTO messages(sender,recipient,body) VALUES (?,?,?)",
            [(sender, recipient, "body") for _ in range(500)],
        )
    reads: list[int] = []
    original = store._connect  # pyright: ignore[reportPrivateUsage]

    def record(_cursor: sqlite3.Cursor, row: tuple[object, ...]) -> tuple[object, ...]:
        if len(row) == 11:
            reads.append(1)
        return row

    def connect() -> sqlite3.Connection:
        db = original()
        db.row_factory = record
        return db

    monkeypatch.setattr(store, "_connect", connect)
    assert len(store.inbox(recipient, 1)) == 1
    assert len(reads) == 100
    assert store.pending_count(recipient, limit=101) == 101
