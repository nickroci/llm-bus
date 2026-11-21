"""Build and smoke-test the installed distribution outside the source checkout."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast

COLLAB_EXTRA_MODULES = frozenset(
    {
        "llm_bus.collab.daemon",
        "llm_bus.collab.transport",
        "llm_bus.collab.pairing",
        "llm_bus.collab.delivery",
    }
)
BUS_DB_ENV = "LLM_BUS_DB"
BUS_CONFIG_ENV = "LLM_BUS_CONFIG"
CODEX_THREAD_ENV = "CODEX_THREAD_ID"
GUIDANCE_MISSING = "Installed wake/resume guidance omits the active run or recovery reference"


def unattended_guidance(cli: Path, sandbox: Path, root: Path, environment: dict[str, str]) -> None:
    """Smoke installed commands and copied guidance with a synthetic host callback."""
    fixture = environment.copy()
    for name in (
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        CODEX_THREAD_ENV,
        "CODEX_SESSION_ID",
    ):
        fixture.pop(name, None)
    fixture.update(
        {
            BUS_DB_ENV: str(sandbox / "acceptance.db"),
            BUS_CONFIG_ENV: str(sandbox / "missing.toml"),
            CODEX_THREAD_ENV: "package-owner",
        }
    )
    (sandbox / ".beads").mkdir()
    references = sandbox / "skills" / "llm-bus"
    shutil.copytree(root / "skills" / "llm-bus", references)

    def invoke(*argv: str, payload: dict[str, str] | None = None) -> str:
        result = subprocess.run(  # noqa: S603 - isolated installed CLI and fixed fixture arguments
            [str(cli), *argv],
            cwd=sandbox,
            env=fixture,
            input=None if payload is None else json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Installed fixture command failed: {argv!r}: {result.stderr}")
        return result.stdout

    profile = cast(
        "dict[str, object]", json.loads(invoke("coordinate", "package-smoke", "--setup"))
    )
    if cast("dict[str, object]", profile["mode"])["choices"] != ["unattended", "guided"]:
        raise ValueError("Installed setup does not offer unattended first")
    callback = {
        "session_id": "package-owner",
        "hook_event_name": "UserPromptSubmit",
        "cwd": str(sandbox),
        "prompt": "Owner confirms the isolated package smoke fixture",
    }
    invoke(
        "coordinate",
        "package-smoke",
        "--objective",
        "Package acceptance",
        "--beads-workspace",
        str(sandbox),
        "--memory-ticket",
        "package-1",
        "--owner-decisions",
    )
    invoke("hook", "codex", payload=callback)
    activated = cast(
        "dict[str, object]",
        json.loads(
            invoke(
                "coordination",
                "unattended",
                "package-smoke",
                "--objective",
                "Package acceptance",
                "--ticket",
                "package-1",
                "--completion",
                "Reviewed PR ready",
                "--expires-at",
                "2099-01-01T00:00:00Z",
                "--permission",
                "owner-decisions",
                "--confirm",
            )
        ),
    )
    run_id = str(activated["run_id"])
    callback["prompt"] = (
        "llm-bus message #1 from claude:package-worker. Read and handle it with `llm-bus inbox`."
    )
    invoke("hook", "codex", payload=callback)
    notice = invoke("--format", "agent", "guidance")
    if run_id not in notice or "recovery.md" not in notice:
        raise ValueError(GUIDANCE_MISSING)
    if not (references / "references/coordination/recovery.md").is_file():
        raise ValueError("Copied recovery guidance is missing")


def application_modules(source: Path) -> list[str]:
    """Inventory modules and package initializers without executing entrypoints."""
    modules: set[str] = set()
    for path in source.rglob("*.py"):
        relative = path.relative_to(source).with_suffix("")
        if relative.name == "__main__":
            continue
        if relative.name == "__init__":
            relative = relative.parent
        modules.add(".".join(relative.parts))
    if not modules:
        raise ValueError("No application modules found for package check")
    return sorted(modules)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    uv = shutil.which("uv")
    if uv is None:
        raise FileNotFoundError("uv is required for the package check")
    environment = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME"):
        environment.pop(key, None)
    with tempfile.TemporaryDirectory(prefix="llm-bus-package-") as temporary:
        sandbox = Path(temporary)
        wheels = sandbox / "dist"
        subprocess.run(  # noqa: S603 - resolved executable and fixed argv, no shell
            [uv, "build", "--out-dir", str(wheels)],
            cwd=root,
            env=environment,
            check=True,
        )
        virtualenv = sandbox / "venv"
        subprocess.run(  # noqa: S603 - resolved executable and fixed argv, no shell
            [uv, "venv", "--python", sys.executable, str(virtualenv)],
            cwd=sandbox,
            env=environment,
            check=True,
        )
        binary_directory = virtualenv / ("Scripts" if os.name == "nt" else "bin")
        python = binary_directory / ("python.exe" if os.name == "nt" else "python")
        wheel = next(wheels.glob("*.whl"))
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [uv, "pip", "install", "--python", str(python), "--no-deps", str(wheel)],
            cwd=sandbox,
            env=environment,
            check=True,
        )
        modules = application_modules(root / "src")
        core = [name for name in modules if name not in COLLAB_EXTRA_MODULES]
        importer = "import importlib, sys; [importlib.import_module(n) for n in sys.argv[1:]]"
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [str(python), "-I", "-c", importer, *core], cwd=sandbox, env=environment, check=True
        )
        subprocess.run(  # noqa: S603 - isolated module entry point, no user state
            [str(python), "-I", "-m", "llm_bus", "--help"],
            cwd=sandbox,
            env=environment,
            check=True,
        )
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [uv, "pip", "install", "--python", str(python), f"{wheel}[collab]"],
            cwd=sandbox,
            env=environment,
            check=True,
        )
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [str(python), "-I", "-c", importer, *sorted(COLLAB_EXTRA_MODULES)],
            cwd=sandbox,
            env=environment,
            check=True,
        )
        cli = binary_directory / ("llm-bus.exe" if os.name == "nt" else "llm-bus")
        subprocess.run(  # noqa: S603 - installed entry point; help does not access user state
            [str(cli), "--help"], cwd=sandbox, env=environment, check=True
        )
        unattended_guidance(cli, sandbox, root, environment)


if __name__ == "__main__":
    main()
