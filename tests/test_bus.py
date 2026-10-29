"""Focused local mailbox and host identity tests."""

# pylint: disable=too-many-lines
# Installer rollback scenarios share fixtures in this behavior suite.

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, cast

import pytest

from llm_bus import host_identity, main
from llm_bus_dispatch import dispatch_pending
from llm_bus_install import configure_hosts
from llm_bus_schema import SCHEMA_VERSION, ensure_schema
from llm_bus_store import BusError, SenderContext, Store, default_path

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def isolate_codex_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep installer tests in their temporary homes, not the running host settings."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


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


def test_message_status_read_only_without_identity_or_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("claude", "two", "/project")
    sent = store.send(SenderContext("codex", "one", "/project"), recipient, "private body")
    monkeypatch.setenv("LLM_BUS_DB", str(store.path))
    monkeypatch.setenv("LLM_BUS_CONFIG", str(tmp_path / "absent.toml"))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Status attempted registration, migration, network, or wake")

    monkeypatch.setattr("llm_bus.host_identity", forbidden)
    monkeypatch.setattr("llm_bus_store.ensure_schema", forbidden)
    monkeypatch.setattr("llm_bus.require_config", forbidden)
    monkeypatch.setattr("llm_bus_dispatch.wake", forbidden)
    with sqlite3.connect(store.path) as db:
        before = list(db.iterdump())
    assert main(["status", str(sent["id"])]) == 0
    result = cast("dict[str, object]", json.loads(capsys.readouterr().out))
    assert result["transport"] == {
        "status": "stored_local",
        "source": "local",
        "observed_at": sent["sent_at"],
    }
    assert "private body" not in json.dumps(result)
    with sqlite3.connect(store.path) as db:
        assert list(db.iterdump()) == before
    for message_id in (0, -1, 999, 2**100):
        assert main(["status", str(message_id)]) == 1
    assert set(result) == {
        "id",
        "remote_id",
        "sender",
        "recipient",
        "reply_to",
        "transport",
        "wake",
        "acknowledged_at",
        "work_report",
        "synchronization",
    }
    missing = tmp_path / "missing" / "bus.sqlite3"
    monkeypatch.setenv("LLM_BUS_DB", str(missing))
    assert main(["status", "1"]) == 1
    assert not missing.parent.exists()


def test_message_status_legacy_missing_metadata(tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("claude", "two", "/project")
    sent = store.send(SenderContext("codex", "one", "/project"), recipient, "legacy")
    with store.transaction() as db:
        for table in ("remote_sync_work", "wake_dispatch", "wake_coverage", "remote_reports"):
            db.execute(f"DROP TABLE {table}")
        db.execute("DROP INDEX messages_reply_to")
        db.execute("ALTER TABLE messages DROP COLUMN reply_to")
    result = store.message_status(sent["id"])
    assert result["reply_to"] is None
    assert result["wake"] == dict.fromkeys(
        ("status", "via", "reason", "checked_at", "coverage_status", "retry_at")
    )
    assert result["work_report"] is None
    assert cast("dict[str, object]", result["synchronization"])["stale"] is True


def test_message_status_sanitizes_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("claude", "two", "/project")
    sent = store.send(SenderContext("codex", "one", "/project"), recipient, "diagnose")
    with store.transaction() as db:
        db.execute(
            "UPDATE messages SET wake_status='failed',wake_via='codex queue',wake_reason=?",
            ("\x1b[31m secret /private/credential.token HTTP body",),
        )
    result = store.message_status(sent["id"])
    assert cast("dict[str, object]", result["wake"])["reason"] == "wake_failed:codex_queue"
    assert "secret" not in json.dumps(result)
    assert "credential" not in json.dumps(result)

    def unsafe_adapter(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"status": "failed", "via": "codex queue", "reason": "\x1b[31m secret credential"}

    monkeypatch.setattr("llm_bus_dispatch.wake", unsafe_adapter)
    dispatch_pending(store, recipient)
    assert store.history(folder=None)[0]["wake_reason"] == "wake_failed:codex_queue"


def test_message_status_never_borrows_other_message_coverage(tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("claude", "two", "/project")
    sender = SenderContext("codex", "one", "/project")
    first = store.send(sender, recipient, "first")
    second = store.send(sender, recipient, "second")
    with store.transaction() as db:
        db.execute(
            "INSERT INTO wake_coverage VALUES (?,'test','requested','2026-10-23T00:00:00Z')",
            (first["id"],),
        )
    wake = cast("dict[str, object]", store.message_status(second["id"])["wake"])
    assert wake["coverage_status"] is None
    assert wake["status"] is None


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


def test_conversation_filter_preserves_scope_and_paging(tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    one = SenderContext("codex", "one", "/one")
    two = SenderContext("claude", "two", "/two")
    store.register(*two)
    root = store.send(one, "claude:two", "root", cross_folder=True)
    child = store.send(two, "codex:one", "child", cross_folder=True, reply_to=root["id"])
    leaf = store.send(one, "claude:two", "leaf", cross_folder=True, reply_to=child["id"])
    store.send(one, "claude:two", "unrelated", cross_folder=True)
    rows = store.history(folder="/two", conversation=child["id"], before=leaf["id"], limit=1)
    assert [row["id"] for row in rows] == [child["id"]]
    assert store.history(folder="/other", conversation=root["id"]) == []
    assert [row["id"] for row in store.history(folder=None, conversation=leaf["id"], after=0)] == [
        root["id"],
        child["id"],
        leaf["id"],
    ]
    for message_id in (0, True, 999):
        with pytest.raises(BusError):
            store.history(folder=None, conversation=message_id)
    with store.transaction() as db:
        db.execute("UPDATE messages SET reply_to=? WHERE id=?", (leaf["id"], root["id"]))
    with pytest.raises(BusError, match="cycle"):
        store.history(folder=None, conversation=root["id"])


def test_peer_filter_matches_both_directions(tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    one = SenderContext("codex", "one", "/project")
    two = SenderContext("claude", "two", "/project")
    store.register(*two)
    root = store.send(one, "claude:two", "out")
    child = store.send(two, "codex:one", "in", reply_to=root["id"])
    assert [row["id"] for row in store.history(folder=None, peer="codex:one")] == [
        child["id"],
        root["id"],
    ]
    assert store.history(folder=None, conversation=root["id"], peer="codex:on") == []


def test_folder_scope_requires_explicit_cross_folder_send(tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    sender = store.register("codex", "one", "/one")
    same = store.register("claude", "two", "/one")
    other = store.register("claude", "three", "/other")
    assert {peer["address"] for peer in store.agents("/one")} == {sender, same}
    assert len(store.agents()) == 3
    # A concurrent hook can register the same sender in another folder before send begins.
    store.register("codex", "one", "/other")
    with pytest.raises(BusError, match="--cross-folder"):
        store.send(SenderContext("codex", "one", "/one"), other, "hello")
    sent = store.send(SenderContext("codex", "one", "/one"), other, "hello", cross_folder=True)
    assert sent["sender_cwd"] == "/one"
    assert sent["recipient_cwd"] == "/other"
    store.register("codex", "one", "/new-location")
    store.register("claude", "three", "/new-location")
    assert store.inbox(other)[0]["sender_cwd"] == "/one"
    assert store.inbox(other)[0]["recipient_cwd"] == "/other"


def test_linked_worktrees_share_routing_scope_but_keep_their_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pylint: disable=too-many-locals
    # Separate paths and agents make the routing and historical-folder assertions explicit.
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required for worktree routing")
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    unrelated = tmp_path / "unrelated"
    subprocess.run([git, "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            git,
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    subprocess.run(
        [git, "-C", str(repo), "worktree", "add", "-q", "--detach", str(worktree)],
        check=True,
    )
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
            "'old worktree message', ?, ?, 'now', NULL)",
            (str(nested), str(nested)),
        )
    migrated = Store(legacy_path)
    subprocess.run(
        [git, "-C", str(repo), "worktree", "remove", "--force", str(worktree)], check=True
    )
    assert sent_within_worktree["id"] in {
        item["id"] for item in store.history(folder=None, repository=str(repo))
    }
    assert [item["id"] for item in migrated.history(folder=None, repository=str(repo))] == [1]


def test_existing_database_gets_nullable_sender_folder(tmp_path: Path) -> None:
    path = tmp_path / "bus.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE agents (address TEXT PRIMARY KEY, kind TEXT, project TEXT, last_seen TEXT);"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, sender TEXT, recipient TEXT, "
            "body TEXT, sent_at TEXT, acknowledged_at TEXT);"
            "INSERT INTO agents VALUES ('codex:one', 'codex', '/one', 'now');"
            "INSERT INTO agents VALUES ('claude:two', 'claude', '/one', 'now');"
            "INSERT INTO messages VALUES (1, 'codex:one', 'claude:two', 'old', 'now', NULL);"
        )
    migrated = Store(path)
    assert migrated.inbox("claude:two")[0]["sender_cwd"] is None
    assert migrated.inbox("claude:two")[0]["recipient_cwd"] is None
    assert migrated.history(folder=None)[0]["wake_status"] is None
    assert migrated.all_agents()[0]["ended_at"] is None
    assert len(migrated.agents("/one")) == 2


def test_existing_git_worktree_agents_get_repository_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required for worktree routing")
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    subprocess.run([git, "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            git,
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    subprocess.run(
        [git, "-C", str(repo), "worktree", "add", "-q", "--detach", str(worktree)],
        check=True,
    )
    nested = worktree / "nested"
    nested.mkdir()
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE agents (address TEXT PRIMARY KEY, kind TEXT, project TEXT, "
            "last_seen TEXT NOT NULL, ended_at TEXT)"
        )
        db.executemany(
            "INSERT INTO agents VALUES (?, 'codex', ?, 'now', NULL)",
            [("codex:main", str(repo)), ("codex:linked", str(nested))],
        )

    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(worktree))
    migrated = Store(path)
    assert {agent["address"] for agent in migrated.agents(str(repo))} == {
        "codex:main",
        "codex:linked",
    }
    sent = migrated.send(SenderContext("codex", "main", str(repo)), "codex:linked", "hello")
    assert sent["recipient_cwd"] == str(nested)


def test_session_end_hides_peer_but_preserves_pending_message(tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("codex", "recipient", "/project")
    sent = store.send(SenderContext("claude", "sender", "/project"), recipient, "hello")

    store.end_session("codex", "recipient")
    assert recipient not in {agent["address"] for agent in store.agents("/project")}
    assert store.is_ended(recipient)
    ended = next(agent for agent in store.all_agents() if agent["address"] == recipient)
    assert ended["ended_at"] is not None
    assert store.inbox(recipient) == [sent]

    store.register("codex", "recipient", "/project")
    assert recipient in {agent["address"] for agent in store.agents("/project")}
    assert not store.is_ended(recipient)


def test_idle_session_closes_after_24_hours_and_reopens_on_registration(tmp_path: Path) -> None:
    path = tmp_path / "bus.sqlite3"
    store = Store(path)
    recipient = store.register("codex", "recipient", "/project")
    sent = store.send(SenderContext("claude", "sender", "/project"), recipient, "hello")
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE agents SET last_seen = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-23 hours') "
            "WHERE address = ?",
            (recipient,),
        )
    assert recipient in {agent["address"] for agent in store.agents("/project")}
    assert not store.is_ended(recipient)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE agents SET last_seen = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-25 hours') "
            "WHERE address = ?",
            (recipient,),
        )

    assert recipient not in {agent["address"] for agent in store.agents("/project")}
    assert store.is_ended(recipient)
    ended = next(agent for agent in store.all_agents() if agent["address"] == recipient)
    assert ended["ended_at"] is not None
    assert store.inbox(recipient) == [sent]

    store.register("codex", "recipient", "/project")
    assert recipient in {agent["address"] for agent in store.agents("/project")}
    assert not store.is_ended(recipient)
    assert store.inbox(recipient) == [sent]


def test_default_database_path_uses_windows_local_app_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_BUS_DB", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("llm_bus_store.sys.platform", "win32")
    assert default_path() == tmp_path / "llm-bus" / "bus.sqlite3"
    monkeypatch.delenv("LOCALAPPDATA")
    monkeypatch.setattr("llm_bus_store.Path.home", lambda: tmp_path)
    assert default_path() == tmp_path / "AppData" / "Local" / "llm-bus" / "bus.sqlite3"


def test_history_preserves_status_and_send_time_folders(tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("claude", "two", "/other")
    first = store.send(
        SenderContext("codex", "one", "/first"), recipient, "first", cross_folder=True
    )
    second = store.send(
        SenderContext("codex", "one", "/first"), recipient, "second", cross_folder=True
    )
    unrelated = store.register("codex", "three", "/third")
    third = store.send(SenderContext("claude", "four", "/third"), unrelated, "third")
    store.acknowledge(recipient, [first["id"]])
    store.register("claude", "two", "/moved")

    assert [item["id"] for item in store.history(folder="/first")] == [second["id"], first["id"]]
    assert [item["id"] for item in store.history(folder="/other")] == [second["id"], first["id"]]
    assert store.history(folder="/moved") == []
    assert [item["id"] for item in store.history(folder=None, repository="/first")] == [
        second["id"],
        first["id"],
    ]
    assert [item["id"] for item in store.history(folder=None, limit=2)] == [
        third["id"],
        second["id"],
    ]
    assert [item["id"] for item in store.history(folder=None, before=third["id"])] == [
        second["id"],
        first["id"],
    ]
    assert [item["id"] for item in store.history(folder=None, after=first["id"])] == [
        second["id"],
        third["id"],
    ]
    assert store.history(folder="/first")[1]["acknowledged_at"] is not None
    assert store.history(folder="/first")[0]["acknowledged_at"] is None
    assert store.history(folder="/first")[0]["wake_status"] is None
    assert store.inbox(recipient) == [second]


def test_history_cli_needs_no_agent_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    monkeypatch.setenv("LLM_BUS_DB", str(tmp_path / "bus.sqlite3"))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("claude", "two", str(folder))
    sent = store.send(SenderContext("codex", "one", str(folder)), recipient, "hello")
    monkeypatch.chdir(tmp_path)

    assert main(["history"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["history", "--folder", str(folder)]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == sent["id"]
    assert main(["history", "--repository", str(folder)]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == sent["id"]
    assert main(["history", "--all", "--after", "0"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["recipient_cwd"] == str(folder)
    assert main(["history", "--all", "--conversation", str(sent["id"]), "--peer", recipient]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == sent["id"]
    assert main(["history", "--all", "--conversation", "0"]) == 1
    assert "positive integer" in capsys.readouterr().err
    assert main(["agents"]) == 0
    agents = cast("list[dict[str, object]]", json.loads(capsys.readouterr().out))
    assert {agent["address"] for agent in agents} == {"codex:one", "claude:two"}
    assert {agent["project"] for agent in agents} == {str(folder)}
    assert main(["history", "--all", "--limit", "0"]) == 1
    assert "History limit" in capsys.readouterr().err


def test_reply_schema_upgrade_preserves_legacy_roots(tmp_path: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    store.register("claude", "two", "/project")
    store.send(SenderContext("codex", "one", "/project"), "claude:two", "retained")
    with store.transaction() as db:
        db.execute("DROP INDEX messages_reply_to")
        db.execute("ALTER TABLE messages DROP COLUMN reply_to")
        db.execute("PRAGMA user_version=3")
    reopened = Store(store.path)
    assert reopened.history(folder=None)[0]["reply_to"] is None
    with reopened.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name='messages_reply_to'"
        ).fetchone()


def test_send_cli_reply_uses_local_parent_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bus.sqlite3"
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    monkeypatch.setenv("CODEX_THREAD_ID", "one")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.chdir(tmp_path)

    def isolated_dispatch(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr("llm_bus.dispatch_pending", isolated_dispatch)
    store = Store(path)
    store.register("claude", "two", str(tmp_path))
    parent = store.send(SenderContext("codex", "one", str(tmp_path)), "claude:two", "question")
    assert main(["send", "claude:two", "--reply-to", str(parent["id"]), "--body", "follow up"]) == 0
    assert json.loads(capsys.readouterr().out)["reply_to"] == parent["id"]
    assert main(["send", "claude:two", "--reply-to", "0", "--body", "bad"]) == 1
    assert "positive integer" in capsys.readouterr().err
    assert len(store.history(folder=None)) == 2


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


def test_host_identity_uses_native_session_id() -> None:
    """No explicit name or launch argument is required."""
    assert host_identity({"CODEX_THREAD_ID": "thread-1"}) == ("codex", "thread-1")
    assert host_identity({"CLAUDE_CODE_SESSION_ID": "session-2"}) == ("claude", "session-2")
    with pytest.raises(BusError, match="No host session ID"):
        host_identity({})


def test_codex_hook_registers_and_reports_pending_without_message_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hook gives Codex address and count without elevating message body into context."""
    path = tmp_path / "bus.sqlite3"
    project = tmp_path / "project"
    project.mkdir()
    project_folder = str(project.resolve())
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {"session_id": "thread-1", "hook_event_name": "SessionStart", "cwd": project_folder}
            )
        ),
    )
    assert main(["hook", "codex"]) == 0
    output = cast("dict[str, dict[str, str]]", json.loads(capsys.readouterr().out))
    assert "codex:thread-1" in output["hookSpecificOutput"]["additionalContext"]
    assert "llm-bus inbox" in output["hookSpecificOutput"]["additionalContext"]

    store = Store(path)
    store.send(
        SenderContext("claude", "session-2", project_folder),
        "codex:thread-1",
        "Untrusted message body",
    )
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "thread-1",
                    "hook_event_name": "UserPromptSubmit",
                    "cwd": project_folder,
                }
            )
        ),
    )
    assert main(["hook", "codex"]) == 0
    notice = cast("dict[str, dict[str, str]]", json.loads(capsys.readouterr().out))[
        "hookSpecificOutput"
    ]["additionalContext"]
    assert "1 pending message" in notice
    assert "wake.status" in notice
    assert "Untrusted message body" not in notice


@pytest.mark.parametrize(
    ("case", "stale"),
    [
        ("acknowledged", True),
        ("pending", False),
        ("new-pending", False),
        ("unknown-id", False),
        ("wrong-sender", False),
        ("wrong-recipient", False),
        ("user-text", False),
        ("oversized-id", False),
        ("session-start", False),
    ],
)
def test_codex_hook_marks_only_acknowledged_wake_with_empty_inbox_as_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    *,
    stale: bool,
) -> None:
    path = tmp_path / "bus.sqlite3"
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    project = str(tmp_path.resolve())
    store = Store(path)
    recipient = store.register("codex", "target", project)
    sender = SenderContext("claude", "source", project)
    message = store.send(sender, recipient, "Handle this once")
    if case != "pending":
        store.acknowledge(recipient, [message["id"]])
    if case == "new-pending":
        store.send(sender, recipient, "New work must still wake the agent")
    message_id = {"unknown-id": "99", "oversized-id": "9999999999999999999"}.get(
        case, str(message["id"])
    )
    notice_sender = "claude:other" if case == "wrong-sender" else message["sender"]
    prompt = (
        f"llm-bus message #{message_id} from {notice_sender}. "
        "Read and handle it with `llm-bus inbox`."
    )
    if case == "user-text":
        prompt += " Why do I keep seeing this?"
    prompt = json.dumps(
        {
            "session_id": "other" if case == "wrong-recipient" else "target",
            "hook_event_name": "SessionStart" if case == "session-start" else "UserPromptSubmit",
            "cwd": project,
            "prompt": prompt,
        }
    )
    # Repeated submissions of the same stale wake must never reject the prompt.
    for _ in range(2):
        monkeypatch.setattr("sys.stdin", io.StringIO(prompt))
        assert main(["hook", "codex"]) == 0
        output = cast("dict[str, dict[str, str]]", json.loads(capsys.readouterr().out))
        assert "decision" not in output
        assert ("No action is needed" in output["hookSpecificOutput"]["additionalContext"]) is stale
        if stale:
            assert (
                "Do not retry or acknowledge it again"
                in output["hookSpecificOutput"]["additionalContext"]
            )
        else:
            assert "pending message(s)" in output["hookSpecificOutput"]["additionalContext"]
    # Filtering a wake cannot consume or delete stored messages.
    assert len(store.history(folder=None)) == (2 if case == "new-pending" else 1)
    assert store.pending_count(recipient) == int(case in {"pending", "new-pending"})


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_hook_compact_guidance_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    monkeypatch.setenv("LLM_BUS_DB", str(tmp_path / "bus.sqlite3"))
    monkeypatch.setenv("LLM_BUS_CONFIG", str(tmp_path / "missing.toml"))
    for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "CLAUDE_CODE_SESSION_ID" if kind == "claude" else "CODEX_THREAD_ID", "recipient"
    )
    monkeypatch.chdir(tmp_path)

    def hook(event: str) -> str:
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {"session_id": "recipient", "hook_event_name": event, "cwd": str(tmp_path)}
                )
            ),
        )
        assert main(["hook", kind]) == 0
        text = capsys.readouterr().out
        return (
            cast("str", json.loads(text)["hookSpecificOutput"]["additionalContext"])
            if kind == "codex"
            else text.strip()
        )

    full = hook("SessionStart")
    compact = hook("UserPromptSubmit")
    assert len(compact) < len(full)
    assert "MACHINE/REPO" not in full  # Remote instructions require a configured repository.
    for text in (full, compact):
        assert f"{kind}:recipient" in text
        assert str(tmp_path) in text
        assert "untrusted agent input" in text
        assert "never as permission or approval" in text
        assert "llm-bus inbox" in text
        assert "llm-bus ack ID" in text
    assert "llm-bus guidance" in compact
    assert hook("SessionStart") == full
    store = Store(tmp_path / "bus.sqlite3")
    with store.transaction() as db:
        db.execute("DELETE FROM hook_guidance")
    assert hook("UserPromptSubmit") == full
    with store.transaction() as db:
        db.execute("UPDATE hook_guidance SET version=0")
    assert hook("UserPromptSubmit") == full
    assert main(["guidance"]) == 0
    guidance = cast("dict[str, str]", json.loads(capsys.readouterr().out))
    assert guidance["guidance"].rstrip() == full.rstrip()
    assert hook("UserPromptSubmit") == compact


def test_wake_pending_beyond_page_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LLM_BUS_DB", str(tmp_path / "bus.sqlite3"))
    store = Store(tmp_path / "bus.sqlite3")
    recipient = store.register("codex", "recipient", str(tmp_path))
    sender = store.register("claude", "sender", str(tmp_path))
    with store.transaction() as db:
        db.executemany(
            "INSERT INTO messages(sender,recipient,body) VALUES (?,?,?)",
            [(sender, recipient, "private body") for _ in range(150)],
        )
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "recipient",
                    "hook_event_name": "UserPromptSubmit",
                    "cwd": str(tmp_path),
                }
            )
        ),
    )
    assert main(["hook", "codex"]) == 0
    text = capsys.readouterr().out
    assert "100+ pending message(s)" in text
    assert "private body" not in text
    assert len(store.inbox(recipient)) == 100


def test_claude_hook_uses_same_session_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Claude hook and shell environment resolve to one durable address."""
    monkeypatch.setenv("LLM_BUS_DB", str(tmp_path / "bus.sqlite3"))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps({"session_id": "claude-session", "hook_event_name": "SessionStart"})
        ),
    )
    assert main(["hook", "claude"]) == 0
    notice = capsys.readouterr().out
    assert "claude:claude-session" in notice
    assert "llm-bus inbox" in notice
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-session")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    assert main(["whoami"]) == 0
    assert json.loads(capsys.readouterr().out) == {"address": "claude:claude-session"}


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_session_end_hook_marks_registered_agent_ended(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "bus.sqlite3"
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    store = Store(path)
    address = store.register(kind, "session", str(tmp_path))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"session_id": "session", "hook_event_name": "SessionEnd"})),
    )
    assert main(["hook", kind]) == 0
    assert capsys.readouterr().out == ""
    assert Store(path).is_ended(address)


def test_install_and_uninstall_preserve_other_host_settings(tmp_path: Path) -> None:
    codex = tmp_path / ".codex" / "hooks.json"
    claude = tmp_path / ".claude" / "settings.json"
    codex.parent.mkdir()
    claude.parent.mkdir()
    codex.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"command": "other"}]}]}}))
    claude.write_text(json.dumps({"model": "sonnet", "permissions": {"allow": ["Bash(git *)"]}}))
    configure_hosts(tmp_path, install=True)
    once = (codex.read_text(), claude.read_text())
    configure_hosts(tmp_path, install=True)
    assert once == (codex.read_text(), claude.read_text())
    assert "hook codex" in codex.read_text()
    assert "hook claude" in claude.read_text()
    assert "SessionEnd" in json.loads(codex.read_text())["hooks"]
    assert "SessionEnd" in json.loads(claude.read_text())["hooks"]
    assert "Bash(llm-bus *)" in claude.read_text()
    configure_hosts(tmp_path, install=False)
    assert json.loads(codex.read_text()) == {
        "hooks": {"SessionStart": [{"hooks": [{"command": "other"}]}]}
    }
    assert json.loads(claude.read_text()) == {
        "model": "sonnet",
        "permissions": {"allow": ["Bash(git *)"]},
    }


def test_install_respects_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home = tmp_path / "custom-codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    configure_hosts(tmp_path, install=True)
    assert "hook codex" in (codex_home / "hooks.json").read_text()
    assert not (tmp_path / ".codex" / "hooks.json").exists()
    configure_hosts(tmp_path, install=False)


def test_uninstall_removes_existing_matching_bus_settings(tmp_path: Path) -> None:
    codex = tmp_path / ".codex" / "hooks.json"
    claude = tmp_path / ".claude" / "settings.json"
    codex.parent.mkdir()
    claude.parent.mkdir()
    codex.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [{"type": "command", "command": "llm-bus hook codex"}],
                        }
                    ]
                }
            }
        )
    )
    claude.write_text(json.dumps({"permissions": {"allow": ["Bash(llm-bus *)"]}}))
    configure_hosts(tmp_path, install=True)
    configure_hosts(tmp_path, install=False)
    assert json.loads(codex.read_text()) == {}
    assert json.loads(claude.read_text()) == {}


def test_optional_bus_hook_fields_are_normalized_and_removed(tmp_path: Path) -> None:
    codex = tmp_path / ".codex" / "hooks.json"
    claude = tmp_path / ".claude" / "settings.json"
    codex.parent.mkdir()
    claude.parent.mkdir()
    codex.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "llm-bus hook codex",
                                    "timeout": 30,
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )
    claude.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "llm-bus hook claude",
                                    "async": True,
                                },
                                {"type": "command", "command": "other"},
                            ]
                        }
                    ]
                }
            }
        )
    )
    configure_hosts(tmp_path, install=True)
    assert codex.read_text().count("hook codex") == len(
        ("SessionStart", "UserPromptSubmit", "SessionEnd")
    )
    assert claude.read_text().count("hook claude") == len(
        ("SessionStart", "UserPromptSubmit", "SessionEnd")
    )
    configure_hosts(tmp_path, install=False)
    assert json.loads(codex.read_text()) == {}
    assert json.loads(claude.read_text()) == {
        "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other"}]}]}
    }


def test_install_rejects_invalid_second_file_before_writing_first(tmp_path: Path) -> None:
    codex = tmp_path / ".codex" / "hooks.json"
    claude = tmp_path / ".claude" / "settings.json"
    codex.parent.mkdir()
    claude.parent.mkdir()
    codex.write_text("{}")
    claude.write_text('{"hooks": []}')
    with pytest.raises(BusError, match="hooks must be a JSON object"):
        configure_hosts(tmp_path, install=True)
    assert codex.read_text() == "{}"


def test_install_rejects_symlinked_settings_without_replacing_link(tmp_path: Path) -> None:
    target = tmp_path / "managed-hooks.json"
    target.write_text("{}")
    codex = tmp_path / ".codex" / "hooks.json"
    codex.parent.mkdir()
    codex.symlink_to(target)
    with pytest.raises(BusError, match="symlink is unsupported"):
        configure_hosts(tmp_path, install=True)
    assert codex.is_symlink()
    assert target.read_text() == "{}"


def test_failed_second_settings_write_restores_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / ".codex" / "hooks.json"
    claude = tmp_path / ".claude" / "settings.json"
    codex.parent.mkdir()
    claude.parent.mkdir()
    codex.write_text('{"hooks": {}}\n')
    claude.write_text('{"model": "sonnet"}\n')
    codex.chmod(0o640)
    before = [(path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in (codex, claude)]
    writes = 0

    def fail_second_write(path: Path, content: bytes, mode: int, _expected: bytes | None) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated second write failure")
        path.write_bytes(content)
        path.chmod(mode)

    monkeypatch.setattr("llm_bus_install._atomic_write", fail_second_write)
    with pytest.raises(OSError, match="second write failure"):
        configure_hosts(tmp_path, install=True)
    assert [
        (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in (codex, claude)
    ] == before


def test_install_detects_concurrent_settings_change_without_losing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / ".codex" / "hooks.json"
    claude = tmp_path / ".claude" / "settings.json"
    codex.parent.mkdir()
    claude.parent.mkdir()
    codex.write_text("{}")
    claude.write_text("{}")

    def update_claude_after_codex_write(
        path: Path, content: bytes, mode: int, _expected: bytes | None
    ) -> None:
        path.write_bytes(content)
        path.chmod(mode)
        if path == codex:
            claude.write_text('{"concurrentHostSetting": "preserved"}')

    monkeypatch.setattr("llm_bus_install._atomic_write", update_claude_after_codex_write)
    with pytest.raises(BusError, match="Settings changed while updating"):
        configure_hosts(tmp_path, install=True)
    assert codex.read_text() == "{}"
    assert json.loads(claude.read_text()) == {"concurrentHostSetting": "preserved"}


def test_install_rechecks_target_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / ".codex" / "hooks.json"
    codex.parent.mkdir()
    codex.write_text("{}")
    codex_reads = 0

    def concurrent_read(path: Path) -> bytes | None:
        nonlocal codex_reads
        if path == codex:
            codex_reads += 1
            if codex_reads == 3:
                codex.write_text('{"concurrentHostSetting": "preserved"}')
        return path.read_bytes() if path.exists() else None

    monkeypatch.setattr("llm_bus_install._current_bytes", concurrent_read)
    with pytest.raises(BusError, match="Settings changed while updating"):
        configure_hosts(tmp_path, install=True)
    assert json.loads(codex.read_text()) == {"concurrentHostSetting": "preserved"}


def test_failed_activation_removes_own_hooks_after_concurrent_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / ".codex" / "hooks.json"
    claude = tmp_path / ".claude" / "settings.json"
    codex.parent.mkdir()
    claude.parent.mkdir()
    codex.write_text("{}")
    claude.write_text("{}")
    writes = 0

    def fail_claude_after_codex_edit(
        path: Path, content: bytes, mode: int, _expected: bytes | None
    ) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("Claude write failed")
        path.write_bytes(content)
        path.chmod(mode)
        if path == codex:
            settings = cast("dict[str, object]", json.loads(codex.read_text()))
            settings["concurrentHostSetting"] = "preserved"
            codex.write_text(json.dumps(settings))

    monkeypatch.setattr("llm_bus_install._atomic_write", fail_claude_after_codex_edit)
    with pytest.raises(OSError, match="Claude write failed"):
        configure_hosts(tmp_path, install=True)
    settings = cast("dict[str, object]", json.loads(codex.read_text()))
    assert settings == {"concurrentHostSetting": "preserved"}
    assert json.loads(claude.read_text()) == {}


def test_uninstall_removes_bus_handler_from_shared_group(tmp_path: Path) -> None:
    codex = tmp_path / ".codex" / "hooks.json"
    codex.parent.mkdir()
    codex.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {"type": "command", "command": "llm-bus hook codex"},
                                {"type": "command", "command": "other"},
                            ]
                        }
                    ]
                }
            }
        )
    )
    configure_hosts(tmp_path, install=False)
    assert json.loads(codex.read_text())["hooks"]["SessionStart"] == [
        {"hooks": [{"type": "command", "command": "other"}]}
    ]


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

    monkeypatch.setattr("llm_bus_dispatch.wake", fake_wake)
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


def test_claude_deferred_wake_preserves_message_for_next_prompt_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    path = tmp_path / "bus.sqlite3"
    recipient = Store(path).register("claude", "recipient", str(folder))
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    monkeypatch.setenv("CODEX_THREAD_ID", "sender")
    monkeypatch.chdir(folder)

    assert main(["send", recipient, "--body", "Please reply"]) == 0
    output = capsys.readouterr()
    sent = cast("dict[str, object]", json.loads(output.out))
    assert cast("dict[str, str]", sent["wake"])["status"] == "unsupported"
    assert "stored; wake unsupported" in output.err
    assert Store(path).history(folder=str(folder))[0]["wake_status"] == "unsupported"

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "recipient",
                    "hook_event_name": "UserPromptSubmit",
                    "cwd": str(folder),
                }
            )
        ),
    )
    assert main(["hook", "claude"]) == 0
    assert "1 pending message(s)" in capsys.readouterr().out
    pending = Store(path).inbox(recipient)
    assert len(pending) == 1
    assert pending[0]["id"] == sent["id"]
    assert pending[0]["body"] == "Please reply"


def test_claude_hook_registers_socket_without_token_and_clears_on_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bus.sqlite3"
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    endpoint = str(tmp_path / "inbox.sock")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", endpoint)
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "must-not-store-this")
    recipient = "claude:recipient"
    for event in ("SessionStart", "SessionEnd"):
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(json.dumps({"session_id": "recipient", "hook_event_name": event})),
        )
        assert main(["hook", "claude"]) == 0
        store = Store(path)
        assert store.messaging_socket(recipient) == (endpoint if event == "SessionStart" else None)
        assert "must-not-store-this" not in capsys.readouterr().out
    with sqlite3.connect(path) as db:
        assert "must-not-store-this" not in "\n".join(db.iterdump())


def test_send_keeps_message_when_wake_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    path = tmp_path / "bus.sqlite3"
    recipient = Store(path).register("codex", "recipient", str(folder))
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    monkeypatch.setenv("CODEX_THREAD_ID", "sender")
    monkeypatch.chdir(folder)
    called: list[tuple[str, int]] = []

    def fail_wake(address: str, message_id: int, _sender: str) -> dict[str, str]:
        called.append((address, message_id))
        return {"status": "failed", "via": "codex queue", "reason": "not reachable"}

    monkeypatch.setattr("llm_bus_dispatch.wake", fail_wake)
    assert main(["send", recipient, "--body", "Please reply"]) == 0
    output = capsys.readouterr()
    sent = cast("dict[str, object]", json.loads(output.out))
    assert sent["wake"] == {"status": "failed", "via": "codex queue", "reason": "not reachable"}
    assert "stored; wake failed" in output.err
    assert called == [(recipient, sent["id"])]
    pending = Store(path).inbox(recipient)
    assert len(pending) == 1
    assert pending[0]["id"] == sent["id"]
    history = Store(path).history(folder=str(folder))[0]
    assert history["wake_status"] == "failed"
    assert history["wake_via"] == "codex queue"
    assert history["wake_reason"] == "wake_failed:codex_queue"
    assert history["wake_checked_at"] is not None


def test_send_to_ended_session_stores_message_without_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    path = tmp_path / "bus.sqlite3"
    store = Store(path)
    recipient = store.register("codex", "recipient", str(folder))
    store.end_session("codex", "recipient")
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    monkeypatch.setenv("CODEX_THREAD_ID", "sender")
    monkeypatch.chdir(folder)

    def unexpected_wake(_address: str, _message_id: int, _sender: str) -> dict[str, str]:
        pytest.fail("ended session must not be woken")

    monkeypatch.setattr("llm_bus_dispatch.wake", unexpected_wake)
    assert main(["send", recipient, "--body", "Please reply"]) == 0
    sent = cast("dict[str, object]", json.loads(capsys.readouterr().out))
    assert cast("dict[str, str]", sent["wake"])["status"] == "failed"
    assert len(Store(path).inbox(recipient)) == 1
    assert Store(path).history(folder=str(folder))[0]["wake_status"] == "failed"


def test_send_reports_message_id_when_wake_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    path = tmp_path / "bus.sqlite3"
    recipient = Store(path).register("codex", "recipient", str(folder))
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    monkeypatch.setenv("CODEX_THREAD_ID", "sender")
    monkeypatch.chdir(folder)

    def broken_wake(_address: str, _message_id: int, _sender: str) -> dict[str, str]:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")

    monkeypatch.setattr("llm_bus_dispatch.wake", broken_wake)
    assert main(["send", recipient, "--body", "Please reply"]) == 0
    output = capsys.readouterr()
    sent = cast("dict[str, object]", json.loads(output.out))
    assert sent["id"] == 1
    assert sent["wake"] == {
        "status": "unknown",
        "via": "host wake",
        "reason": "wake adapter raised UnicodeDecodeError",
    }
    assert "message #1 stored; wake unknown" in output.err
    assert len(Store(path).inbox(recipient)) == 1
    assert Store(path).history(folder=str(folder))[0]["wake_status"] == "unknown"


def test_send_reports_message_id_when_wake_recording_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    path = tmp_path / "bus.sqlite3"
    recipient = Store(path).register("codex", "recipient", str(folder))
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    monkeypatch.setenv("CODEX_THREAD_ID", "sender")
    monkeypatch.chdir(folder)

    def requested_wake(_address: str, _id: int, _sender: str) -> dict[str, str]:
        return {"status": "requested", "via": "codex queue"}

    monkeypatch.setattr("llm_bus_dispatch.wake", requested_wake)

    def fail_record(*_args: object) -> None:
        raise sqlite3.OperationalError("database locked")

    monkeypatch.setattr("llm_bus_dispatch._finish", fail_record)
    assert main(["send", recipient, "--body", "Please reply"]) == 0
    output = capsys.readouterr()
    sent = cast("dict[str, object]", json.loads(output.out))
    assert sent["id"] == 1
    assert "could not record wake result" in output.err
    assert Store(path).inbox(recipient)[0]["id"] == 1
    assert Store(path).history(folder=str(folder))[0]["wake_status"] is None


def test_uninstall_removes_hooks_then_uv_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_hosts(tmp_path, install=True)
    monkeypatch.setattr("llm_bus.Path.home", lambda: tmp_path)

    def fake_which(_name: str) -> str:
        return "/usr/local/bin/uv"

    monkeypatch.setattr("llm_bus.shutil.which", fake_which)
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check
        calls.append(command)
        assert "llm-bus hook codex" not in (tmp_path / ".codex/hooks.json").read_text()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("llm_bus.subprocess.run", fake_run)
    assert main(["uninstall"]) == 0
    assert json.loads(capsys.readouterr().out)["installed"] is False
    assert calls == [["/usr/local/bin/uv", "tool", "uninstall", "llm-bus"]]


def test_install_reports_codex_hook_trust_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("llm_bus.Path.home", lambda: tmp_path)
    assert main(["install"]) == 0
    result = cast("dict[str, object]", json.loads(capsys.readouterr().out))
    assert result["installed"] is True
    assert "/hooks" in cast("str", result["codex_next_step"])


def test_failed_uninstall_restores_exact_host_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = configure_hosts(tmp_path, install=True)
    before = [path.read_bytes() for path in paths]
    monkeypatch.setattr("llm_bus.Path.home", lambda: tmp_path)

    def fake_which(_name: str) -> str:
        return "/usr/local/bin/uv"

    def fail_run(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("llm_bus.shutil.which", fake_which)
    monkeypatch.setattr("llm_bus.subprocess.run", fail_run)
    assert main(["uninstall"]) == 1
    assert "returned non-zero" in capsys.readouterr().err
    assert [path.read_bytes() for path in paths] == before


def test_uninstall_without_uv_keeps_host_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = configure_hosts(tmp_path, install=True)
    before = [path.read_bytes() for path in paths]
    monkeypatch.setattr("llm_bus.Path.home", lambda: tmp_path)

    def missing_uv(_name: str) -> None:
        return None

    monkeypatch.setattr("llm_bus.shutil.which", missing_uv)
    assert main(["uninstall"]) == 1
    assert "uv is required" in capsys.readouterr().err
    assert [path.read_bytes() for path in paths] == before


def test_failed_uninstall_preserves_concurrent_host_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    codex, claude = configure_hosts(tmp_path, install=True)
    monkeypatch.setattr("llm_bus.Path.home", lambda: tmp_path)

    def fake_which(_name: str) -> str:
        return "/usr/local/bin/uv"

    def fail_after_host_change(
        command: list[str], *, check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert check
        settings = cast("dict[str, object]", json.loads(claude.read_text()))
        settings["concurrentHostSetting"] = "preserved"
        claude.write_text(json.dumps(settings))
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("llm_bus.shutil.which", fake_which)
    monkeypatch.setattr("llm_bus.subprocess.run", fail_after_host_change)
    assert main(["uninstall"]) == 1
    capsys.readouterr()
    assert "hook codex" in codex.read_text()
    assert "hook claude" in claude.read_text()
    assert json.loads(claude.read_text())["concurrentHostSetting"] == "preserved"


def test_failed_uninstall_restores_original_matcher_and_event_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / ".codex" / "hooks.json"
    codex.parent.mkdir()
    original = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [
                        {"type": "command", "command": "llm-bus hook codex", "timeout": 30},
                        {"type": "command", "command": "other"},
                    ],
                }
            ]
        }
    }
    codex.write_text(json.dumps(original))
    monkeypatch.setattr("llm_bus.Path.home", lambda: tmp_path)

    def fake_which(_name: str) -> str:
        return "/usr/local/bin/uv"

    def fail_run(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("llm_bus.shutil.which", fake_which)
    monkeypatch.setattr("llm_bus.subprocess.run", fail_run)
    assert main(["uninstall"]) == 1
    assert json.loads(codex.read_text()) == original


def test_failed_uninstall_restores_bus_entry_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / ".codex" / "hooks.json"
    claude = tmp_path / ".claude" / "settings.json"
    codex.parent.mkdir()
    claude.parent.mkdir()
    codex_before = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "llm-bus hook codex"}]},
                {"hooks": [{"type": "command", "command": "other"}]},
            ]
        }
    }
    claude_before = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "llm-bus hook claude"}]},
                {"hooks": [{"type": "command", "command": "other"}]},
            ]
        },
        "permissions": {"allow": ["Bash(llm-bus *)", "Bash(git *)"]},
    }
    codex.write_text(json.dumps(codex_before))
    claude.write_text(json.dumps(claude_before))
    monkeypatch.setattr("llm_bus.Path.home", lambda: tmp_path)

    def fake_which(_name: str) -> str:
        return "/usr/local/bin/uv"

    monkeypatch.setattr("llm_bus.shutil.which", fake_which)

    def fail_run(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("llm_bus.subprocess.run", fail_run)
    assert main(["uninstall"]) == 1
    assert json.loads(codex.read_text()) == codex_before
    assert json.loads(claude.read_text()) == claude_before


def test_failed_uninstall_keeps_bus_order_with_concurrent_insertions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / ".codex" / "hooks.json"
    claude = tmp_path / ".claude" / "settings.json"
    codex.parent.mkdir()
    claude.parent.mkdir()
    codex_bus = {"hooks": [{"type": "command", "command": "llm-bus hook codex"}]}
    claude_bus = {"hooks": [{"type": "command", "command": "llm-bus hook claude"}]}
    first = {"hooks": [{"type": "command", "command": "first"}]}
    last = {"hooks": [{"type": "command", "command": "last"}]}
    concurrent = {"hooks": [{"type": "command", "command": "concurrent"}]}
    codex.write_text(json.dumps({"hooks": {"SessionStart": [first, codex_bus, last]}}))
    claude.write_text(
        json.dumps(
            {
                "hooks": {"SessionStart": [first, claude_bus, last]},
                "permissions": {"allow": ["Bash(git *)", "Bash(llm-bus *)", "Bash(uv *)"]},
            }
        )
    )
    monkeypatch.setattr("llm_bus.Path.home", lambda: tmp_path)

    def fake_which(_name: str) -> str:
        return "/usr/local/bin/uv"

    def fail_after_host_change(
        command: list[str], *, check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert check
        codex.write_text(json.dumps({"hooks": {"SessionStart": [concurrent, first, last]}}))
        claude.write_text(
            json.dumps(
                {
                    "hooks": {"SessionStart": [concurrent, first, last]},
                    "permissions": {"allow": ["Bash(ls *)", "Bash(git *)", "Bash(uv *)"]},
                }
            )
        )
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("llm_bus.shutil.which", fake_which)
    monkeypatch.setattr("llm_bus.subprocess.run", fail_after_host_change)
    assert main(["uninstall"]) == 1
    assert json.loads(codex.read_text()) == {
        "hooks": {"SessionStart": [concurrent, first, codex_bus, last]}
    }
    assert json.loads(claude.read_text()) == {
        "hooks": {"SessionStart": [concurrent, first, claude_bus, last]},
        "permissions": {"allow": ["Bash(ls *)", "Bash(git *)", "Bash(llm-bus *)", "Bash(uv *)"]},
    }


def test_failed_uninstall_restores_standalone_before_shared_bus_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / ".codex" / "hooks.json"
    codex.parent.mkdir()
    standalone = {"hooks": [{"type": "command", "command": "llm-bus hook codex", "timeout": 1}]}
    shared = {
        "hooks": [
            {"type": "command", "command": "llm-bus hook codex", "timeout": 2},
            {"type": "command", "command": "other"},
        ]
    }
    shared_without_bus = {"hooks": [{"type": "command", "command": "other"}]}
    tail = {"hooks": [{"type": "command", "command": "tail"}]}
    concurrent = {"hooks": [{"type": "command", "command": "concurrent"}]}
    codex.write_text(json.dumps({"hooks": {"SessionStart": [standalone, shared, tail]}}))
    monkeypatch.setattr("llm_bus.Path.home", lambda: tmp_path)

    def fake_which(_name: str) -> str:
        return "/usr/local/bin/uv"

    def fail_after_host_change(
        command: list[str], *, check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert check
        codex.write_text(
            json.dumps({"hooks": {"SessionStart": [concurrent, shared_without_bus, tail]}})
        )
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("llm_bus.shutil.which", fake_which)
    monkeypatch.setattr("llm_bus.subprocess.run", fail_after_host_change)
    assert main(["uninstall"]) == 1
    assert json.loads(codex.read_text()) == {
        "hooks": {"SessionStart": [concurrent, standalone, shared, tail]}
    }


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


def test_migration_reopen_skips_ddl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # pylint: disable=protected-access,clean-code-commented-out-code
    # Trace the real constructor connection to detect repeated migration statements.
    path = tmp_path / "migration.sqlite3"
    Store(path)
    statements: list[str] = []
    original = Store._connect  # pyright: ignore[reportPrivateUsage]

    def connect(store: Store) -> sqlite3.Connection:
        db = original(store)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(Store, "_connect", connect)
    Store(path)
    assert not any(
        statement.startswith(("CREATE", "ALTER", "BEGIN IMMEDIATE", "PRAGMA table_info"))
        for statement in statements
    )


def test_migration_rollback_and_future_version(tmp_path: Path) -> None:
    path = tmp_path / "legacy-rollback.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE agents (address TEXT PRIMARY KEY, kind TEXT, project TEXT, last_seen TEXT)"
        )
        db.execute("INSERT INTO agents VALUES ('codex:old', 'codex', '/repo', 'now')")
        db.commit()

        def unavailable_scope(_folder: str) -> str:
            raise OSError("scope unavailable")

        with pytest.raises(OSError, match="scope unavailable"):
            ensure_schema(db, unavailable_scope)
        assert db.execute("PRAGMA user_version").fetchone() == (0,)
        columns = cast(
            "list[tuple[int, str, str, int, object, int]]",
            db.execute("PRAGMA table_info(agents)").fetchall(),
        )
        assert [row[1] for row in columns] == [
            "address",
            "kind",
            "project",
            "last_seen",
        ]
        assert db.execute("SELECT name FROM sqlite_master WHERE name='messages'").fetchone() is None
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        with pytest.raises(ValueError, match="newer"):
            ensure_schema(db, str)
        assert db.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION + 1,)


def test_migration_concurrent_openers_preserve_history(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.sqlite3"
    with ThreadPoolExecutor(max_workers=2) as pool:
        stores = list(pool.map(Store, [path, path]))
    recipient = stores[0].register("codex", "recipient", "/project")
    message = stores[0].send(SenderContext("claude", "sender", "/project"), recipient, "retained")
    assert stores[1].inbox(recipient) == [message]
    with stores[1].transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
