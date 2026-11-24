"""Host identity and command dispatch."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import llm_bus.collab.cli
import llm_bus.coordination.cli
from llm_bus.cli.commands import human_command, owner_force_release, registered_command, send
from llm_bus.cli.hooks import claude_socket, hook
from llm_bus.cli.installation import configure_host_hooks
from llm_bus.cli.output import serialize
from llm_bus.cli.parser import build_parser
from llm_bus.collab.models import CollabError
from llm_bus.coordination import approvals
from llm_bus.coordination.setup import profile, run_activation_wizard, run_wizard
from llm_bus.errors import BusError
from llm_bus.hosts.processes import host_process
from llm_bus.hosts.sessions import require_main_agent
from llm_bus.mailbox.connection import default_path
from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.store import Store
from llm_bus.remote.bridge import run_bridge

if TYPE_CHECKING:
    from collections.abc import Sequence

CLAUDE_SESSION_ENV = "CLAUDE_CODE_SESSION_ID"


CODEX_THREAD_ENV = "CODEX_THREAD_ID"


CODEX_SESSION_ENV = "CODEX_SESSION_ID"


# Queued, not holding: `lock acquire NAME && use-resource` must not run the second command.
LOCK_WAITING_EXIT = 3


def host_identity(environment: dict[str, str]) -> tuple[str, str]:
    """Read native session identity from Claude or Codex shell tools."""
    claude_id = environment.get(CLAUDE_SESSION_ENV)
    codex_id = environment.get(CODEX_THREAD_ENV) or environment.get(CODEX_SESSION_ENV)
    if claude_id and codex_id:
        raise BusError("Both Claude and Codex session IDs are set; cannot choose sender")
    if claude_id:
        return "claude", claude_id
    if codex_id:
        return "codex", codex_id
    raise BusError("No host session ID found; run inside a Claude Code or Codex agent session")


def _optional_address() -> str | None:
    try:
        kind, session_id = host_identity(dict(os.environ))
    except BusError:
        return None
    agent_address = f"{kind}:{session_id}"
    require_main_agent(agent_address)
    return agent_address


def _owner_force(command: object, args: dict[str, object]) -> bool:
    """A forced release outside any agent session is the owner's terminal path."""
    return (
        command == "lock"
        and args.get("lock_command") == "release"
        and bool(args.get("force"))
        and _optional_address() is None
    )


def _sessionless(store: Store, command: object, args: dict[str, object]) -> object:
    if command == "collab":
        return llm_bus.collab.cli.run(args, _optional_address())
    if command == "lock":
        return owner_force_release(store, args)
    return _setup(store, args)


def _setup(store: Store, args: dict[str, object]) -> dict[str, object] | None:
    """Setup registers no session: terminal owners get the wizard, agents get the profile."""
    cwd = str(Path.cwd().resolve())
    if args.get("action") == "unattended":
        if not (sys.stdin.isatty() and sys.stdout.isatty()) or args["format"] == "agent":
            raise BusError("Owner terminal activation requires an interactive terminal")
        return run_activation_wizard(store, str(args["project"]), cwd, _optional_address())
    if sys.stdin.isatty() and sys.stdout.isatty() and args["format"] != "agent":
        return run_wizard(store, str(args["project"]), cwd, _optional_address())
    return profile(cwd)


def _unattended_setup(command: object, args: dict[str, object]) -> bool:
    return (
        command == "coordination" and args.get("action") == "unattended" and bool(args.get("setup"))
    )


def _require_application_command(command: object) -> None:
    if command in {"upgrade", "reinstall"}:
        raise BusError("Use the public llm-bus entrypoint for managed installation commands")


def main(argv: Sequence[str] | None = None) -> int:  # noqa: C901, PLR0912 - command dispatch
    """Run command, returning nonzero for input and storage errors."""
    # pylint: disable=too-many-branches
    args = cast("dict[str, object]", vars(build_parser().parse_args(argv)))
    command = args["command"]
    try:
        _require_application_command(command)
        if command in {"install", "uninstall"}:
            print(
                serialize(
                    configure_host_hooks(install=command == "install"),
                    str(command),
                    str(args["format"]),
                )
            )
            return 0
        store = (
            Store.read_only(default_path())
            if command in {"status", "retention-preview"}
            else Store(default_path())
        )
        if command in {"bridge", "hook"}:
            if command == "bridge":
                run_bridge(
                    store,
                    once=cast("bool", args["once"]),
                    refresh_status=cast("bool", args["refresh_status"]),
                )
            else:
                hook(store, cast("str", args["kind"]))
            return 0
        if human_command(store, command, args):
            return 0
        if (
            command == "collab"
            or (command == "coordinate" and args.get("setup"))
            or _unattended_setup(command, args)
            or _owner_force(command, args)
        ):
            result = _sessionless(store, command, args)
            if result is not None:
                print(serialize(result, str(command), str(args["format"])))
            return 0
        kind, session_id = host_identity(dict(os.environ))
        require_main_agent(f"{kind}:{session_id}")
        project = str(Path.cwd().resolve())
        if command in {"coordinate", "join", "coordination"}:
            if command in {"coordinate", "join"}:
                store.register(
                    kind,
                    session_id,
                    project,
                    messaging_socket=claude_socket(kind),
                    host=host_process(kind),
                )
            if command == "coordination" and args["action"] == "approve":
                result = approvals.approve(store, args, SenderContext(kind, session_id, project))
            else:
                result = llm_bus.coordination.cli.run(store, args, f"{kind}:{session_id}", project)
        elif command == "send":
            result = send(store, args, SenderContext(kind, session_id, project))
        else:
            agent_address = store.register(
                kind,
                session_id,
                project,
                messaging_socket=claude_socket(kind),
                label=cast("str | None", args.get("label")) if command == "whoami" else None,
                host=host_process(kind),
            )
            result = registered_command(store, command, args, agent_address, project)
        print(serialize(result, str(command), str(args["format"])))
        queued = (
            command == "lock"
            and isinstance(result, dict)
            and cast("dict[str, object]", result).get("status") == "waiting"
        )
    except (
        BusError,
        CollabError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"llm-bus: {error}", file=sys.stderr)
        return 1
    return LOCK_WAITING_EXIT if queued else 0
