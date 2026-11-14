"""Shared test isolation from the agent host running the suite."""

import pytest

HOST_ENVIRONMENT = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
)


@pytest.fixture(autouse=True)
def isolate_host_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host identity and wake sockets out of tests; tests set the ones they need."""
    for name in HOST_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    # The suite's own ancestors may include a live claude or codex process.
    monkeypatch.setattr("llm_bus.cli.app.host_process", _no_host)
    monkeypatch.setattr("llm_bus.cli.hooks.host_process", _no_host)
    # Stall tests drive isolated clocks/timers explicitly, never detached host processes.
    monkeypatch.setattr("llm_bus.coordination.stalls.spawn_checked", _no_stall_process)


def _no_host(_kind: str) -> None:
    return None


def _no_stall_process(*_args: object, **_kwargs: object) -> bool:
    return True
