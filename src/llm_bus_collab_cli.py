"""Agent-facing `llm-bus collab` commands. None of them returns peer message text."""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import urlencode

import llm_bus_collab_identity as identity
from llm_bus_collab_state import (
    CollabError,
    CollabState,
    NewMessage,
    effective_mode,
    github_user,
    running_daemon,
)
from llm_bus_store import routing_scope

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

OWNER_LINK_NOTE = (
    "Give this link to your owner to open in a browser. Only they choose permissions; "
    "do not open or submit it yourself."
)
SIGNING_SCOPE = "admin:ssh_signing_key"


def add_parser(add_command: Callable[..., argparse.ArgumentParser]) -> None:
    """Register `collab` using the top-level subparsers' add_parser method."""
    collab = add_command("collab", help="Ad hoc GitHub-verified peer collaboration")
    actions = collab.add_subparsers(dest="collab_command", required=True)
    setup = actions.add_parser("setup", help="Check or create your GitHub signing identity")
    setup.add_argument("--github-user")
    setup.add_argument("--key", help="Private key path, or a .pub path for an ssh-agent key")
    setup.add_argument("--generate", action="store_true", help="Create a new signing key")
    actions.add_parser("daemon", help="Run the collaboration daemon and owner page")
    actions.add_parser("invite", help="Print an owner link that creates an invite")
    join = actions.add_parser("join", help="Print an owner link to review and accept an invite")
    join.add_argument("invite", nargs="?", help="Invite text; read from stdin when omitted")
    send = actions.add_parser("send", help="Queue a message to a connected collaborator")
    send.add_argument("user")
    send.add_argument("--body")
    send.add_argument("--reply-to", help="UID of the message being answered")
    actions.add_parser("status", help="Show identity, daemon, and collaborators")


def _repository() -> str:
    git = shutil.which("git")
    if git is None:
        raise CollabError("git is required")
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise CollabError("Run collaboration commands inside a Git repository")
    return str(Path(result.stdout.strip()).resolve())


def _daemon_url(state: CollabState) -> str:
    details = running_daemon(state)
    if details is None or not isinstance(details.get("url"), str):
        raise CollabError("The collaboration daemon is not running; start `llm-bus collab daemon`")
    return cast("str", details["url"])


def _setup(state: CollabState, args: dict[str, object]) -> dict[str, object]:
    current = state.identity()
    user = cast("str | None", args["github_user"]) or (current[0] if current else None)
    key_arg = cast("str | None", args["key"])
    key = Path(key_arg).expanduser() if key_arg else (Path(current[1]) if current else None)
    if user is not None:
        github_user(user)
    if args["generate"]:
        if user is None:
            raise CollabError("Pass --github-user with --generate")
        key = identity.generate_key(key or Path.home() / ".ssh" / f"llm_bus_{user.lower()}", user)
    report: dict[str, object] = {"github_user": user, "signing_key": str(key) if key else None}
    steps: list[str] = []
    if user is None or key is None:
        steps.append(
            "Ask your owner which GitHub account to use, then run setup with "
            "--github-user USER and either --key PATH or --generate."
        )
        return {**report, "ready": False, "next_steps": steps}
    public = identity.public_key(key)
    state.set_identity(user, key)
    try:
        published = identity.is_published(user, key)
    except CollabError as exc:
        return {**report, "ready": False, "next_steps": [f"Could not check GitHub: {exc}"]}
    if not published:
        pub = key if key.suffix == ".pub" else key.with_name(key.name + ".pub")
        steps += [
            f"Ask your owner before publishing anything to GitHub account {user}.",
            f"Confirm gh is logged in as {user}: gh api user --jq .login",
            (
                f"If gh lacks the {SIGNING_SCOPE} scope, the owner runs: "
                f"gh auth refresh -h github.com -s {SIGNING_SCOPE}"
            ),
            f"Publish the signing key: gh ssh-key add {pub} --type signing --title llm-bus",
            "Run `llm-bus collab setup` again to confirm.",
        ]
    return {
        **report,
        "fingerprint": identity.fingerprint(public),
        "published": published,
        "ready": published,
        "next_steps": steps,
    }


def _send(state: CollabState, args: dict[str, object], session: str | None) -> dict[str, object]:
    if session is None:
        raise CollabError("Run collab send inside a Claude Code or Codex session")
    user = github_user(cast("str", args["user"]).lstrip("@"))
    scope = routing_scope(_repository())
    peers = [
        p
        for p in state.peers()
        if p["github_user"].lower() == user.lower()
        and effective_mode(p) is not None
        and routing_scope(p["repository"]) == scope
    ]
    if not peers:
        # pylint: disable-next=clean-code-business-policy-literal
        raise CollabError(f"No active collaboration with @{user} in this repository")
    peer = peers[-1]
    body = cast("str | None", args["body"])
    text = body if body is not None else sys.stdin.read()
    reply_to = cast("str | None", args["reply_to"])
    state.set_session(peer["id"], session)
    kind = "reply" if reply_to else "request"
    message = state.add_message(
        peer["id"],
        NewMessage(
            "out", kind, str(uuid.uuid4()), text, "queued", reply_to, origin_session=session
        ),
    )
    if message is None:
        raise RuntimeError("Outgoing message was not stored")
    return {
        "queued": message["id"],
        "to": f"@{peer['github_user']}",
        "their_grant": peer["their_grant"],
        "note": "Replies and other peer text go to your owner's page unless they chose full mode.",
    }


def _status(state: CollabState) -> dict[str, object]:
    found = state.identity()
    try:
        url: str | None = _daemon_url(state)
    except CollabError:
        url = None
    return {
        "github_user": found[0] if found else None,
        "daemon": "running" if url else "stopped",
        "owner_page": url,
        "peers": [
            {
                "github_user": p["github_user"],
                "repository": p["repository"],
                "their_messages_here": effective_mode(p) or "revoked",
                "they_allow_you": p["their_grant"],
                "confirm_code": p["confirm_code"],
            }
            for p in state.peers()
        ],
        "unread_messages": len(state.messages(state="unread")),
        "replies_awaiting_owner": len(state.messages(state="held")),
    }


def _run_daemon() -> None:
    """Load the daemon only on demand; it needs the optional iroh extra."""
    daemon = importlib.import_module("llm_bus_collab_daemon")
    cast("Callable[[], None]", daemon.run_daemon)()


def run(args: dict[str, object], session: str | None) -> object:
    """Dispatch one collab subcommand; `session` is this agent's bus address, if any."""
    command = args["collab_command"]
    state = CollabState()
    if command == "setup":
        return _setup(state, args)
    if command == "daemon":
        _run_daemon()
        return None
    if command == "invite":
        query = urlencode({"repo": _repository(), "session": session or ""})
        return {"open": f"{_daemon_url(state)}invite?{query}", "note": OWNER_LINK_NOTE}
    if command == "join":
        text = cast("str | None", args["invite"]) or sys.stdin.read()
        url = _daemon_url(state)
        join_id = state.add_join(text, _repository(), session)
        return {"open": f"{url}join/{join_id}", "note": OWNER_LINK_NOTE}
    if command == "send":
        return _send(state, args, session)
    return _status(state)
