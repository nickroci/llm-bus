"""Gemini CLI as a native host: hooks, identity, settings and Herdr wake."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import pytest

import llm_bus.wake.adapters
from llm_bus import maintenance
from llm_bus.cli.app import host_identity, main
from llm_bus.errors import BusError
from llm_bus.hosts.hooks import settings_kind
from llm_bus.hosts.processes import HostProcess, is_gemini_command
from llm_bus.hosts.restore import restore_bus_settings
from llm_bus.hosts.settings import configure_hosts, snapshot_host_settings
from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.store import Store
from tests.support import hook_input

from .support import fake_herdr, settings_paths

if TYPE_CHECKING:
    from pathlib import Path

GEMINI_HOST = HostProcess(4242, "Thu Nov 26 09:00:00 2026")


def _gemini_host(_kind: str) -> HostProcess:
    return GEMINI_HOST


def _gemini_hook(monkeypatch: pytest.MonkeyPatch, event: str, cwd: Path, **extra: object) -> None:
    payload: dict[str, object] = {
        "session_id": "gem-1",
        "hook_event_name": event,
        "cwd": str(cwd),
        **extra,
    }
    monkeypatch.setattr("sys.stdin", hook_input(payload))
    assert main(["hook", "gemini"]) == 0


def _registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    path = tmp_path / "bus.sqlite3"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p3")
    monkeypatch.setattr("llm_bus.cli.hooks.host_process", _gemini_host)
    return path, project.resolve()


def test_gemini_hooks_register_session_pane_and_host_with_json_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path, project = _registered(tmp_path, monkeypatch)
    _gemini_hook(monkeypatch, "SessionStart", project, source="startup")
    started = cast("dict[str, dict[str, str]]", json.loads(capsys.readouterr().out))
    assert started["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "gemini:gem-1" in started["hookSpecificOutput"]["additionalContext"]

    store = Store(path)
    assert store.messaging_socket("gemini:gem-1") == "w1:p3"
    assert store.host_session("gemini", GEMINI_HOST) == "gemini:gem-1"

    store.send(SenderContext("claude", "peer", str(project)), "gemini:gem-1", "secret body")
    _gemini_hook(monkeypatch, "BeforeAgent", project, prompt="Continue")
    prompted = cast("dict[str, dict[str, str]]", json.loads(capsys.readouterr().out))
    assert prompted["hookSpecificOutput"]["hookEventName"] == "BeforeAgent"
    assert "1 pending message(s)" in prompted["hookSpecificOutput"]["additionalContext"]
    assert "secret body" not in prompted["hookSpecificOutput"]["additionalContext"]

    _gemini_hook(monkeypatch, "SessionEnd", project, reason="exit")
    assert store.is_ended("gemini:gem-1")
    assert store.messaging_socket("gemini:gem-1") is None
    assert store.host_session("gemini", GEMINI_HOST) is None


def test_gemini_hook_rejects_unknown_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _path, project = _registered(tmp_path, monkeypatch)
    payload = {"session_id": "gem-1", "hook_event_name": "AfterTool", "cwd": str(project)}
    monkeypatch.setattr("sys.stdin", hook_input(payload))
    assert main(["hook", "gemini"]) == 1
    assert "valid session_id or event" in capsys.readouterr().err


def test_gemini_shell_identity_comes_from_the_registered_gemini_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path, project = _registered(tmp_path, monkeypatch)
    _gemini_hook(monkeypatch, "SessionStart", project)
    capsys.readouterr()
    monkeypatch.setattr("llm_bus.cli.app.nearest_host", lambda: ("gemini", GEMINI_HOST))
    # A Gemini launched by a Claude shell inherits Claude's session ID. Gemini still wins.
    environment = {"GEMINI_CLI": "1", "CLAUDE_CODE_SESSION_ID": "parent"}
    assert host_identity(environment, Store(path)) == ("gemini", "gem-1")
    assert host_identity(environment) == ("gemini", "gem-1")

    monkeypatch.setenv("GEMINI_CLI", "1")
    monkeypatch.chdir(project)
    assert main(["--format", "json", "whoami"]) == 0
    assert "gemini:gem-1" in capsys.readouterr().out


def test_gemini_identity_requires_a_registered_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BUS_DB", str(tmp_path / "bus.sqlite3"))
    monkeypatch.setattr("llm_bus.cli.app.nearest_host", lambda: ("gemini", GEMINI_HOST))
    with pytest.raises(BusError, match="Gemini session is not registered"):
        host_identity({"GEMINI_CLI": "1"})


def test_nearest_non_gemini_host_keeps_its_own_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # Claude launched by a Gemini shell inherits GEMINI_CLI. Its own session ID applies.
    monkeypatch.setattr("llm_bus.cli.app.nearest_host", lambda: ("claude", GEMINI_HOST))
    environment = {"GEMINI_CLI": "1", "CLAUDE_CODE_SESSION_ID": "child"}
    assert host_identity(environment) == ("claude", "child")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("node /usr/local/bin/gemini -p hi", True),
        ("/opt/node/bin/node --max-old-space-size=12288 /opt/node/bin/gemini", True),
        ("node /opt/lib/node_modules/@google/gemini-cli/bundle/gemini.js", True),
        ("gemini --resume latest", True),
        ("node /opt/bin/claude", False),
        ("node server.js gemini", False),
        ("", False),
    ],
)
def test_gemini_command_detection(command: str, expected: bool) -> None:  # noqa: FBT001
    assert is_gemini_command(command) is expected
    assert maintenance.is_host_command(command.split()) is expected


def test_gemini_settings_install_only_when_gemini_folder_exists(tmp_path: Path) -> None:
    settings_paths(tmp_path)
    configure_hosts(tmp_path, install=True)
    assert not (tmp_path / ".gemini").exists()

    gemini = tmp_path / ".gemini" / "settings.json"
    gemini.parent.mkdir()
    original = {"ui": {"theme": "dark"}, "tools": {"allowed": ["run_shell_command(git)"]}}
    gemini.write_text(json.dumps(original))
    result = configure_hosts(tmp_path, install=True)
    assert gemini in result
    assert settings_kind(gemini) == "gemini"
    installed = cast("dict[str, dict[str, list[object]]]", json.loads(gemini.read_text()))
    assert set(installed["hooks"]) == {"SessionStart", "BeforeAgent", "SessionEnd"}
    assert "hook gemini" in json.dumps(installed["hooks"])
    # Gemini deprecates tools.allowed, so the bus adds no allow rule there.
    assert installed["tools"]["allowed"] == ["run_shell_command(git)"]
    once = gemini.read_text()
    configure_hosts(tmp_path, install=True)
    assert gemini.read_text() == once

    configure_hosts(tmp_path, install=False)
    assert json.loads(gemini.read_text()) == original


def test_gemini_settings_respect_gemini_cli_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_paths(tmp_path)
    gemini_home = tmp_path / "gemini-home"
    (gemini_home / ".gemini").mkdir(parents=True)
    monkeypatch.setenv("GEMINI_CLI_HOME", str(gemini_home))
    configure_hosts(tmp_path, install=True)
    assert "hook gemini" in (gemini_home / ".gemini" / "settings.json").read_text()
    assert gemini_home / ".gemini" / "settings.json" in maintenance.host_settings(tmp_path, ())


def test_failed_uninstall_restores_gemini_hooks(tmp_path: Path) -> None:
    settings_paths(tmp_path)
    gemini = tmp_path / ".gemini" / "settings.json"
    gemini.parent.mkdir()
    configure_hosts(tmp_path, install=True)
    installed = cast("object", json.loads(gemini.read_text()))
    snapshots = snapshot_host_settings(tmp_path)
    configure_hosts(tmp_path, install=False, snapshots=snapshots)
    assert "hooks" not in json.loads(gemini.read_text())
    restore_bus_settings(snapshots)
    assert json.loads(gemini.read_text()) == installed


def test_offline_launcher_lets_gemini_hooks_pass() -> None:
    assert ["hook", "gemini"] in maintenance.HOOK_COMMANDS


def test_gemini_wake_prompts_its_herdr_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = fake_herdr(monkeypatch, "gemini")
    result = llm_bus.wake.adapters.wake("gemini:gem-1", 9, "claude:peer", endpoint="w1:p3")
    assert result == {"status": "requested", "via": "herdr agent prompt"}
    notice = "llm-bus message #9 from claude:peer. Read and handle it with `llm-bus inbox`."
    assert commands == [
        ["/bin/herdr", "agent", "get", "w1:p3"],
        ["/bin/herdr", "agent", "prompt", "w1:p3", notice],
    ]


def test_gemini_wake_never_types_into_a_pane_running_another_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = fake_herdr(monkeypatch, "claude")
    result = llm_bus.wake.adapters.wake("gemini:gem-1", 9, "claude:peer", endpoint="w1:p3")
    assert result["status"] == "failed"
    assert [command[1:3] for command in commands] == [["agent", "get"]]


def test_gemini_wake_reports_herdr_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_herdr(monkeypatch, "gemini", prompt_code=1)
    result = llm_bus.wake.adapters.wake("gemini:gem-1", 9, "claude:peer", endpoint="w1:p3")
    assert result == {"status": "failed", "via": "herdr agent prompt", "reason": "busy"}


def test_gemini_wake_without_herdr_pane_waits_for_next_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = fake_herdr(monkeypatch, "gemini")
    result = llm_bus.wake.adapters.wake("gemini:gem-1", 9, "claude:peer")
    assert result["status"] == "unsupported"
    assert "next turn hook" in result.get("reason", "")
    assert commands == []
