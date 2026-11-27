"""Identify and merge bus-owned hook and permission entries."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import cast

from llm_bus.errors import BusError

EVENTS = ("SessionStart", "UserPromptSubmit", "SessionEnd")
# Gemini CLI names its prompt-submission event BeforeAgent.
GEMINI_EVENTS = ("SessionStart", "BeforeAgent", "SessionEnd")


CLAUDE_ALLOW_RULE = "Bash(llm-bus *)"
# Antigravity runs agent commands in a terminal sandbox that hides the bus mailbox. A command
# allow rule runs matching commands, by token prefix, without prompting and outside the sandbox.
AGY_ALLOW_RULE = "command(llm-bus)"
# Antigravity hooks.json maps hook names to event handlers; the bus owns this name.
AGY_HOOK_NAME = "llm-bus"
# Antigravity runs PreInvocation before every model call; the bus hook ignores all but the first
# of each turn. It has no session-end event.
AGY_EVENTS = ("SessionStart", "PreInvocation")
# Settings kind of Antigravity's own settings.json, which carries only the allow rule.
AGY_SETTINGS = "agy-settings"


def host_events(kind: str) -> tuple[str, ...]:
    return GEMINI_EVENTS if kind == "gemini" else EVENTS


def settings_kind(path: Path) -> str:
    """The host owning a settings file.

    Antigravity keeps hooks in `.gemini/config/hooks.json` and permissions in
    `.gemini/antigravity-cli/settings.json`; Codex uses any other `hooks.json`; Gemini CLI uses
    `.gemini/settings.json`; anything else is Claude's.
    """
    if path.parent.parent.name == ".gemini":
        if path.name == "hooks.json" and path.parent.name == "config":
            return "agy"
        if path.name == "settings.json" and path.parent.name == "antigravity-cli":
            return AGY_SETTINGS
    if path.name == "hooks.json":
        return "codex"
    return "gemini" if path.parent.name == ".gemini" else "claude"


MISSING_EXECUTABLE_MESSAGE = (
    "Cannot find llm-bus on PATH; run uv tool update-shell and retry installation"
)


def hook_group(kind: str) -> dict[str, object]:
    executable = shutil.which("llm-bus")
    if executable is None:
        raise BusError(MISSING_EXECUTABLE_MESSAGE)
    arguments = [str(Path(executable).absolute()), "hook", kind]
    command = subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)
    return {"hooks": [{"type": "command", "command": command}]}


def is_bus_handler(handler: object, kind: str) -> bool:
    if not isinstance(handler, dict):
        return False
    configured = cast("dict[str, object]", handler)
    command = configured.get("command")
    if configured.get("type") != "command" or not isinstance(command, str):
        return False
    try:
        arguments = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return False
    return (
        bool(arguments)
        and Path(arguments[0].strip('"')).name.lower() in {"llm-bus", "llm-bus.exe"}
        and arguments[1:] == ["hook", kind]
    )


def without_bus_handler(group: object, kind: str) -> object | None:
    if not isinstance(group, dict):
        return group
    configured = cast("dict[str, object]", group)
    handlers = configured.get("hooks")
    if not isinstance(handlers, list):
        return configured
    retained = [
        handler for handler in cast("list[object]", handlers) if not is_bus_handler(handler, kind)
    ]
    if not retained:
        return None
    return {**configured, "hooks": retained}


def replace_bus_handler(
    groups: list[object], kind: str, canonical: dict[str, object]
) -> list[object]:
    """Install the bus handler where it already sits so other groups keep their indexes."""
    handler = cast("list[object]", canonical["hooks"])[0]
    result: list[object] = []
    placed = False
    for group in groups:
        configured: dict[str, object] = (
            cast("dict[str, object]", group) if isinstance(group, dict) else {}
        )
        group = cast("object", group)
        handlers = configured.get("hooks")
        if not isinstance(handlers, list):
            result.append(group)
            continue
        handlers = cast("list[object]", handlers)
        if not any(is_bus_handler(item, kind) for item in handlers):
            result.append(group)
            continue
        pure = all(is_bus_handler(item, kind) for item in handlers)
        if not placed and pure:
            result.append(canonical)
            placed = True
        elif not placed and set(configured) == {"hooks"}:
            first = next(i for i, item in enumerate(handlers) if is_bus_handler(item, kind))
            result.append(
                {
                    "hooks": [
                        handler if i == first else item
                        for i, item in enumerate(handlers)
                        if i == first or not is_bus_handler(item, kind)
                    ]
                }
            )
            placed = True
        elif (cleaned := without_bus_handler(group, kind)) is not None:
            result.append(cleaned)
    if not placed:
        result.append(canonical)
    return result


def object_at(settings: dict[str, object], key: str, path: Path) -> dict[str, object]:
    value = settings.get(key, {})
    if not isinstance(value, dict):
        raise BusError(f"{key} must be a JSON object in {path}")
    return dict(cast("dict[str, object]", value))


def update_hooks(
    settings: dict[str, object], path: Path, kind: str, *, install: bool
) -> dict[str, object]:
    updated = dict(settings)
    hooks = object_at(settings, "hooks", path)
    canonical = hook_group(kind) if install else {}
    for event in host_events(kind):
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise BusError(f"hooks.{event} must be a JSON array in {path}")
        group_list = cast("list[object]", groups)
        retained = (
            replace_bus_handler(group_list, kind, canonical)
            if install
            else [
                cleaned
                for group in group_list
                if (cleaned := without_bus_handler(group, kind)) is not None
            ]
        )
        if retained:
            hooks[event] = retained
        else:
            hooks.pop(event, None)
    if not hooks:
        updated.pop("hooks", None)
    else:
        updated["hooks"] = hooks
    return updated


def update_agy_hooks(settings: dict[str, object], *, install: bool) -> dict[str, object]:
    """Install or remove the bus-owned named hook in Antigravity's hooks.json."""
    updated = dict(settings)
    if not install:
        updated.pop(AGY_HOOK_NAME, None)
        return updated
    handler = cast("list[object]", hook_group("agy")["hooks"])[0]
    updated[AGY_HOOK_NAME] = {event: [handler] for event in AGY_EVENTS}
    return updated


def update_claude_permission(
    settings: dict[str, object], path: Path, *, install: bool
) -> dict[str, object]:
    return update_allow_rule(settings, path, CLAUDE_ALLOW_RULE, install=install)


def update_allow_rule(
    settings: dict[str, object], path: Path, rule: str, *, install: bool
) -> dict[str, object]:
    """Add or remove one bus allow rule in a host's `permissions.allow` array."""
    updated = dict(settings)
    permissions = object_at(settings, "permissions", path)
    allowed = permissions.get("allow", [])
    if not isinstance(allowed, list) or any(
        not isinstance(rule, str) for rule in cast("list[object]", allowed)
    ):
        raise BusError(f"permissions.allow must be an array of strings in {path}")
    rules = [item for item in cast("list[str]", allowed) if item != rule]
    if install:
        rules.append(rule)
    if rules:
        permissions["allow"] = rules
    else:
        permissions.pop("allow", None)
    if not permissions:
        updated.pop("permissions", None)
    else:
        updated["permissions"] = permissions
    return updated


def apply_bus_settings(
    settings: dict[str, object], path: Path, kind: str, *, install: bool
) -> dict[str, object]:
    """Install or remove the bus hooks, or the host's llm-bus allow rule, in one settings file."""
    if kind == "agy":
        return update_agy_hooks(settings, install=install)
    if kind == AGY_SETTINGS:
        return update_allow_rule(settings, path, AGY_ALLOW_RULE, install=install)
    updated = update_hooks(settings, path, kind, install=install)
    return update_claude_permission(updated, path, install=install) if kind == "claude" else updated
