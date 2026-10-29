"""Install and remove user-level host hooks without replacing other settings."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import cast

from llm_bus_store import BusError

EVENTS = ("SessionStart", "UserPromptSubmit", "SessionEnd")
CLAUDE_ALLOW_RULE = "Bash(llm-bus *)"
CODEX_HOME_ENV = "CODEX_HOME"
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
MISSING_EXECUTABLE_MESSAGE = (
    "Cannot find llm-bus on PATH; run uv tool update-shell and retry installation"
)
type SettingsSnapshot = list[tuple[Path, bytes | None, int]]


def _codex_settings_path(home: Path) -> Path:
    codex_home = os.environ.get(CODEX_HOME_ENV)
    return (Path(codex_home).expanduser() if codex_home else home / ".codex") / "hooks.json"


def _hook_group(kind: str) -> dict[str, object]:
    executable = shutil.which("llm-bus")
    if executable is None:
        raise BusError(MISSING_EXECUTABLE_MESSAGE)
    arguments = [str(Path(executable).absolute()), "hook", kind]
    command = subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)
    return {"hooks": [{"type": "command", "command": command}]}


def _claude_settings_path(home: Path) -> Path:
    config_dir = os.environ.get(CLAUDE_CONFIG_DIR_ENV)
    return (Path(config_dir).expanduser() if config_dir else home / ".claude") / "settings.json"


def _is_bus_handler(handler: object, kind: str) -> bool:
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


def _without_bus_handler(group: object, kind: str) -> object | None:
    if not isinstance(group, dict):
        return group
    configured = cast("dict[str, object]", group)
    handlers = configured.get("hooks")
    if not isinstance(handlers, list):
        return configured
    retained = [
        handler for handler in cast("list[object]", handlers) if not _is_bus_handler(handler, kind)
    ]
    if not retained:
        return None
    return {**configured, "hooks": retained}


def _current_bytes(path: Path) -> bytes | None:
    if path.is_symlink():
        raise BusError(f"Settings symlink is unsupported; edit its target explicitly: {path}")
    return path.read_bytes() if path.exists() else None


def _parse_settings(path: Path, content: bytes | None) -> dict[str, object]:
    if content is None:
        return {}
    try:
        data = cast("object", json.loads(content))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise BusError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(data, dict):
        raise BusError(f"Settings file must contain a JSON object: {path}")
    return cast("dict[str, object]", data)


def _object_at(settings: dict[str, object], key: str, path: Path) -> dict[str, object]:
    value = settings.get(key, {})
    if not isinstance(value, dict):
        raise BusError(f"{key} must be a JSON object in {path}")
    return dict(cast("dict[str, object]", value))


def _update_hooks(
    settings: dict[str, object], path: Path, kind: str, *, install: bool
) -> dict[str, object]:
    updated = dict(settings)
    hooks = _object_at(settings, "hooks", path)
    for event in EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise BusError(f"hooks.{event} must be a JSON array in {path}")
        retained = [
            cleaned
            for group in cast("list[object]", groups)
            if (cleaned := _without_bus_handler(group, kind)) is not None
        ]
        if install:
            retained.append(_hook_group(kind))
        if retained:
            hooks[event] = retained
        else:
            hooks.pop(event, None)
    if not hooks:
        updated.pop("hooks", None)
    else:
        updated["hooks"] = hooks
    return updated


def _update_claude_permission(
    settings: dict[str, object], path: Path, *, install: bool
) -> dict[str, object]:
    updated = dict(settings)
    permissions = _object_at(settings, "permissions", path)
    allowed = permissions.get("allow", [])
    if not isinstance(allowed, list) or any(
        not isinstance(rule, str) for rule in cast("list[object]", allowed)
    ):
        raise BusError(f"permissions.allow must be an array of strings in {path}")
    rules = [rule for rule in cast("list[str]", allowed) if rule != CLAUDE_ALLOW_RULE]
    if install:
        rules.append(CLAUDE_ALLOW_RULE)
    if rules:
        permissions["allow"] = rules
    else:
        permissions.pop("allow", None)
    if not permissions:
        updated.pop("permissions", None)
    else:
        updated["permissions"] = permissions
    return updated


def _atomic_write(path: Path, content: bytes, mode: int, expected: bytes | None) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        Path(temporary).chmod(mode)
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
        if _current_bytes(path) != expected:
            raise BusError(f"Settings changed while updating {path}")
        Path(temporary).replace(path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def _write_settings(
    path: Path, settings: dict[str, object], expected: bytes | None, mode: int
) -> bytes:
    if _current_bytes(path) != expected:
        raise BusError(f"Settings changed while updating {path}")
    content = (json.dumps(settings, indent=2) + "\n").encode()
    _atomic_write(path, content, mode, expected)
    return content


def snapshot_host_settings(home: Path) -> SettingsSnapshot:
    """Capture both host settings from one validated read per file."""
    paths = (_codex_settings_path(home), _claude_settings_path(home))
    snapshots: SettingsSnapshot = []
    for path in paths:
        content = _current_bytes(path)
        _parse_settings(path, content)
        mode = stat.S_IMODE(path.stat().st_mode) if content is not None else 0o600
        snapshots.append((path, content, mode))
    return snapshots


def _group_has_handler(group: object, kind: str) -> bool:
    if not isinstance(group, dict):
        return False
    handlers = cast("dict[str, object]", group).get("hooks")
    return isinstance(handlers, list) and any(
        _is_bus_handler(handler, kind) for handler in cast("list[object]", handlers)
    )


def _insertion_index(current: list[object], original: list[object], position: int) -> int:
    for predecessor in reversed(original[:position]):
        if predecessor in current:
            return current.index(predecessor) + 1
    for successor in original[position + 1 :]:
        if successor in current:
            return current.index(successor)
    return min(position, len(current))


def _restore_claude_permission(
    current: dict[str, object], original: dict[str, object], path: Path
) -> dict[str, object]:
    restored = _update_claude_permission(current, path, install=False)
    original_permissions = _object_at(original, "permissions", path)
    original_allowed = cast("list[object]", original_permissions.get("allow", []))
    if CLAUDE_ALLOW_RULE not in original_allowed:
        return restored
    current_permissions = _object_at(restored, "permissions", path)
    current_allowed = cast("list[object]", current_permissions.get("allow", []))
    original_position = original_allowed.index(CLAUDE_ALLOW_RULE)
    current_allowed.insert(
        _insertion_index(current_allowed, original_allowed, original_position), CLAUDE_ALLOW_RULE
    )
    current_permissions["allow"] = current_allowed
    restored["permissions"] = current_permissions
    return restored


def _restore_hook_groups(
    current_groups: list[object], original_groups: list[object], kind: str
) -> list[object]:
    groups = list(current_groups)
    restored_positions: set[int] = set()
    for position, original_group in enumerate(original_groups):
        if not _group_has_handler(original_group, kind):
            continue
        without_bus = _without_bus_handler(original_group, kind)
        if without_bus is not None and without_bus in groups:
            groups[groups.index(without_bus)] = original_group
            restored_positions.add(position)
    for position, original_group in enumerate(original_groups):
        if _group_has_handler(original_group, kind) and position not in restored_positions:
            groups.insert(_insertion_index(groups, original_groups, position), original_group)
    return groups


def _restore_bus_entries(
    current: dict[str, object], original: dict[str, object], path: Path, kind: str
) -> dict[str, object]:
    restored = _update_hooks(current, path, kind, install=False)
    hooks = _object_at(restored, "hooks", path)
    original_hooks = _object_at(original, "hooks", path)
    for event in EVENTS:
        groups = _restore_hook_groups(
            cast("list[object]", hooks.get(event, [])),
            cast("list[object]", original_hooks.get(event, [])),
            kind,
        )
        if groups:
            hooks[event] = groups
    if hooks:
        restored["hooks"] = hooks
    if kind == "claude":
        restored = _restore_claude_permission(restored, original, path)
    return restored


def restore_bus_settings(snapshots: SettingsSnapshot) -> None:
    """Restore prior bus hooks into current settings after failed tool removal."""
    for index, (path, original_content, _) in enumerate(snapshots):
        current_content = _current_bytes(path)
        current = _parse_settings(path, current_content)
        original = _parse_settings(path, original_content)
        kind = "codex" if index == 0 else "claude"
        restored = _restore_bus_entries(current, original, path, kind)
        if restored != current:
            mode = stat.S_IMODE(path.stat().st_mode) if current_content is not None else 0o600
            _write_settings(path, restored, current_content, mode)


def _restore_written(snapshot: tuple[Path, bytes | None, int], written: bytes, kind: str) -> None:
    path, content, mode = snapshot
    current = _current_bytes(path)
    if current != written:
        restored = _restore_bus_entries(
            _parse_settings(path, current), _parse_settings(path, content), path, kind
        )
        if restored != _parse_settings(path, current):
            current_mode = stat.S_IMODE(path.stat().st_mode) if current is not None else 0o600
            _write_settings(path, restored, current, current_mode)
        return
    if content is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_write(path, content, mode, written)


def configure_hosts(
    home: Path, *, install: bool, snapshots: SettingsSnapshot | None = None
) -> list[Path]:
    """Merge or remove bus-owned hooks in both user settings files."""
    codex_path = _codex_settings_path(home)
    claude_path = _claude_settings_path(home)
    if snapshots is None:
        snapshots = snapshot_host_settings(home)
    codex_settings = _parse_settings(codex_path, snapshots[0][1])
    claude_settings = _parse_settings(claude_path, snapshots[1][1])
    codex_before = json.dumps(codex_settings, sort_keys=True)
    claude_before = json.dumps(claude_settings, sort_keys=True)
    codex_settings = _update_hooks(codex_settings, codex_path, "codex", install=install)
    claude_settings = _update_hooks(claude_settings, claude_path, "claude", install=install)
    claude_settings = _update_claude_permission(claude_settings, claude_path, install=install)
    written: list[tuple[tuple[Path, bytes | None, int], bytes, str]] = []
    try:
        if json.dumps(codex_settings, sort_keys=True) != codex_before:
            content = _write_settings(codex_path, codex_settings, snapshots[0][1], snapshots[0][2])
            written.append((snapshots[0], content, "codex"))
        if json.dumps(claude_settings, sort_keys=True) != claude_before:
            content = _write_settings(
                claude_path, claude_settings, snapshots[1][1], snapshots[1][2]
            )
            written.append((snapshots[1], content, "claude"))
    except (BusError, OSError):
        for snapshot, content, kind in reversed(written):
            _restore_written(snapshot, content, kind)
        raise
    return [codex_path, claude_path]
