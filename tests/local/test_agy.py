"""Antigravity (`agy`) hooks, identity and settings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from llm_bus import maintenance
from llm_bus.cli.app import host_identity, main
from llm_bus.hosts.hooks import AGY_ALLOW_RULE, settings_kind
from llm_bus.hosts.processes import HostProcess
from llm_bus.hosts.restore import restore_bus_settings
from llm_bus.hosts.settings import configure_hosts, snapshot_host_settings
from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.store import Store
from llm_bus.wake.dispatch import dispatch_pending
from tests.support import hook_input

from .support import settings_paths

if TYPE_CHECKING:
    import pytest

AGY_HOST = HostProcess(5151, "Fri Nov 27 09:00:00 2026")
CONVERSATION = "ec33ebf9-0cba-4100-8142-c61503f6c587"
ADDRESS = f"agy:{CONVERSATION}"


def _agy_host(_kind: str) -> HostProcess:
    return AGY_HOST


def _agy_hook(monkeypatch: pytest.MonkeyPatch, event: str, cwd: Path, **extra: object) -> None:
    payload: dict[str, object] = {
        "conversationId": CONVERSATION,
        "hookEventName": event,
        "cwd": str(cwd),
        "workspacePaths": [str(cwd)],
        "lastUserInput": "Fix the failing test",
        **extra,
    }
    monkeypatch.setattr("sys.stdin", hook_input(payload))
    assert main(["hook", "agy"]) == 0


def _injected(output: str) -> str:
    data = cast("dict[str, list[dict[str, dict[str, str]]]]", json.loads(output))
    (step,) = data["injectSteps"]
    return step["systemMessage"]["systemMessage"]


def _registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    path = tmp_path / "bus.sqlite3"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    monkeypatch.setattr("llm_bus.cli.hooks.host_process", _agy_host)
    return path, project.resolve()


def test_agy_hooks_register_conversation_and_report_pending_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path, project = _registered(tmp_path, monkeypatch)
    _agy_hook(monkeypatch, "SessionStart", project)
    assert ADDRESS in _injected(capsys.readouterr().out)

    store = Store(path)
    assert store.messaging_socket(ADDRESS) is None
    assert store.host_session("agy", AGY_HOST) == ADDRESS

    sent = store.send(SenderContext("claude", "peer", str(project)), ADDRESS, "secret body")
    wake = dispatch_pending(store, ADDRESS, trigger_id=sent["id"])
    assert wake["status"] == "unsupported"
    _agy_hook(monkeypatch, "PreInvocation", project, invocationNum=0, initialNumSteps=3)
    notice = _injected(capsys.readouterr().out)
    assert "1 pending message(s)" in notice
    assert "secret body" not in notice


def test_agy_hook_ignores_later_model_calls_of_a_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path, project = _registered(tmp_path, monkeypatch)
    _agy_hook(monkeypatch, "PreInvocation", project, invocationNum=1, initialNumSteps=5)
    assert json.loads(capsys.readouterr().out) == {}
    assert Store(path).host_session("agy", AGY_HOST) is None


def test_agy_resumed_conversation_registers_on_its_first_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Resuming a conversation runs no SessionStart; the turn's first PreInvocation registers it.
    path, project = _registered(tmp_path, monkeypatch)
    _agy_hook(monkeypatch, "PreInvocation", project, invocationNum=0, initialNumSteps=9)
    assert ADDRESS in _injected(capsys.readouterr().out)
    assert Store(path).host_session("agy", AGY_HOST) == ADDRESS


def test_agy_hook_rejects_payload_without_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _path, project = _registered(tmp_path, monkeypatch)
    payload = {"hookEventName": "SessionStart", "cwd": str(project)}
    monkeypatch.setattr("sys.stdin", hook_input(payload))
    assert main(["hook", "agy"]) == 1
    assert "valid session_id or event" in capsys.readouterr().err


def test_agy_shell_identity_beats_inherited_claude_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Antigravity inherits the session ID of a Claude shell that started it. Its own wins.
    environment = {"ANTIGRAVITY_CONVERSATION_ID": CONVERSATION, "CLAUDE_CODE_SESSION_ID": "parent"}
    monkeypatch.setattr("llm_bus.cli.app.nearest_host", lambda: ("agy", AGY_HOST))
    assert host_identity(environment) == ("agy", CONVERSATION)
    # The terminal sandbox may block ps; the Antigravity ID still applies.
    monkeypatch.setattr("llm_bus.cli.app.nearest_host", lambda: None)
    assert host_identity(environment) == ("agy", CONVERSATION)


def test_host_started_inside_agy_keeps_its_own_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("llm_bus.cli.app.nearest_host", lambda: ("claude", AGY_HOST))
    environment = {"ANTIGRAVITY_CONVERSATION_ID": CONVERSATION, "CLAUDE_CODE_SESSION_ID": "child"}
    assert host_identity(environment) == ("claude", "child")


def test_agy_shell_whoami(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _path, project = _registered(tmp_path, monkeypatch)
    monkeypatch.setattr("llm_bus.cli.app.nearest_host", lambda: ("agy", AGY_HOST))
    monkeypatch.setattr("llm_bus.cli.app.host_process", _agy_host)
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", CONVERSATION)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.chdir(project)
    assert main(["--format", "json", "whoami"]) == 0
    assert ADDRESS in capsys.readouterr().out


def _agy_folders(tmp_path: Path) -> tuple[Path, Path]:
    hooks = tmp_path / ".gemini" / "config" / "hooks.json"
    settings = tmp_path / ".gemini" / "antigravity-cli" / "settings.json"
    hooks.parent.mkdir(parents=True)
    settings.parent.mkdir(parents=True)
    return hooks, settings


def test_agy_settings_kinds() -> None:
    assert settings_kind(Path("/h/.gemini/config/hooks.json")) == "agy"
    assert settings_kind(Path("/h/.gemini/antigravity-cli/settings.json")) == "agy-settings"
    assert settings_kind(Path("/h/.gemini/settings.json")) == "gemini"
    assert settings_kind(Path("/h/.codex/hooks.json")) == "codex"
    assert settings_kind(Path("/h/.claude/settings.json")) == "claude"


def test_agy_install_only_when_antigravity_folders_exist(tmp_path: Path) -> None:
    settings_paths(tmp_path)
    configure_hosts(tmp_path, install=True)
    assert not (tmp_path / ".gemini").exists()

    hooks, settings = _agy_folders(tmp_path)
    original_hooks = {"lint": {"PostToolUse": [{"matcher": "*", "hooks": [{"command": "x"}]}]}}
    original_settings = {"agentMode": "auto", "permissions": {"allow": ["command(git status)"]}}
    hooks.write_text(json.dumps(original_hooks))
    settings.write_text(json.dumps(original_settings))
    result = configure_hosts(tmp_path, install=True)
    assert hooks in result
    assert settings in result

    installed = cast("dict[str, dict[str, list[dict[str, str]]]]", json.loads(hooks.read_text()))
    assert installed["lint"] == original_hooks["lint"]
    assert set(installed["llm-bus"]) == {"SessionStart", "PreInvocation"}
    assert installed["llm-bus"]["SessionStart"][0]["command"].endswith("hook agy")
    permissions = cast("dict[str, dict[str, list[str]]]", json.loads(settings.read_text()))
    assert permissions["permissions"]["allow"] == ["command(git status)", AGY_ALLOW_RULE]
    assert hooks in maintenance.host_settings(tmp_path, ())

    once = (hooks.read_text(), settings.read_text())
    configure_hosts(tmp_path, install=True)
    assert (hooks.read_text(), settings.read_text()) == once

    configure_hosts(tmp_path, install=False)
    assert json.loads(hooks.read_text()) == original_hooks
    assert json.loads(settings.read_text()) == original_settings


def test_failed_uninstall_restores_agy_hooks_and_allow_rule(tmp_path: Path) -> None:
    settings_paths(tmp_path)
    hooks, settings = _agy_folders(tmp_path)
    configure_hosts(tmp_path, install=True)
    installed = (json.loads(hooks.read_text()), json.loads(settings.read_text()))
    snapshots = snapshot_host_settings(tmp_path)
    configure_hosts(tmp_path, install=False, snapshots=snapshots)
    assert "llm-bus" not in json.loads(hooks.read_text())
    restore_bus_settings(snapshots)
    assert (json.loads(hooks.read_text()), json.loads(settings.read_text())) == installed


def test_offline_launcher_and_maintenance_know_agy() -> None:
    assert ["hook", "agy"] in maintenance.HOOK_COMMANDS
    assert maintenance.is_host_command(["/Users/me/.local/bin/agy", "--continue"])
