"""Host hooks must work without the installing terminal's PATH."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import TYPE_CHECKING, cast

import pytest

from llm_bus_install import configure_hosts

if TYPE_CHECKING:
    from pathlib import Path

type HookHandler = dict[str, str]
type HookGroup = dict[str, list[HookHandler]]


@pytest.mark.skipif(os.name == "nt", reason="Exercises the macOS/Linux command shell")
def test_hooks_run_from_quoted_installation_without_tool_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    executable = shutil.which("llm-bus")
    assert executable is not None
    bin_dir = tmp_path / "tools with spaces"
    bin_dir.mkdir()
    local_tool = bin_dir / "llm-bus"
    shutil.copy2(executable, local_tool)
    monkeypatch.setenv("PATH", str(bin_dir))
    paths = configure_hosts(tmp_path, install=True)
    first = [path.read_bytes() for path in paths]
    configure_hosts(tmp_path, install=True)
    assert [path.read_bytes() for path in paths] == first
    for path in paths:
        settings = cast("dict[str, dict[str, list[HookGroup]]]", json.loads(path.read_text()))
        for groups in settings["hooks"].values():
            for group in groups:
                command = group["hooks"][0]["command"]
                result = subprocess.run(
                    ["/bin/sh", "-c", command + " --help"],
                    env={**os.environ, "PATH": "/usr/bin:/bin"},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                assert result.returncode == 0, result.stderr
                assert "usage:" in result.stdout
    local_tool.unlink()
    configure_hosts(tmp_path, install=False)
    assert all("hooks" not in json.loads(path.read_text()) for path in paths)


def test_custom_claude_directory_migrates_legacy_hooks_and_uninstalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    config_dir = tmp_path / "custom claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    config_dir.mkdir()
    settings_path = config_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {"type": "command", "command": "llm-bus hook claude"},
                                {"type": "command", "command": "other-hook"},
                            ]
                        }
                    ]
                }
            }
        )
    )
    configure_hosts(tmp_path, install=True)
    assert not (tmp_path / ".claude").exists()
    assert settings_path.read_text().count("hook claude") == 3
    configure_hosts(tmp_path, install=False)
    assert json.loads(settings_path.read_text()) == {
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "other-hook"}]}]}
    }
