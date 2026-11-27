"""Install and uninstall host integration and the uv tool."""

import shutil
import subprocess
from pathlib import Path

from llm_bus.errors import BusError
from llm_bus.hosts.hooks import settings_kind
from llm_bus.hosts.restore import restore_bus_settings
from llm_bus.hosts.settings import configure_hosts, snapshot_host_settings


def configure_host_hooks(*, install: bool) -> dict[str, object]:
    home = Path.home()
    if install:
        paths = configure_hosts(home, install=True)
    else:
        uv = shutil.which("uv")
        if uv is None:
            raise BusError("uv is required to uninstall the llm-bus tool")
        snapshots = snapshot_host_settings(home)
        paths = configure_hosts(home, install=False, snapshots=snapshots)
        try:
            subprocess.run([uv, "tool", "uninstall", "llm-bus"], check=True)  # noqa: S603
        except (OSError, subprocess.CalledProcessError):
            restore_bus_settings(snapshots)
            raise
    result: dict[str, object] = {
        "installed": install,
        "settings": [str(path) for path in paths],
    }
    if install:
        result["codex_next_step"] = "Restart Codex, then review and trust llm-bus hooks in /hooks"
        if any(settings_kind(path) == "gemini" for path in paths):
            result["gemini_next_step"] = "Restart Gemini CLI to load llm-bus hooks"
        if any(settings_kind(path) == "agy" for path in paths):
            result["agy_next_step"] = "Restart Antigravity (agy) to load llm-bus hooks"
    return result
