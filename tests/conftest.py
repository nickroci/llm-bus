"""Shared test isolation from the agent host running the suite."""

from pathlib import Path

import pytest

HOST_ENVIRONMENT = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "GEMINI_CLI",
    "GEMINI_CLI_HOME",
    "HERDR_PANE_ID",
)


@pytest.fixture(autouse=True)
def isolate_host_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host identity and wake sockets out of tests; tests set the ones they need."""
    for name in HOST_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    # Public entrypoints must never consult the running owner's maintenance journal.
    monkeypatch.setenv("HOME", str(tmp_path))
    # The suite's own ancestors may include a live claude or codex process.
    monkeypatch.setattr("llm_bus.cli.app.host_process", _no_host)
    monkeypatch.setattr("llm_bus.cli.hooks.host_process", _no_host)
    monkeypatch.setattr("llm_bus.cli.app.nearest_host", _no_nearest_host)
    # Stall tests drive isolated clocks/timers explicitly, never detached host processes.
    monkeypatch.setattr("llm_bus.coordination.stalls.spawn_checked", _no_stall_process)


def _no_host(_kind: str) -> None:
    return None


def _no_nearest_host() -> None:
    return None


def _no_stall_process(*_args: object, **_kwargs: object) -> bool:
    return True
