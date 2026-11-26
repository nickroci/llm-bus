"""Install and remove user-level host hooks without replacing other settings."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import cast

from llm_bus.errors import BusError
from llm_bus.hosts import files
from llm_bus.hosts.hooks import apply_bus_settings, settings_kind
from llm_bus.hosts.restore import restore_written

CODEX_HOME_ENV = "CODEX_HOME"
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
GEMINI_CLI_HOME_ENV = "GEMINI_CLI_HOME"
CODEX_HOMES_KEY = "codex_homes"


def native_metadata_path(home: Path) -> Path:
    return home / ".local/share/llm-bus/native-hosts.json"


def codex_metadata_homes(home: Path) -> list[Path]:
    """Use installed host bindings across shells; retain legacy lookup before installation."""
    path = native_metadata_path(home)
    content = files.current_bytes(path)
    if content is None:
        return [codex_settings_path(home).parent.resolve()]
    settings = files.parse_settings(path, content)
    values = settings.get(CODEX_HOMES_KEY)
    if set(settings) != {CODEX_HOMES_KEY} or not isinstance(values, list) or not values:
        raise BusError("Invalid native host metadata bindings; rerun llm-bus install")
    paths: list[Path] = []
    for value in cast("list[object]", values):
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise BusError("Native Codex home bindings must be absolute paths")
        paths.append(Path(value))
    return paths


def remember_codex_home(home: Path) -> None:
    """Persist installed homes so bridges and Claude do not guess another process's home."""
    path = native_metadata_path(home)
    before = files.current_bytes(path)
    homes = {*codex_metadata_homes(home), codex_settings_path(home).parent.resolve()}
    settings: dict[str, object] = {CODEX_HOMES_KEY: sorted(str(item) for item in homes)}
    if before is None or files.parse_settings(path, before) != settings:
        files.write_settings(path, settings, before, 0o600)


def codex_settings_path(home: Path) -> Path:
    codex_home = os.environ.get(CODEX_HOME_ENV)
    return (Path(codex_home).expanduser() if codex_home else home / ".codex") / "hooks.json"


def claude_settings_path(home: Path) -> Path:
    config_dir = os.environ.get(CLAUDE_CONFIG_DIR_ENV)
    return (Path(config_dir).expanduser() if config_dir else home / ".claude") / "settings.json"


def gemini_settings_path(home: Path) -> Path:
    gemini_home = os.environ.get(GEMINI_CLI_HOME_ENV)
    return (Path(gemini_home).expanduser() if gemini_home else home) / ".gemini" / "settings.json"


def host_settings_paths(home: Path) -> list[Path]:
    """Codex and Claude settings always; Gemini settings once Gemini CLI has created its folder."""
    paths = [codex_settings_path(home), claude_settings_path(home)]
    gemini = gemini_settings_path(home)
    if gemini.parent.is_dir():
        paths.append(gemini)
    return paths


def snapshot_host_settings(home: Path) -> files.SettingsSnapshot:
    """Capture every host's settings from one validated read per file."""
    paths = host_settings_paths(home)
    snapshots: files.SettingsSnapshot = []
    for path in paths:
        content = files.current_bytes(path)
        files.parse_settings(path, content)
        mode = stat.S_IMODE(path.stat().st_mode) if content is not None else 0o600
        snapshots.append((path, content, mode))
    return snapshots


def configure_hosts(
    home: Path, *, install: bool, snapshots: files.SettingsSnapshot | None = None
) -> list[Path]:
    """Merge or remove bus-owned hooks in every host's user settings file."""
    if snapshots is None:
        snapshots = snapshot_host_settings(home)
    # Validate and compute every file before writing any of them.
    changes: list[tuple[tuple[Path, bytes | None, int], dict[str, object], str]] = []
    for snapshot in snapshots:
        path, content, _mode = snapshot
        kind = settings_kind(path)
        settings = files.parse_settings(path, content)
        updated = apply_bus_settings(settings, path, kind, install=install)
        if json.dumps(updated, sort_keys=True) != json.dumps(settings, sort_keys=True):
            changes.append((snapshot, updated, kind))
    written: list[tuple[tuple[Path, bytes | None, int], bytes, str]] = []
    try:
        for snapshot, updated, kind in changes:
            content = files.write_settings(snapshot[0], updated, snapshot[1], snapshot[2])
            written.append((snapshot, content, kind))
        if install:
            remember_codex_home(home)
    except (BusError, OSError):
        for snapshot, content, kind in reversed(written):
            restore_written(snapshot, content, kind)
        raise
    return [path for path, _content, _mode in snapshots]
