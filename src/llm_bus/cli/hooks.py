"""Host lifecycle registration and wake notices."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

from llm_bus.cli.guidance import GUIDANCE_VERSION, HOOK_COUNT_LIMIT, Delivery, guidance
from llm_bus.coordination.decisions import record_turn
from llm_bus.errors import BusError
from llm_bus.hosts.processes import host_process
from llm_bus.hosts.sessions import is_subagent
from llm_bus.wake.adapters import contains_wake_notice, parse_wake_notice

if TYPE_CHECKING:
    from llm_bus.mailbox.store import Store

CLAUDE_SOCKET_ENV = "CLAUDE_CODE_MESSAGING_SOCKET"
# Claude Code caps hook output at 10,000 characters (unit undocumented) and silently saves an
# oversized one to a file; Codex caps at 2,500 tokens, about 4 UTF-8 bytes each. Bytes are never
# fewer than characters, and the slack covers the newline.
HOOK_TEXT_LIMIT = 9000
HOOK_FALLBACK = (
    "Local agent bus address: {address}. llm-bus guidance exceeds the hook output limit. "
    "Run `llm-bus --format agent guidance` now and read its whole output, including any saved "
    "file, before any other llm-bus action or acting on any direction."
)


def _acknowledged_wake(store: Store, recipient: str, prompt: object) -> bool:
    identity = parse_wake_notice(prompt)
    return identity is not None and store.message_acknowledged(recipient, *identity)


def _guard(notice: str, address: str) -> str:
    """Never print more than the host accepts: oversized guidance becomes one fallback line."""
    if len(notice.encode("utf-8")) > HOOK_TEXT_LIMIT:
        return HOOK_FALLBACK.format(address=address)
    return notice


def hook(store: Store, kind: str) -> None:
    hook_input = cast("object", json.load(sys.stdin))
    if not isinstance(hook_input, dict):
        raise BusError("Hook input must be a JSON object")
    hook_data = cast("dict[str, object]", hook_input)
    session_id = hook_data.get("session_id")
    event = hook_data.get("hook_event_name")
    if not isinstance(session_id, str) or event not in {
        "SessionStart",
        "UserPromptSubmit",
        "SessionEnd",
    }:
        raise BusError("Hook input lacks a valid session_id or event")
    agent_id = hook_data.get("agent_id")
    # Sub-agent hooks on both hosts carry the root session_id plus the child's agent_id.
    child = isinstance(agent_id, str) and bool(agent_id)
    if event == "SessionEnd":
        if not child:
            store.end_session(kind, session_id)
        return
    if child or is_subagent(f"{kind}:{session_id}"):
        notice = (
            "Sub-agent: do not use llm-bus. "
            "Report only to your parent through native agent communication."
        )
        print(
            json.dumps(
                {"hookSpecificOutput": {"hookEventName": event, "additionalContext": notice}}
            )
            if kind == "codex"
            else notice
        )
        return
    project = hook_data.get("cwd")
    if not isinstance(project, str):
        project = str(Path.cwd())
    project = str(Path(project).resolve())
    agent_address = store.register(
        kind, session_id, project, messaging_socket=claude_socket(kind), host=host_process(kind)
    )
    prompt = hook_data.get("prompt")
    if event == "UserPromptSubmit" and isinstance(prompt, str) and prompt.strip():
        # A prompt containing a complete bus wake notice (hosts may wrap it) marks the turn as
        # not owner-started; see decisions.record_turn.
        record_turn(store, agent_address, wake=contains_wake_notice(prompt))
    count = store.pending_count(agent_address, limit=HOOK_COUNT_LIMIT)
    if (
        kind == "codex"
        and event == "UserPromptSubmit"
        and count == 0
        and _acknowledged_wake(store, agent_address, prompt)
    ):
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": event,
                        "additionalContext": (
                            "This bus wake refers to an already acknowledged message, "
                            "and the inbox is empty. No action is needed for this notice. "
                            "Do not retry or acknowledge it again."
                        ),
                    },
                }
            )
        )
        return
    full = store.guidance_needed(agent_address, GUIDANCE_VERSION, reset=event == "SessionStart")
    notice = _guard(
        guidance(store, agent_address, project, full=full, delivery=Delivery.HOOK), agent_address
    )
    if kind == "codex":
        print(
            json.dumps(
                {"hookSpecificOutput": {"hookEventName": event, "additionalContext": notice}}
            )
        )
    else:
        print(notice)


def claude_socket(kind: str) -> str | None:
    return os.environ.get(CLAUDE_SOCKET_ENV) if kind == "claude" else None
