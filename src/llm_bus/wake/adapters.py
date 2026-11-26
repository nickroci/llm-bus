"""Best-effort host wake requests after durable bus delivery."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import stat
import subprocess
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from llm_bus.hosts.sessions import is_subagent

WAKE_TIMEOUT_SECONDS = 10
SQLITE_MAX_MESSAGE_ID = 2**63 - 1
WAKE_FAILED = "failed"
CLAUDE_WAKE_REASON = (
    "Claude native inbox is unavailable; pending messages are reported by the next turn hook"
)
CLAUDE_WAKE_VIA = "Claude inbox socket"
CLAUDE_SOCKET_FAILURE_REASON = "Claude inbox socket could not be reached; message remains pending"
HERDR_WAKE_VIA = "herdr agent prompt"
GEMINI_PANE_CHANGED = "Herdr pane no longer runs Gemini; message remains pending"
GEMINI_WAKE_REASON = (
    "Gemini wake needs the session to run in a Herdr pane with herdr on PATH; "
    "pending messages are reported by the next turn hook"
)
WAKE_NOTICE = re.compile(
    r"llm-bus message #([1-9][0-9]{0,18}) from (\S+)\. "
    r"Read and handle it with `llm-bus inbox`\."
)


def parse_wake_notice(prompt: object) -> tuple[int, str] | None:
    """Recognize only a complete bus wake, never a prompt quoting or extending one."""
    if not isinstance(prompt, str):
        return None
    match = WAKE_NOTICE.fullmatch(prompt)
    if match is None:
        return None
    message_id = int(match[1])
    if message_id > SQLITE_MAX_MESSAGE_ID:
        return None
    return message_id, match[2]


def contains_wake_notice(prompt: object) -> bool:
    """A complete bus wake anywhere in the prompt, such as inside a host wrapper.

    Use only to mark a turn as not owner-started: a false match can refuse an
    answer, never permit an action.
    """
    return isinstance(prompt, str) and any(
        int(match[1]) <= SQLITE_MAX_MESSAGE_ID for match in WAKE_NOTICE.finditer(prompt)
    )


class WakeResult(TypedDict):
    status: str
    via: str
    reason: NotRequired[str]


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv only, no shell
        command, capture_output=True, text=True, timeout=WAKE_TIMEOUT_SECONDS, check=False
    )


def _failure(result: subprocess.CompletedProcess[str], via: str) -> WakeResult:
    detail = result.stderr.strip().splitlines()
    reason = detail[-1] if detail else f"{via} exited with status {result.returncode}"
    return {"status": WAKE_FAILED, "via": via, "reason": reason}


def _wake_codex(session_id: str, notice: str) -> WakeResult:
    binary = shutil.which("codex")
    if binary is None:
        return {"status": WAKE_FAILED, "via": "codex queue", "reason": "codex is not on PATH"}
    command = [
        binary,
        "queue",
        "--resume",
        "--thread",
        session_id,
        "--message",
        notice,
    ]
    try:
        result = _run(command)
        if result.returncode != 0 and "unexpected argument '--resume' found" in result.stderr:
            # Older Codex rejects the flag before queueing; retrying cannot duplicate a notice.
            command.remove("--resume")
            result = _run(command)
    except subprocess.TimeoutExpired:
        return {"status": "unknown", "via": "codex queue", "reason": "wake request timed out"}
    except OSError as exc:
        return {"status": WAKE_FAILED, "via": "codex queue", "reason": str(exc)}
    if result.returncode != 0 and (
        "direct app-server input is not allowed for multi-agent v2 sub-agents" in result.stderr
        or "direct app-server input is not allowed for unloaded spawned sub-agents" in result.stderr
    ):
        return {
            "status": "unsupported",
            "via": "codex queue",
            "reason": "Codex sub-agent wakes are unsupported; address its parent coordinator",
        }
    return (
        {"status": "requested", "via": "codex queue"}
        if result.returncode == 0
        else _failure(result, "codex queue")
    )


def _wake_claude(session_id: str, notice: str, socket_path: str | None) -> WakeResult:
    if not socket_path or os.name == "nt":
        return {"status": "unsupported", "via": "none", "reason": CLAUDE_WAKE_REASON}
    failure_status = WAKE_FAILED
    try:
        endpoint = Path(socket_path)
        metadata = endpoint.lstat()
        if (
            not endpoint.is_absolute()
            or not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            return {
                "status": WAKE_FAILED,
                "via": CLAUDE_WAKE_VIA,
                "reason": (
                    "Claude inbox must be a local socket owned by this user with private writes"
                ),
            }
        frame = {
            "type": "user",
            "session_id": session_id,
            "message": {
                "role": "user",
                "content": notice,
            },
            "priority": "next",
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(WAKE_TIMEOUT_SECONDS)
            connection.connect(socket_path)
            failure_status = "unknown"
            connection.sendall((json.dumps(frame) + "\n").encode("utf-8"))
    except TimeoutError:
        return {"status": "unknown", "via": CLAUDE_WAKE_VIA, "reason": "wake request timed out"}
    except OSError:
        return {
            "status": failure_status,
            "via": CLAUDE_WAKE_VIA,
            "reason": CLAUDE_SOCKET_FAILURE_REASON,
        }
    return {"status": "requested", "via": CLAUDE_WAKE_VIA}


def _herdr_agent(binary: str, pane: str) -> object:
    """The agent Herdr currently detects in a pane, or None."""
    result = _run([binary, "agent", "get", pane])
    if result.returncode != 0:
        return None
    try:
        response = cast("object", json.loads(result.stdout))
    except json.JSONDecodeError:
        return None
    for key in ("result", "agent"):
        if not isinstance(response, dict):
            return None
        response = cast("dict[str, object]", response).get(key)
    return response


def _wake_gemini(notice: str, pane: str | None) -> WakeResult:
    """Submit the notice to the session's Herdr pane after checking it still runs Gemini.

    Herdr types the notice and Enter into the terminal, so a wake that races an unsubmitted
    owner prompt can append to and submit it. Gemini exposes no other way to start a turn.
    """
    binary = shutil.which("herdr")
    if not pane or binary is None:
        return {"status": "unsupported", "via": "none", "reason": GEMINI_WAKE_REASON}
    try:
        agent = _herdr_agent(binary, pane)
        if not isinstance(agent, dict) or cast("dict[str, object]", agent).get("agent") != "gemini":
            return {
                "status": WAKE_FAILED,
                "via": HERDR_WAKE_VIA,
                "reason": GEMINI_PANE_CHANGED,
            }
        result = _run([binary, "agent", "prompt", pane, notice])
    except subprocess.TimeoutExpired:
        return {"status": "unknown", "via": HERDR_WAKE_VIA, "reason": "wake request timed out"}
    except OSError as exc:
        return {"status": WAKE_FAILED, "via": HERDR_WAKE_VIA, "reason": str(exc)}
    return (
        {"status": "requested", "via": HERDR_WAKE_VIA}
        if result.returncode == 0
        else _failure(result, HERDR_WAKE_VIA)
    )


def wake(
    recipient: str, message_id: int, sender: str, *, endpoint: str | None = None
) -> WakeResult:
    """Request a host turn without risking the already-committed bus message."""
    if is_subagent(recipient):
        return {"status": "unsupported", "via": "none", "reason": "Address the parent main agent"}
    notice = (
        f"llm-bus message #{message_id} from {sender}. Read and handle it with `llm-bus inbox`."
    )
    kind, separator, session_id = recipient.partition(":")
    if not separator or not session_id:
        return {"status": "unsupported", "via": "none", "reason": "Invalid recipient address"}
    if kind == "codex":
        return _wake_codex(session_id, notice)
    if kind == "claude":
        return _wake_claude(session_id, notice, endpoint)
    if kind == "gemini":
        return _wake_gemini(notice, endpoint)
    return {"status": "unsupported", "via": "none", "reason": "Unknown recipient kind"}
