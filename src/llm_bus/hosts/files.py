"""Validated reads and guarded atomic writes for host settings."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import cast

from llm_bus.errors import BusError


# The maintenance runtime is copied standalone and cannot share application helpers.
# pylint: disable=duplicate-code
def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# pylint: enable=duplicate-code


def sync_parent_directories(path: Path) -> None:
    # Visible directories may survive a failed write without durable ancestor entries.
    for parent in reversed(path.absolute().parents):
        sync_directory(parent)


def current_bytes(path: Path) -> bytes | None:
    if path.is_symlink():
        raise BusError(f"Settings symlink is unsupported; edit its target explicitly: {path}")
    return path.read_bytes() if path.exists() else None


def parse_settings(path: Path, content: bytes | None) -> dict[str, object]:
    if content is None:
        return {}
    try:
        data = cast("object", json.loads(content))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise BusError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(data, dict):
        raise BusError(f"Settings file must contain a JSON object: {path}")
    return cast("dict[str, object]", data)


def atomic_write(path: Path, content: bytes, mode: int, expected: bytes | None) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    sync_parent_directories(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        Path(temporary).chmod(mode)
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if current_bytes(path) != expected:
            raise BusError(f"Settings changed while updating {path}")
        Path(temporary).replace(path)
        sync_directory(path.parent)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def write_settings(
    path: Path, settings: dict[str, object], expected: bytes | None, mode: int
) -> bytes:
    if current_bytes(path) != expected:
        raise BusError(f"Settings changed while updating {path}")
    content = (json.dumps(settings, indent=2) + "\n").encode()
    atomic_write(path, content, mode, expected)
    return content


type SettingsSnapshot = list[tuple[Path, bytes | None, int]]
