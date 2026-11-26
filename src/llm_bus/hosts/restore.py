"""Merge prior bus settings back after failed installation changes."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING, cast

from llm_bus.hosts import files
from llm_bus.hosts.hooks import (
    CLAUDE_ALLOW_RULE,
    host_events,
    is_bus_handler,
    object_at,
    settings_kind,
    update_claude_permission,
    update_hooks,
    without_bus_handler,
)

if TYPE_CHECKING:
    from pathlib import Path


def group_has_handler(group: object, kind: str) -> bool:
    if not isinstance(group, dict):
        return False
    handlers = cast("dict[str, object]", group).get("hooks")
    return isinstance(handlers, list) and any(
        is_bus_handler(handler, kind) for handler in cast("list[object]", handlers)
    )


def insertion_index(current: list[object], original: list[object], position: int) -> int:
    for predecessor in reversed(original[:position]):
        if predecessor in current:
            return current.index(predecessor) + 1
    for successor in original[position + 1 :]:
        if successor in current:
            return current.index(successor)
    return min(position, len(current))


def restore_claude_permission(
    current: dict[str, object], original: dict[str, object], path: Path
) -> dict[str, object]:
    restored = update_claude_permission(current, path, install=False)
    original_permissions = object_at(original, "permissions", path)
    original_allowed = cast("list[object]", original_permissions.get("allow", []))
    if CLAUDE_ALLOW_RULE not in original_allowed:
        return restored
    current_permissions = object_at(restored, "permissions", path)
    current_allowed = cast("list[object]", current_permissions.get("allow", []))
    original_position = original_allowed.index(CLAUDE_ALLOW_RULE)
    current_allowed.insert(
        insertion_index(current_allowed, original_allowed, original_position), CLAUDE_ALLOW_RULE
    )
    current_permissions["allow"] = current_allowed
    restored["permissions"] = current_permissions
    return restored


def restore_hook_groups(
    current_groups: list[object], original_groups: list[object], kind: str
) -> list[object]:
    groups = list(current_groups)
    restored_positions: set[int] = set()
    for position, original_group in enumerate(original_groups):
        if not group_has_handler(original_group, kind):
            continue
        without_bus = without_bus_handler(original_group, kind)
        if without_bus is not None and without_bus in groups:
            groups[groups.index(without_bus)] = original_group
            restored_positions.add(position)
    for position, original_group in enumerate(original_groups):
        if group_has_handler(original_group, kind) and position not in restored_positions:
            groups.insert(insertion_index(groups, original_groups, position), original_group)
    return groups


def restore_bus_entries(
    current: dict[str, object], original: dict[str, object], path: Path, kind: str
) -> dict[str, object]:
    restored = update_hooks(current, path, kind, install=False)
    hooks = object_at(restored, "hooks", path)
    original_hooks = object_at(original, "hooks", path)
    for event in host_events(kind):
        groups = restore_hook_groups(
            cast("list[object]", hooks.get(event, [])),
            cast("list[object]", original_hooks.get(event, [])),
            kind,
        )
        if groups:
            hooks[event] = groups
    if hooks:
        restored["hooks"] = hooks
    if kind == "claude":
        restored = restore_claude_permission(restored, original, path)
    return restored


def restore_bus_settings(snapshots: files.SettingsSnapshot) -> None:
    """Restore prior bus hooks into current settings after failed tool removal."""
    for path, original_content, _ in snapshots:
        current_content = files.current_bytes(path)
        current = files.parse_settings(path, current_content)
        original = files.parse_settings(path, original_content)
        kind = settings_kind(path)
        restored = restore_bus_entries(current, original, path, kind)
        if restored != current:
            mode = stat.S_IMODE(path.stat().st_mode) if current_content is not None else 0o600
            files.write_settings(path, restored, current_content, mode)


def restore_written(snapshot: tuple[Path, bytes | None, int], written: bytes, kind: str) -> None:
    path, content, mode = snapshot
    current = files.current_bytes(path)
    if current != written:
        restored = restore_bus_entries(
            files.parse_settings(path, current), files.parse_settings(path, content), path, kind
        )
        if restored != files.parse_settings(path, current):
            current_mode = stat.S_IMODE(path.stat().st_mode) if current is not None else 0o600
            files.write_settings(path, restored, current, current_mode)
        return
    if content is None:
        path.unlink(missing_ok=True)
    else:
        files.atomic_write(path, content, mode, written)
