"""A coordination project that never uses delegation keeps its current wire shapes."""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, cast

from llm_bus.cli.app import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

OPERATIONS = ("assign", "reprioritize", "clarify", "pause", "cancel", "review")
DIRECTION_KEYS = {
    "version",
    "project_id",
    "generation",
    "grant_id",
    "operation",
    "verified",
    "reason",
}
SHOW_KEYS = {
    "scope",
    "project_id",
    "label",
    "generation",
    "coordinator",
    "objective",
    "beads_workspace",
    "memory_ticket",
    "revoked_at",
    "references",
    "grants",
    "enrollments",
    "targets",
}


def test_project_without_delegation_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LLM_BUS_DB", str(tmp_path / "bus.sqlite3"))
    monkeypatch.setenv("LLM_BUS_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beads").mkdir()
    owner, worker = ("codex", "owner"), ("claude", "worker")

    def run(session: tuple[str, str], *argv: str, stdin: str = "") -> object:
        kind, session_id = session
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
        monkeypatch.setenv(
            "CLAUDE_CODE_SESSION_ID" if kind == "claude" else "CODEX_THREAD_ID", session_id
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        assert main(list(argv)) == 0
        return cast("object", json.loads(capsys.readouterr().out))

    def grants() -> list[dict[str, object]]:
        shown = cast("dict[str, object]", run(owner, "coordination", "show", "demo"))
        return cast("list[dict[str, object]]", shown["grants"])

    run(owner, "whoami")
    run(worker, "whoami")
    run(
        owner,
        "coordinate",
        "demo",
        "--memory-ticket",
        "bus-1",
        "--objective",
        "Build",
        "--beads-workspace",
        str(tmp_path),
    )
    run(owner, "coordination", "grant", "demo", "--recipient", "claude:worker", "--until-revoked")
    run(worker, "join", "demo")
    (grant,) = grants()

    for operation in OPERATIONS:
        run(
            owner,
            "send",
            "claude:worker",
            "--project",
            "demo",
            "--direction",
            operation,
            stdin=f"{operation} body",
        )
    inbox = cast("list[dict[str, object]]", run(worker, "inbox"))
    assert [message["body"] for message in inbox] == [f"{name} body" for name in OPERATIONS]
    for message, operation in zip(inbox, OPERATIONS, strict=True):
        direction = cast("dict[str, object]", message["coordination"])
        assert set(direction) == DIRECTION_KEYS
        assert direction["version"] == 1
        assert direction["operation"] == operation
        assert direction["grant_id"] == grant["grant_id"]
        assert direction["verified"] is True
        assert direction["reason"] == "verified"

    shown = cast("dict[str, object]", run(owner, "coordination", "show", "demo"))
    # Revision 2 adds caller-bound execution status alongside unacknowledged directions.
    assert set(shown) == SHOW_KEYS | {"directions", "project_execution_allowed", "unattended"}
    assert shown["project_execution_allowed"] is False
    assert shown["unattended"] == {"active": False, "run": None, "history": []}

    run(worker, "join", "demo")
    assert grants() == [grant]
