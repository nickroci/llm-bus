"""Local policy parsing rejects accidental capability expansion."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from llm_bus_policy import load_config
from llm_bus_store import BusError


def test_scope_reuse_ends_at_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git required for topology checks")
    repo = tmp_path / "repo"
    subprocess.run([git, "init", "-q", str(repo)], check=True)
    path = tmp_path / "policy.toml"
    path.write_text(policy_text())
    monkeypatch.setenv("LLM_BUS_CONFIG", str(path))
    config = load_config()
    assert config is not None
    stored = config.repositories[0].scope
    assert config.repository_scope(stored) == config.repository(str(repo))
    (repo / ".git").rename(repo / "old-git")
    subprocess.run([git, "init", "-q", str(repo)], check=True)
    # A different common directory is a different trust boundary at the next load.
    moved = tmp_path / "new-common"
    (repo / ".git").rename(moved)
    (repo / ".git").write_text(f"gitdir: {moved}\n")
    fresh = load_config()
    assert fresh is not None
    assert fresh.repository_scope(stored) is None
    assert config.repository(str(repo)) is None


if TYPE_CHECKING:
    from pathlib import Path


def policy_text() -> str:
    return """policy_version = 2
[remote]
url = "https://bus.test"
machine_id = "b"
credential_file = "b.token"
[[repositories]]
path = "repo"
repo_id = "xyz"
[[repositories.peers]]
machine_id = "a"
"""


def test_peer_defaults_deny(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "policy.toml"
    path.write_text(policy_text())
    monkeypatch.setenv("LLM_BUS_CONFIG", str(path))
    config = load_config()
    assert config is not None
    peer = config.peer("xyz", "a")
    assert peer is not None
    assert not peer.send_messages
    assert peer.receive_messages == "deny"
    assert peer.send_reports == peer.receive_reports == ()
    assert config.peer("xyz", "missing") is None


@pytest.mark.parametrize(
    "change",
    [
        'send_messages = "true"',
        'receive_messages = "all"',
        'receive_reports = ["custom"]',
        'send_reports = ["received", "received"]',
        'send_to = ["xyz"]',
        "unknown = true",
        'machine_id = "*"',
    ],
)
def test_invalid_peer_policy_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    path = tmp_path / "policy.toml"
    text = policy_text()
    if change.startswith("machine_id"):
        text = text.replace('machine_id = "a"', change)
    else:
        text += change + "\n"
    path.write_text(text)
    monkeypatch.setenv("LLM_BUS_CONFIG", str(path))
    with pytest.raises(BusError):
        load_config()


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("policy_version = 2", ""),
        ("policy_version = 2", "policy_version = true"),
        ("policy_version = 2", "policy_version = 2.0"),
        ('machine_id = "a"', 'machine_id = "b"'),
        ('machine_id = "a"', 'machine_id = "a"\n[[repositories.peers]]\nmachine_id = "a"'),
        ('repo_id = "xyz"', 'repo_id = "xyz"\nsend_to = []'),
    ],
)
def test_invalid_configuration_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, original: str, replacement: str
) -> None:
    path = tmp_path / "policy.toml"
    path.write_text(policy_text().replace(original, replacement))
    monkeypatch.setenv("LLM_BUS_CONFIG", str(path))
    with pytest.raises(BusError):
        load_config()


@pytest.mark.parametrize(
    "tail",
    [
        '[[repositories]]\npath = "repo"\nrepo_id = "other"\n',
        '[[repositories]]\npath = "other"\nrepo_id = "xyz"\n',
    ],
)
def test_duplicate_repository_identity_or_scope_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tail: str
) -> None:
    path = tmp_path / "policy.toml"
    path.write_text(policy_text() + tail, encoding="utf-8")
    monkeypatch.setenv("LLM_BUS_CONFIG", str(path))
    with pytest.raises(BusError, match="Duplicate remote repository"):
        load_config()


def test_report_capabilities_are_explicit_and_not_reciprocal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "policy.toml"
    path.write_text(
        policy_text()
        + """send_reports = ["received", "proceeding", "not_proceeding", "completed", "failed"]
receive_messages = "monitor"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_BUS_CONFIG", str(path))
    config = load_config()
    assert config is not None
    peer = config.peer("xyz", "a")
    assert peer is not None
    assert peer.send_reports == ("received", "proceeding", "not_proceeding", "completed", "failed")
    assert peer.receive_reports == ()
    assert not peer.send_messages
    assert peer.receive_messages == "monitor"
