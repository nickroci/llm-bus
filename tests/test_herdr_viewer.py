import json
import subprocess
from subprocess import CompletedProcess

import pytest

pytest.importorskip("curses")

# Windows must skip this module before loading the viewer.
import viewer  # pylint: disable=C0413


def row(message_id: int, **changes: object) -> dict[str, object]:
    record: dict[str, object] = {
        "id": message_id,
        "sender": "codex:one",
        "recipient": "claude:two",
        "body": "hello",
        "sender_cwd": "/one",
        "recipient_cwd": "/two",
        "sent_at": "2026-09-28T10:00:00Z",
        "acknowledged_at": None,
    }
    record.update(changes)
    return record


def test_folder_context_prefers_workspace_and_handles_missing_data() -> None:
    assert (
        viewer.folder_from_context(json.dumps({"workspace_cwd": "/project/../project"}))
        == "/project"
    )
    assert viewer.folder_from_context(json.dumps({"focused_pane_cwd": "/pane"})) == "/pane"
    assert viewer.folder_from_context(json.dumps({"workspace_cwd": "relative"})) is None
    assert viewer.folder_from_context("bad json") is None
    assert viewer.folder_from_context(None) is None


@pytest.mark.parametrize(
    "payload",
    [
        "bad json",
        "{}",
        "[1]",
        json.dumps([{"id": 1}]),
        json.dumps([row(1, id=True)]),
        json.dumps([row(1, body=42)]),
        json.dumps([row(1, sender_cwd=42)]),
    ],
)
def test_rejects_malformed_history(payload: str) -> None:
    with pytest.raises(ValueError, match="llm-bus"):
        viewer.parse_messages(payload)


def test_empty_and_legacy_history() -> None:
    assert viewer.parse_messages("[]") == []
    message = viewer.parse_messages(json.dumps([row(1, sender_cwd=None, recipient_cwd=None)]))[0]
    assert message.sender_cwd is None
    assert "folders: unknown to unknown" in viewer.message_lines([message], 80)
    assert "wake: unrecorded" in viewer.message_lines([message], 80)
    assert "[pending]" in viewer.message_lines([message], 80)[0]


def test_reply_history_metadata_is_optional_and_validated() -> None:
    assert viewer.parse_messages(json.dumps([row(2, reply_to=1)]))[0].reply_to == 1
    assert viewer.parse_messages(json.dumps([row(1)]))[0].reply_to is None
    for invalid in (0, -1, True, "1"):
        with pytest.raises(ValueError, match="llm-bus"):
            viewer.parse_messages(json.dumps([row(2, reply_to=invalid)]))


def test_wake_result_is_visible() -> None:
    message = viewer.parse_messages(
        json.dumps(
            [
                row(
                    1,
                    wake_status="failed",
                    wake_via="codex queue",
                    wake_reason="not reachable",
                    wake_checked_at="2026-09-28T10:00:01Z",
                )
            ]
        )
    )[0]
    assert any(
        "wake: failed via codex queue at 2026-09-28T10:00:01Z (not reachable)" in line
        for line in viewer.message_lines([message], 100)
    )


def test_missing_bus_stays_visible() -> None:
    state = viewer.Viewer("", "/project")
    state.load()
    assert state.error == "llm-bus executable not found in PATH. Install llm-bus, then press r."


def test_history_command_uses_scope_and_before_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        calls.append(command)
        return CompletedProcess(command, 0, json.dumps([row(5)]), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert viewer.fetch_history("/bin/llm-bus", "/project", before=10)[0].id == 5
    assert calls[0] == [
        "/bin/llm-bus",
        "history",
        "--repository",
        "/project",
        "--limit",
        "50",
        "--before",
        "10",
    ]
    viewer.fetch_history("/bin/llm-bus", None)
    assert calls[1] == ["/bin/llm-bus", "history", "--all", "--limit", "50"]


def test_paging_and_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str | None, int | None]] = []

    def fetch(_binary: str, folder: str | None, before: int | None = None) -> list[viewer.Message]:
        calls.append((folder, before))
        ids = range(100, 50, -1) if before is None else range(50, 0, -1)
        return viewer.parse_messages(json.dumps([row(message_id) for message_id in ids]))

    monkeypatch.setattr(viewer, "fetch_history", fetch)
    state = viewer.Viewer("llm-bus", "/project")
    state.load()
    state.older()
    assert (state.page, calls[-1]) == (1, ("/project", 51))
    state.newer()
    assert state.page == 0
    state.switch("global")
    assert calls[-1] == (None, None)
    state.switch("folder")
    assert calls[-1] == ("/project", None)
    no_folder = viewer.Viewer("llm-bus", None)
    no_folder.switch("folder")
    assert no_folder.error is not None


def test_untrusted_text_cannot_emit_terminal_controls() -> None:
    message = viewer.parse_messages(
        json.dumps([row(1, body="hi\x1b[31m\n\u202eabc", sender="bad\x07sender")])
    )[0]
    lines = viewer.message_lines([message], 80)
    assert all("\x1b" not in line and "\x07" not in line and "\u202e" not in line for line in lines)
    assert any("hi?[31m" in line for line in lines)
