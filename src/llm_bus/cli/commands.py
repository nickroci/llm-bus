"""Mailbox command execution and output."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

from llm_bus.cli.guidance import guidance
from llm_bus.cli.output import serialize
from llm_bus.cli.references import resolve_reference
from llm_bus.coordination import stalls, work_state
from llm_bus.coordination.decisions import run_decision_timer
from llm_bus.coordination.delivery import outgoing
from llm_bus.errors import BusError
from llm_bus.hosts.processes import under_agent_host
from llm_bus.locks.models import FORCE_AUTHORITY, Key, Request
from llm_bus.locks.service import Locks, force_release, lock_key
from llm_bus.locks.timer import detach, run_timer
from llm_bus.mailbox.models import address
from llm_bus.mailbox.preview import preview
from llm_bus.policy.config import load_config, optional_config, require_config
from llm_bus.remote.discovery import remote_peers
from llm_bus.remote.outgoing import enqueue_remote
from llm_bus.remote.store import RemoteStore
from llm_bus.reports.store import Reports
from llm_bus.routing import routing_scope
from llm_bus.wake.dispatch import dispatch_pending

if TYPE_CHECKING:
    from llm_bus.coordination.models import Approval
    from llm_bus.mailbox.models import SenderContext
    from llm_bus.mailbox.store import Store


def _lock(store: Store, args: dict[str, object], agent_address: str, project: str) -> object:
    locks = Locks(store, agent_address, project)
    action = args["lock_command"]
    resource = cast("str | None", args.get("resource"))
    shared = bool(args.get("shared"))
    if action == "list":
        return locks.catalog(every_scope=cast("bool", args["all"]))
    if action == "status":
        key = locks.key(resource, shared=shared) if resource is not None else None
        return locks.status(key, every_scope=cast("bool", args["all"]))
    if resource is None:
        raise BusError("Resource name is required")
    key = locks.key(resource, shared=shared)
    if action == "acquire":
        return locks.acquire(
            key,
            Request(
                check=cast("str | None", args["check"]),
                reason=cast("str | None", args["reason"]),
                ttl_minutes=cast("int | None", args["ttl"]),
                describe=cast("str | None", args["describe"]),
                new=cast("bool", args["new"]),
                notify=cast("bool", args["notify"]),
            ),
        )
    if action == "cancel":
        return locks.cancel(key)
    if args["force"]:
        return locks.force_release(key, cast("str | None", args["reason"]))
    if args["reason"] is not None:
        raise BusError("--reason applies only to a forced release")
    return locks.release(key)


def interactive_terminal() -> bool:
    """A TTY outside any agent host; an agent with a pseudo-terminal still runs under one."""
    return sys.stdin.isatty() and sys.stdout.isatty() and not under_agent_host()


def owner_force_release(store: Store, args: dict[str, object]) -> object:
    """Force from a terminal without a host session: only an interactive owner may."""
    if not interactive_terminal() or args["format"] == "agent":
        raise BusError(f"Only {FORCE_AUTHORITY} can run `llm-bus lock release --force`")
    key = lock_key(
        cast("str", args["resource"]), str(Path.cwd().resolve()), shared=bool(args["shared"])
    )
    return force_release(store, key, cast("str | None", args["reason"]), actor=None)


def _hidden_timer(store: Store, command: object, args: dict[str, object]) -> bool:
    """Run a detached hidden timer command; the parent returns at once, the child waits."""
    if command == "lock" and args["lock_command"] == "timer":
        if not detach():
            run_timer(store, Key(cast("str", args["scope"]), cast("str", args["resource"])))
        return True
    if command == "coordination" and args["action"] == "decision-timer":
        if not detach():
            run_decision_timer(store, cast("str", args["decision_id"]))
        return True
    if command == "coordination" and args["action"] == "stall-timer":
        if not detach():
            stalls.run_timer(
                store,
                str(args["scope"]),
                str(args["project"]),
                str(args["generation"]),
                str(args["token"]),
            )
        return True
    return False


def human_command(store: Store, command: object, args: dict[str, object]) -> bool:
    if _hidden_timer(store, command, args):
        return True
    if command in {"status", "retention-preview"}:
        print(
            serialize(
                preview(store.path)
                if command == "retention-preview"
                else store.message_status(cast("int", args["message_id"])),
                str(command),
                str(args.get("format", "json")),
            )
        )
        return True
    if command == "monitor":
        _monitor(store, args)
        return True
    if command in {"agents", "remote-status"}:
        if command == "agents":
            result: object = store.all_agents()
        else:
            config = require_config()
            result = Reports(store, config.connection).statuses()
        print(serialize(result, str(command), str(args.get("format", "json"))))
        return True
    if command == "history":
        selected_folder = cast("str | None", args["folder"])
        selected_repository = cast("str | None", args["repository"])
        folder = (
            None
            if args["all"] or selected_repository is not None
            else str(Path(selected_folder or Path.cwd()).resolve())
        )
        repository = (
            str(Path(selected_repository).resolve()) if selected_repository is not None else None
        )
        peer = cast("str | None", args["peer"])
        # Collaboration senders are retained history identities, not native targets.
        if peer is not None and not peer.startswith("collab:"):
            peer = resolve_reference(store, peer, repository or folder or str(Path.cwd()))
        print(
            serialize(
                store.history(
                    folder=folder,
                    repository=repository,
                    before=cast("int | None", args["before"]),
                    after=cast("int | None", args["after"]),
                    limit=cast("int", args["limit"]),
                    conversation=cast("int | None", args["conversation"]),
                    peer=peer,
                ),
                str(command),
                str(args.get("format", "json")),
            )
        )
        return True
    return False


def _monitor(store: Store, args: dict[str, object]) -> None:
    repository = (
        None
        if args["all"]
        else str(Path(cast("str | None", args["repository"]) or Path.cwd()).resolve())
    )
    rows = RemoteStore.monitor_history(
        store,
        repository=repository,
        before=cast("int | None", args["before"]),
        limit=cast("int", args["limit"]),
    )
    for key, scope, connection, item in rows:
        # JSON string escaping covers control characters and Unicode formatting controls.
        # Escape every remote field, including metadata; render no terminal links or markup.
        escaped = {name: json.dumps(value, ensure_ascii=True) for name, value in item.items()}
        print(f"#{key} {escaped['sent_at']}  {escaped['id']}")
        print(f"{escaped['sender']} to {escaped['recipient']}")
        print(f"repository: {escaped['repo_id']} scope: {json.dumps(scope, ensure_ascii=True)}")
        print(f"connection: {json.dumps(connection, ensure_ascii=True)}")
        print(escaped["body"])
        print()


def send(
    store: Store, args: dict[str, object], sender: SenderContext, approval: Approval | None = None
) -> object:
    args = {**args, "to": resolve_reference(store, str(args["to"]), sender.project)}
    label, operation = args.get("project"), args.get("direction")
    if (label is None) != (operation is None):
        raise BusError("Use --project and --direction together")
    if label is None and (args.get("run_id") is not None or args.get("ticket") is not None):
        raise BusError("Unattended bindings require --project and --direction")
    direction = None
    if label is not None:
        with store.transaction() as db:
            direction = outgoing(
                db,
                sender=address(sender.kind, sender.session_id),
                scope=routing_scope(sender.project),
                label=str(label),
                recipient=str(args["to"]),
                operation=str(operation),
                approval=approval,
                run_id=cast("str | None", args.get("run_id")),
                ticket=cast("str | None", args.get("ticket")),
            )
    configured_body = args["body"]
    body = cast("str", configured_body) if configured_body is not None else sys.stdin.read()
    if "/" in cast("str", args["to"]):
        config = load_config()
        if config is None:
            raise BusError("Remote sending requires a remote configuration file")
        return enqueue_remote(
            store,
            config,
            sender,
            cast("str", args["to"]),
            body,
            reply_to=cast("int | None", args["reply_to"]),
            direction=direction,
            approval=approval,
        )
    sent = store.send(
        sender,
        cast("str", args["to"]),
        body,
        cross_folder=cast("bool", args["cross_folder"]),
        reply_to=cast("int | None", args["reply_to"]),
        direction=direction,
        approval=approval,
    )
    try:
        wake_result = dispatch_pending(store, sent["recipient"], trigger_id=sent["id"])
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Delivery already committed; always report message ID.
        wake_result = {
            "status": "unknown",
            "via": "wake coordinator",
            "reason": f"wake recording failed: {type(exc).__name__}",
        }
        if args.get("format") != "agent":
            print(
                f"llm-bus: message #{sent['id']} stored; could not record wake result: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
    if wake_result["status"] != "requested" and args.get("format") != "agent":
        print(
            f"llm-bus: message #{sent['id']} stored; wake {wake_result['status']}: "
            f"{wake_result.get('reason', 'no details')}",
            file=sys.stderr,
        )
    return {**sent, "wake": wake_result}


def _report(store: Store, args: dict[str, object], agent_address: str) -> object:
    message_id = cast("int", args["message_id"])
    result: dict[str, object] | None = None
    with store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        imported = work_state.imported(db, message_id)
        if not imported:
            result = work_state.record(
                db, message_id, agent_address, args["status"], args["reason"]
            )
    if result is not None:
        stalls.schedule_messages(store, [message_id])
        return result
    config = require_config()
    return Reports(store, config.connection).enqueue(
        message_id, agent_address, args["status"], args["reason"]
    )


def registered_command(
    store: Store, command: object, args: dict[str, object], agent_address: str, project: str
) -> object:
    if command == "whoami":
        current = next(peer for peer in store.agents(project) if peer["address"] == agent_address)
        result: object = {"address": agent_address, "label": current.get("label")}
    elif command == "guidance":
        result = {"guidance": guidance(store, agent_address, project, full=True)}
    elif command == "list":
        selected = None if args["all"] else project
        peers: list[object] = list(store.agents(selected))
        config = optional_config()
        if config is not None:
            peers.extend(remote_peers(store, config, selected))
        result = peers
    elif command == "inbox":
        result = store.inbox(agent_address, cast("int", args["limit"]))
    elif command == "report":
        result = _report(store, args, agent_address)
    elif command == "ack":
        result = {"acknowledged": store.acknowledge(agent_address, cast("list[int]", args["ids"]))}
    elif command == "lock":
        result = _lock(store, args, agent_address, project)
    else:
        raise RuntimeError("Unreachable command")
    return result
