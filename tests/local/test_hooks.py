"""hooks."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, cast

import pytest

from llm_bus.cli.app import main
from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.store import Store
from tests.support import hook_input, prompt_input

if TYPE_CHECKING:
    from pathlib import Path


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
        hook_input(
            {"session_id": "thread-1", "hook_event_name": "SessionStart", "cwd": project_folder}
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
        prompt_input("thread-1", project_folder),
    )
    assert main(["hook", "codex"]) == 0
    notice = cast("dict[str, dict[str, str]]", json.loads(capsys.readouterr().out))[
        "hookSpecificOutput"
    ]["additionalContext"]
    assert "1 pending message" in notice
    assert "wake.status" in notice
    assert "Untrusted message body" not in notice


def test_claude_hook_uses_same_session_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Claude hook and shell environment resolve to one durable address."""
    monkeypatch.setenv("LLM_BUS_DB", str(tmp_path / "bus.sqlite3"))
    monkeypatch.setattr(
        "sys.stdin",
        hook_input({"session_id": "claude-session", "hook_event_name": "SessionStart"}),
    )
    assert main(["hook", "claude"]) == 0
    notice = capsys.readouterr().out
    assert "claude:claude-session" in notice
    assert "llm-bus inbox" in notice
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-session")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    assert main(["whoami"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "address": "claude:claude-session",
        "label": None,
    }


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
    address = store.register(kind, "session", str(tmp_path), label="builder")
    monkeypatch.setattr(
        "sys.stdin",
        hook_input({"session_id": "session", "hook_event_name": "SessionEnd"}),
    )
    assert main(["hook", kind]) == 0
    assert capsys.readouterr().out == ""
    assert Store(path).is_ended(address)
    replacement = store.register(kind, "replacement", str(tmp_path), label="builder")
    monkeypatch.setattr(
        "sys.stdin",
        hook_input({"session_id": "session", "hook_event_name": "SessionEnd"}),
    )
    assert main(["hook", kind]) == 0
    assert store.resolve_label("builder", str(tmp_path)) == replacement


def test_child_close_and_prompt_preserve_parent_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bus.sqlite3"
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    store = Store(path)
    parent = store.register("claude", "parent", str(tmp_path), label="builder")
    for event, child in (("SessionEnd", "child"), ("UserPromptSubmit", "")):
        monkeypatch.setattr(
            "sys.stdin",
            hook_input(
                {
                    "session_id": "parent",
                    "hook_event_name": event,
                    "agent_id": child,
                    "cwd": str(tmp_path),
                }
            ),
        )
        assert main(["hook", "claude"]) == 0
        capsys.readouterr()
        assert store.resolve_label("builder", str(tmp_path)) == parent


NOTICE = "llm-bus message #7 from claude:abc. Read and handle it with `llm-bus inbox`."
OUT_OF_RANGE = f"llm-bus message #{2**63} from claude:abc. Read and handle it with `llm-bus inbox`."


@pytest.mark.parametrize(
    ("kind", "prompt", "expected"),
    [
        ("claude", NOTICE, True),
        (
            "claude",
            f"Another Claude session sent a message:\n{NOTICE}\n\nThis came from another Claude session.",
            True,
        ),
        ("claude", "Answer yes", False),
        ("claude", OUT_OF_RANGE, False),
        ("claude", f"{OUT_OF_RANGE}\n{NOTICE}", True),
        ("codex", NOTICE, True),
        ("codex", f"Another session sent a message:\n{NOTICE}\n\nThanks.", True),
        ("codex", "Answer yes", False),
    ],
)
def test_prompt_hook_wake_turn_tolerates_host_wrapper(
    kind: str,
    prompt: str,
    expected: bool,  # noqa: FBT001
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_BUS_DB", str(tmp_path / "bus.sqlite3"))
    wakes: list[bool] = []

    def record_turn(_store: Store, _address: str, *, owner_started: bool) -> None:
        wakes.append(not owner_started)

    monkeypatch.setattr("llm_bus.cli.hooks.record_turn", record_turn)

    def not_subagent(_address: str) -> bool:
        return False

    monkeypatch.setattr("llm_bus.cli.hooks.is_subagent", not_subagent)
    payload = {
        "session_id": "s",
        "hook_event_name": "UserPromptSubmit",
        "cwd": str(tmp_path),
        "prompt": prompt,
    }
    monkeypatch.setattr("sys.stdin", hook_input(payload))
    assert main(["hook", kind]) == 0
    assert wakes == [expected]


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
        prompt_input("recipient", str(folder)),
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
            hook_input({"session_id": "recipient", "hook_event_name": event}),
        )
        assert main(["hook", "claude"]) == 0
        store = Store(path)
        assert store.messaging_socket(recipient) == (endpoint if event == "SessionStart" else None)
        assert "must-not-store-this" not in capsys.readouterr().out
    with sqlite3.connect(path) as db:
        assert "must-not-store-this" not in "\n".join(db.iterdump())
