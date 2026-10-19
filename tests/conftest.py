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
