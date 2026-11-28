"""Local test setup shared by routing and migration scenarios."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

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
