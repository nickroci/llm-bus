"""Command grammar and help text."""

from __future__ import annotations

import argparse
from pathlib import Path

import llm_bus.collab.cli
import llm_bus.coordination.cli
from llm_bus.locks.models import FORCE_AUTHORITY
from llm_bus.reports.protocol import REPORT_REASONS


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915 - explicit CLI declarations
    # pylint: disable=too-many-statements
    parser = argparse.ArgumentParser(description="Local durable mailbox for Claude and Codex")
    parser.add_argument(
        "--format",
        choices=("json", "agent"),
        default="json",
        help="Output format (before command): expanded json, or concise agent output",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    llm_bus.coordination.cli.add_parsers(commands.add_parser)
    commands.add_parser("whoami", help="Show this session's bus address").add_argument(
        "--label", help="Claim a unique repository-scoped session label"
    )
    commands.add_parser("guidance", help="Recover full current identity, trust, and usage guidance")
    peers = commands.add_parser("list", help="List agents in this Git repository or folder")
    peers.add_argument(
        "--all", action="store_true", help="Include agents in other repositories or folders"
    )
    commands.add_parser("agents", help="Read all registered agents, including ended sessions")
    send = commands.add_parser("send", help="Store a message and request recipient wake")
    send.add_argument("to", help="Recipient address from `llm-bus list`")
    send.add_argument("--body", help="Message text; otherwise read standard input")
    send.add_argument("--reply-to", type=int, help="Retained local parent message ID")
    send.add_argument("--project", help="Coordination project; requires --direction")
    send.add_argument("--run-id", help="Bind direction to exact unattended run")
    send.add_argument("--ticket", help="Current claimed ticket for unattended direction")
    send.add_argument(
        "--direction", choices=("assign", "reprioritize", "clarify", "pause", "cancel", "review")
    )
    send.add_argument(
        "--cross-folder", action="store_true", help="Allow sending to another repository or folder"
    )
    inbox = commands.add_parser("inbox")
    inbox.add_argument("--limit", type=int, default=100)
    history = commands.add_parser("history", help="Read stored messages without changing inboxes")
    scope = history.add_mutually_exclusive_group()
    scope.add_argument(
        "--folder", help="Show messages involving this folder; default is current folder"
    )
    scope.add_argument(
        "--repository", help="Show messages involving this Git repository and its worktrees"
    )
    scope.add_argument("--all", action="store_true", help="Show messages from every folder")
    cursor = history.add_mutually_exclusive_group()
    cursor.add_argument("--before", type=int, help="Show older messages below this ID")
    cursor.add_argument("--after", type=int, help="Show newer messages above this ID")
    history.add_argument("--limit", type=int, default=100)
    history.add_argument("--conversation", type=int, help="Read the reply tree containing this ID")
    history.add_argument("--peer", help="Match sender or recipient address exactly")
    monitor = commands.add_parser("monitor", help="Read separate remote human monitoring history")
    monitor_scope = monitor.add_mutually_exclusive_group()
    monitor_scope.add_argument(
        "--repository", help="Repository path; defaults to current repository"
    )
    monitor_scope.add_argument("--all", action="store_true", help="Read every retained repository")
    monitor.add_argument("--before", type=int, help="Read rows below this monitor ID")
    monitor.add_argument("--limit", type=int, default=100)
    acknowledge = commands.add_parser("ack", help="Acknowledge handled messages")
    acknowledge.add_argument("ids", type=int, nargs="+")
    hook = commands.add_parser("hook", help="Register session and notify agent at turn boundary")
    hook.add_argument("kind", choices=("claude", "codex"))
    commands.add_parser("install", help="Activate user-level Codex and Claude hooks")
    commands.add_parser("uninstall", help="Remove hooks and uninstall the uv tool")
    commands.add_parser(
        "upgrade",
        help="Build this checkout's HEAD, reinstall the uv tool, then delete older packages",
    ).add_argument(
        "--skills-dir",
        type=Path,
        help="Also replace DIR/llm-bus with this checkout's skills/llm-bus from HEAD, "
        "for example ~/.agents/skills",
    )
    bridge = commands.add_parser(
        "bridge", help="Synchronize remote messages without a host session"
    )
    bridge.add_argument("--once", action="store_true", help="Synchronize once and exit")
    bridge.add_argument(
        "--refresh-status",
        action="store_true",
        help="Refresh status reads once, including suspended observations; requires --once",  # pylint: disable=clean-code-business-policy-literal
    )
    commands.add_parser("remote-status", help="Show the latest 100 outgoing remote delivery states")
    llm_bus.collab.cli.add_parser(commands.add_parser)
    status = commands.add_parser(
        "status", help="Read retained body-free diagnostics without identity or config"
    )
    status.add_argument("message_id", type=int, help="Retained local integer message ID")
    _lock_parser(
        commands.add_parser(
            "lock", help="Coordinate exclusive use of a named resource with other sessions"
        )
    )
    report = commands.add_parser(
        "report",
        help="Record a typed work report for a received coordination direction or imported message",
    )
    report.add_argument("message_id", type=int)
    report.add_argument("--status", required=True, choices=tuple(REPORT_REASONS))
    report.add_argument("--reason")
    return parser


def _lock_parser(lock: argparse.ArgumentParser) -> None:
    actions = lock.add_subparsers(dest="lock_command", required=True)
    acquire = actions.add_parser("acquire", help="Take, extend, or queue for a resource")
    acquire.add_argument("resource", help="Name, such as db:orders or file:src/schema.sql")
    acquire.add_argument(
        "--check",
        help="Your estimate of when you can release, such as 45m, 2h, or 1d; you are asked then",
    )
    acquire.add_argument("--reason", help="Why you need it; shown to the holder")
    acquire.add_argument(
        "--describe", help="What this name protects; required the first time a name is used"
    )
    acquire.add_argument(
        "--new", action="store_true", help="Confirm a name that resembles an existing one"
    )
    acquire.add_argument("--ttl", type=int, help="Lease minutes before it lapses (default 1440)")
    acquire.add_argument(
        "--notify",
        action="store_true",
        help="Message and wake the holder to check whether it still needs the resource",
    )
    release = actions.add_parser("release", help="Give it up; the first waiter takes it")
    release.add_argument("resource")
    release.add_argument(
        "--force",
        action="store_true",
        help=f"Take it from another holder; only {FORCE_AUTHORITY} may",
    )
    release.add_argument("--reason", help="With --force: why the holder loses it; recorded")
    cancel = actions.add_parser("cancel", help="Leave the queue for a resource you no longer need")
    cancel.add_argument("resource")
    catalog = actions.add_parser(
        "list", help="Show known lock names, what they protect, and who holds them"
    )
    catalog.add_argument("--all", action="store_true", help="Every repository and folder")
    status = actions.add_parser("status", help="Show holders, leases, and queues")
    status.add_argument("resource", nargs="?")
    status.add_argument("--all", action="store_true", help="Every repository and folder")
    for parser in (acquire, release, cancel, status):
        parser.add_argument(
            "--shared", action="store_true", help="Machine-wide name instead of this folder's"
        )
    timer = actions.add_parser("timer", help=argparse.SUPPRESS)
    timer.add_argument("scope")
    timer.add_argument("resource")
