"""Local test setup shared by routing and migration scenarios."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

import llm_bus.wake.adapters

if TYPE_CHECKING:
    from pathlib import Path


def linked_worktree(tmp_path: Path) -> tuple[Path, Path, str]:
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
    return repo, worktree, git


def settings_paths(tmp_path: Path) -> tuple[Path, Path]:
    codex = tmp_path / ".codex" / "hooks.json"
    claude = tmp_path / ".claude" / "settings.json"
    codex.parent.mkdir()
    claude.parent.mkdir()
    return codex, claude


def _bin_path(name: str) -> str:
    return f"/bin/{name}"


def fake_herdr(
    monkeypatch: pytest.MonkeyPatch, agent: str, *, prompt_code: int = 0
) -> list[list[str]]:
    """Record herdr commands; `agent get` reports `agent`, `agent prompt` exits `prompt_code`."""
    commands: list[list[str]] = []

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1:3] == ["agent", "get"]:
            payload = {"result": {"agent": {"agent": agent, "pane_id": command[3]}}}
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(
            command, prompt_code, "{}", "busy" if prompt_code else ""
        )

    monkeypatch.setattr(shutil, "which", _bin_path)
    monkeypatch.setattr(llm_bus.wake.adapters, "_run", run)
    return commands
