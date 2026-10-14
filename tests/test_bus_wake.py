"""Host wake adapters never undo already-stored messages."""

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import pytest

import llm_bus_wake


def test_claude_wake_never_sends_terminal_input(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def herdr_path(_name: str) -> str:
        return "/bin/herdr"

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        agents = {
            "result": {
                "agents": [
                    {
                        "agent": "claude",
                        "pane_id": "w1:p1",
                        "agent_session": {"kind": "id", "value": "target"},
                    }
                ]
            }
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(agents), "")

    monkeypatch.setattr(shutil, "which", herdr_path)
    monkeypatch.setattr(llm_bus_wake, "_run", run)
    result = llm_bus_wake.wake("claude:target", 7, "codex:source")
    assert commands == []
    assert result["status"] == "unsupported"
    assert result["via"] == "none"
    assert "next turn hook" in result.get("reason", "")


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(socket, "AF_UNIX"), reason="Unix sockets required"
)
def test_claude_native_wake_uses_socket_and_targets_exact_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid_terminal(_command: list[str]) -> subprocess.CompletedProcess[str]:
        pytest.fail("Claude wake must never write terminal input")

    monkeypatch.setattr(llm_bus_wake, "_run", forbid_terminal)
    with (
        TemporaryDirectory(prefix="bus-", dir="/tmp") as folder,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server,
    ):
        endpoint = Path(folder) / "inbox.sock"
        server.bind(str(endpoint))
        endpoint.chmod(0o600)
        server.listen(1)
        server.settimeout(2)
        assert llm_bus_wake.wake(
            "claude:target", 7, "codex:source", claude_socket=str(endpoint)
        ) == {
            "status": "requested",
            "via": "Claude inbox socket",
        }
        connection = server.accept()[0]
        with connection:
            connection.settimeout(2)
            with connection.makefile("rb") as reader:
                frame = cast("object", json.loads(reader.readline()))
        assert frame == {
            "type": "user",
            "session_id": "target",
            "message": {
                "role": "user",
                "content": "llm-bus message #7 from codex:source. Read and handle it with `llm-bus inbox`.",
            },
            "priority": "next",
        }
    assert (
        llm_bus_wake.wake("claude:target", 8, "codex:source", claude_socket=str(endpoint))["status"]
        == "failed"
    )


@pytest.mark.skipif(os.name == "nt", reason="Unix sockets required")
def test_claude_wake_refuses_non_socket_and_symlink(tmp_path: Path) -> None:
    endpoint = tmp_path / "ordinary-file"
    endpoint.write_text("user draft")
    link = tmp_path / "link"
    link.symlink_to(endpoint)
    for path in (endpoint, link):
        assert (
            llm_bus_wake.wake("claude:target", 7, "codex:source", claude_socket=str(path))["status"]
            == "failed"
        )
    assert endpoint.read_text() == "user draft"


@pytest.mark.parametrize("sender", ["claude:source", "codex:source"])
def test_codex_wake_queues_message_id_and_sender(
    monkeypatch: pytest.MonkeyPatch, sender: str
) -> None:
    commands: list[list[str]] = []

    def codex_path(_name: str) -> str:
        return "/bin/codex"

    monkeypatch.setattr(shutil, "which", codex_path)

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "Queued", "")

    monkeypatch.setattr(llm_bus_wake, "_run", run)
    assert llm_bus_wake.wake("codex:session-123", 42, sender) == {
        "status": "requested",
        "via": "codex queue",
    }
    assert commands == [
        [
            "/bin/codex",
            "queue",
            "--thread",
            "session-123",
            "--message",
            f"llm-bus message #42 from {sender}. Read and handle it with `llm-bus inbox`.",
        ]
    ]


def test_missing_host_and_timeout_report_wake_state(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_path(_name: str) -> None:
        return None

    monkeypatch.setattr(shutil, "which", missing_path)
    assert llm_bus_wake.wake("codex:one", 1, "codex:source")["status"] == "failed"
    assert llm_bus_wake.wake("claude:two", 2, "codex:source")["status"] == "unsupported"

    def codex_path(_name: str) -> str:
        return "/bin/codex"

    monkeypatch.setattr(shutil, "which", codex_path)

    def timeout(command: list[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 10)

    monkeypatch.setattr(llm_bus_wake, "_run", timeout)
    assert llm_bus_wake.wake("codex:one", 1, "codex:source")["status"] == "unknown"
