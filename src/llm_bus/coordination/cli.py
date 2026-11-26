"""Explicit owner setup and exact native session enrollment."""

# Keep related public coordination command parsing and transactional dispatch together.
# pylint: disable=too-many-lines

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast
from uuid import uuid4

from llm_bus.cli.guidance import Delivery, guidance
from llm_bus.cli.references import resolve_reference
from llm_bus.coordination import (
    amendments,
    decisions,
    delegation,
    dev_secrets,
    gate_records,
    github_merge,
    recovery,
    runs,
    stalls,
    store,
    work_state,
)
from llm_bus.coordination import instructions as instructions_store
from llm_bus.coordination.authority import project_execution_allowed
from llm_bus.coordination.availability import record_local, record_remote
from llm_bus.coordination.models import (
    OPERATIONS,
    branch_name,
    ticket_text,
    timestamp,
    uuid4_string,
)
from llm_bus.errors import BusError
from llm_bus.hosts.kinds import KIND_PATTERN
from llm_bus.locks import notices
from llm_bus.routing import routing_scope
from llm_bus.wake.dispatch import dispatch_pending

if TYPE_CHECKING:
    from collections.abc import Callable
    from sqlite3 import Connection

    from llm_bus.mailbox.models import Message
    from llm_bus.mailbox.store import Store

    ActionResult = tuple[dict[str, object] | None, list[Message]]

NARROW_ITEM = "ITEM"
OPEN_HELP = (
    "Only acknowledged assign, review and reprioritize directions without a completed "
    "or failed report or a later acknowledged cancel"
)
MINUTES_ITEM = "MINUTES"
MAX_TEXT = 4096
MAX_REFERENCES = 32
CONTROL_BOUNDARY = 32
ROUTINE_HELP = "Resolve routine scope under an active run"
RUN_RETRY = "run-retry"
RUN_RETRY_HELP = "Count a read-only retry for the current ticket claim"


def add_parsers(  # pylint: disable=too-many-statements
    add_parser: Callable[..., argparse.ArgumentParser],
) -> None:
    coordinate = add_parser("coordinate", help="Owner enrolls this coordinator")
    coordinate.add_argument("project")
    _references(coordinate, required=False)
    coordinate.add_argument("--delegate", action="append", metavar="GATE:BRANCH")
    coordinate.add_argument(
        "--auto-merge",
        action="store_true",
        help="Delegate auto-merge: a worker merges on a coordinator-accepted gate record",
    )
    coordinate.add_argument(
        "--owner-decisions",
        action="store_true",
        help="Delegate owner decisions: workers accept coordinator-recorded owner answers",
    )
    _optional_delegation_flags(coordinate)
    coordinate.add_argument("--unattended", action="store_true")
    _run_scope_flags(coordinate)
    coordinate.add_argument(
        "--setup",
        action="store_true",
        help="Print the setup profile, or run the confirmation wizard in a terminal",
    )
    join = add_parser("join", help="Owner consents and enrolls this main session")
    join.add_argument("project")
    join.add_argument("--narrow", action="append", metavar=NARROW_ITEM)
    actions = add_parser("coordination").add_subparsers(dest="action", required=True)
    _amend_parser(actions.add_parser)
    _gate_parsers(actions.add_parser)
    _decision_parsers(actions.add_parser)
    _instructions_parser(actions.add_parser)
    _stall_parsers(actions.add_parser)
    _unattended_parsers(actions.add_parser)
    _secret_parsers(actions.add_parser)
    _recovery_parsers(actions.add_parser)
    approve = actions.add_parser("approve")
    approve.add_argument("project")
    approve.add_argument("--recipient", required=True)
    approve.add_argument("--gate", required=True, choices=delegation.GATES)
    approve.add_argument("--ticket", required=True)
    approve.add_argument("--branch", required=True)
    approve.add_argument("--base", required=True)
    approve.add_argument("--pr", type=int)
    approve.add_argument("--head-sha")
    approve.add_argument("--expires-in", default="2h")
    approve.add_argument("--run-id")
    claim = actions.add_parser("claim")
    claim.add_argument("project")
    claim.add_argument("--ticket", required=True)
    claim.add_argument("--branch", required=True)
    _lifecycle_parsers(actions.add_parser)


def _recovery_parsers(add_action: Callable[..., argparse.ArgumentParser]) -> None:
    result = add_action("run-result", help="Verify and retain exact-head reviewed green PR proof")
    result.add_argument("project")
    result.add_argument("--run-id", required=True)
    result.add_argument("--record-id", required=True)
    result.add_argument("--repository", required=True)
    reconcile = add_action("run-merge-reconcile", help="Confirm a prior merge without replay")
    reconcile.add_argument("project")
    reconcile.add_argument("--message-id", required=True, type=int)
    retry = add_action(RUN_RETRY, help=RUN_RETRY_HELP)
    retry.add_argument("project")
    retry.add_argument("--run-id", required=True)
    retry.add_argument("--ticket", required=True)
    retry.add_argument("--worker", required=True)
    retry.add_argument("--read-only", action="store_true")
    for name in ("handoff-ready", "run-handoff"):
        action = add_action(name, help="Reconcile an unattended ticket handoff")
        action.add_argument("project")
        action.add_argument("--worktree", required=True)
        action.add_argument("--pr", type=int)
        action.add_argument("--repository")
        if name == "run-handoff":
            action.add_argument("--handoff-id", required=True)
            action.add_argument("--worker", required=True)
        else:
            action.add_argument("--run-id", required=True)
            action.add_argument("--ticket", required=True)
            action.add_argument("--no-pending-effects", action="store_true")  # pylint: disable=clean-code-business-policy-literal


def _secret_parsers(add_action: Callable[..., argparse.ArgumentParser]) -> None:
    for name in ("dev-secret-reserve", "dev-secret-check", "dev-secret-outcome"):
        action = add_action(name, help="Exact owner-signed development secret consent")
        action.add_argument("project")
        if name == "dev-secret-reserve":
            action.add_argument("--receipt-file", required=True)
            action.add_argument("--signature-file", required=True)
        else:
            action.add_argument("--receipt-id", required=True)
        if name == "dev-secret-outcome":
            action.add_argument("--status", choices=sorted(dev_secrets.STATUSES), required=True)
        else:
            action.add_argument("--plan-file", required=True)


def _unattended_parsers(add_action: Callable[..., argparse.ArgumentParser]) -> None:
    unattended = add_action("unattended", help="Preview or explicitly activate a local run")
    unattended.add_argument("project")
    unattended.add_argument("--setup", action="store_true", help="Interactive owner terminal setup")
    unattended.add_argument("--objective")
    _run_scope_flags(unattended)
    for name in ("unattended-revoke", "unattended-complete"):
        end = add_action(name)
        end.add_argument("project")
        end.add_argument("--run-id", required=True)


def _lifecycle_parsers(add_action: Callable[..., argparse.ArgumentParser]) -> None:
    for name in ("show", "setup", "grant", "revoke", "replace", "leave", "target"):
        action = add_action(name)
        action.add_argument("project")
        if name == "setup":
            action.add_argument("--project-id", required=True)
            action.add_argument("--generation", required=True)
            action.add_argument("--coordinator", required=True)
            action.add_argument(
                "--delegate",
                action="append",
                metavar="GATE:BRANCH",
                help="Receiver owner's own gate delegation for this remote coordinator",
            )
            _references(action, required=True)
        if name == "replace":
            action.add_argument("--generation", required=True)
            action.add_argument("--coordinator", required=True)
        if name in {"grant", "revoke", "target"}:
            action.add_argument("--recipient", required=name != "revoke")
        if name == "target":
            action.add_argument("--grant-id", required=True)
        if name == "grant":
            lifetime = action.add_mutually_exclusive_group(required=True)
            lifetime.add_argument("--expires-at")
            lifetime.add_argument("--until-revoked", action="store_true")


def _run_scope_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ticket", action="append", help="One explicit ticket in the finite run scope"
    )
    parser.add_argument("--completion", help="Owner-confirmed completion condition")
    parser.add_argument(
        "--completion-proof", choices=("reviewed-pr", "merged-pr"), default="reviewed-pr"
    )
    parser.add_argument("--expires-at", help="Finite run deadline (ISO timestamp)")
    parser.add_argument("--permission", action="append", help="One existing delegation item")
    parser.add_argument(
        "--confirm", action="store_true", help="Owner confirms the reviewed run scope"
    )


def _optional_delegation_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-execution", action="store_true")
    parser.add_argument(
        "--decision-defaults",
        dest="decision_default_minutes",
        type=int,
        metavar=MINUTES_ITEM,
        help=(
            "Default an unanswered scope decision with a recommended option after MINUTES "
            "(1-1440); needs --owner-decisions"
        ),
    )


def _amend_parser(add_action: Callable[..., argparse.ArgumentParser]) -> None:
    amend = add_action("amend")
    amend.add_argument("project")
    change = amend.add_mutually_exclusive_group()
    change.add_argument("--delegate", action="append", metavar="GATE:BRANCH")
    change.add_argument("--no-delegation", action="store_true")
    amend.add_argument("--auto-merge", action="store_true")
    amend.add_argument("--owner-decisions", action="store_true")
    _optional_delegation_flags(amend)
    amend.add_argument("--objective", help="Append a dated objective amendment")


def _decision_parsers(add_action: Callable[..., argparse.ArgumentParser]) -> None:
    ask = add_action("decision-ask", help="Record an owner decision question")
    ask.add_argument("project")
    ask.add_argument("--class", dest="decision_class", required=True, choices=decisions.CLASSES)
    ask.add_argument("--question", required=True)
    for name in ("recommended", "ticket", "worker", "answer"):
        ask.add_argument(f"--{name}")
    reply = add_action("decision-answer", help="Record the owner's answer to a decision")
    reply.add_argument("project")
    reply.add_argument("--decision-id", required=True)
    reply.add_argument("--answer", required=True)
    routine = add_action("decision-resolve", help=ROUTINE_HELP)
    routine.add_argument("project")
    for name in ("decision-id", "answer", "run-id", "rationale"):
        routine.add_argument(f"--{name}", required=True)
    show = add_action("decision-show", help="Show a decision and whether you may act on it")
    show.add_argument("project")
    show.add_argument("--decision-id", required=True)
    listed = add_action("decision-list", help="List the project's decisions")
    listed.add_argument("project")
    listed.add_argument("--defaulted", action="store_true", help="Only decisions taken by default")
    directions = add_action(
        "direction-list",
        help="List your directions with work state; --open keeps unfinished work",
    )
    directions.add_argument("project")
    directions.add_argument("--open", action="store_true", help=OPEN_HELP)
    timer = add_action("decision-timer", help=argparse.SUPPRESS)
    timer.add_argument("project")
    timer.add_argument("--decision-id", required=True)


def _instructions_parser(add_action: Callable[..., argparse.ArgumentParser]) -> None:
    instructions = add_action("instructions", help="Set owner standing instructions")
    instructions.add_argument("project")
    instructions.add_argument("--audience", required=True, choices=instructions_store.AUDIENCES)
    source = instructions.add_mutually_exclusive_group(required=True)
    source.add_argument("--file")
    source.add_argument("--text")
    instructions.add_argument("--amend", action="store_true")


def _gate_parsers(add_action: Callable[..., argparse.ArgumentParser]) -> None:
    record = add_action("gate-record")
    record.add_argument("project")
    for name in ("ticket", "branch", "base", "head-sha"):
        record.add_argument(f"--{name}", required=True)
    record.add_argument("--pr", type=int, required=True)
    record.add_argument("--review-evidence", action="append", default=[])
    record.add_argument("--required-check", action="append", default=[])
    record.add_argument("--run-id", help="Bind an observational PR record without granting merge")
    for name in ("gate-accept", "gate-reject"):
        decide = add_action(name)
        decide.add_argument("project")
        decide.add_argument("--record-id", required=True)
        if name == "gate-accept":
            decide.add_argument("--head-sha", required=True)
            decide.add_argument("--admin", action="store_true")
            decide.add_argument("--run-id")
            decide.add_argument("--repository", help="Exact GitHub OWNER/REPO for unattended merge")
    merge = add_action("gate-merge", help="Verify and merge a run-bound GitHub approval")
    merge.add_argument("project")
    merge.add_argument("--message-id", type=int, required=True)


def _references(parser: argparse.ArgumentParser, *, required: bool) -> None:
    for name in ("objective", "beads-workspace", "memory-ticket"):
        parser.add_argument(f"--{name}", required=required)
    parser.add_argument("--reference", action="append", default=[])


def text_reference(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        raise BusError("Expected nonempty bounded reference (at most 4096 characters)")
    if any(ord(char) < CONTROL_BOUNDARY for char in value):
        raise BusError("Control characters forbidden in references")
    return value


def _address(value: object) -> str:
    address = text_reference(value)
    if re.fullmatch(rf"(?:[^/:\s]+/[^/:\s]+/)?(?:{KIND_PATTERN}):[^/:\s]+", address) is None:
        raise BusError("Expected exact native or machine/repository qualified address")
    return address


def _path(value: object) -> str:
    path = Path(text_reference(value))
    if not path.is_absolute() or not path.exists():
        raise BusError("Expected existing absolute local path")
    return str(path.resolve())


def beads_workspace(value: object) -> str:
    workspace = _path(value)
    if not Path(workspace).is_dir() or not (Path(workspace) / ".beads").exists():
        raise BusError("Coordination requires an existing Beads workspace")
    return workspace


def _stall_parsers(add_action: Callable[..., argparse.ArgumentParser]) -> None:
    for name in ("stalls", "stall-config"):
        action = add_action(name)
        action.add_argument("project")
        action.add_argument("--threshold", required=name == "stall-config")
    timer = add_action("stall-timer", help=argparse.SUPPRESS)
    timer.add_argument("project")
    for option in ("scope", "generation", "token"):
        timer.add_argument(f"--{option}", required=True)


def _inspect_stalls(
    mailbox: Store, scope: str, label: str, address: str, args: dict[str, object]
) -> dict[str, object]:
    seconds = stalls.parse_threshold(str(args["threshold"])) if args.get("threshold") else None
    with mailbox.transaction() as db:
        return stalls.inspect(
            db,
            store.project(db, scope, label),
            address,
            now=datetime.now(UTC),
            threshold_seconds=seconds,
        )


def _stall_config(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str
) -> ActionResult:
    return stalls.configure(db, record, address, stalls.parse_threshold(str(args["threshold"]))), []


def run(  # noqa: C901, PLR0912, PLR0915 - owner action dispatch
    mailbox: Store, args: dict[str, object], address: str, cwd: str
) -> dict[str, object]:
    """Use app-selected native identity. These commands grant no remote owner power."""
    # pylint: disable=too-many-branches,too-many-locals,too-many-statements
    scope = routing_scope(cwd)
    args = dict(args)
    for field in ("coordinator", "recipient", "worker"):
        if args.get(field) is not None:
            args[field] = resolve_reference(mailbox, str(args[field]), cwd)
    label = text_reference(args["project"])
    action = str(args["command"] if args["command"] != "coordination" else args["action"])
    if action == "stalls":
        return _inspect_stalls(mailbox, scope, label, address, args)
    if action == "gate-merge":
        return github_merge.execute(
            mailbox, scope, label, address, int(str(args["message_id"])), cwd=cwd
        )
    if action == "run-merge-reconcile":
        return github_merge.reconcile(
            mailbox, scope, label, address, int(str(args["message_id"])), cwd=cwd
        )
    if action == "run-result":
        return github_merge.record_result(mailbox, scope, label, address, args, cwd=cwd)
    completion_evidence: github_merge.CompletionEvidence = {}
    if action == "unattended-complete":
        completion_evidence = github_merge.verify_completion(
            mailbox, scope, label, address, str(args["run_id"]), cwd=cwd
        )
    if action in {"handoff-ready", "run-handoff"}:
        args["observation"] = recovery.observe(
            str(args["worktree"]), pr=args.get("pr"), repository=args.get("repository")
        )
    with mailbox.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        new_project = False
        if action == "setup":
            _setup(db, scope, label, args, address)
        elif action == "coordinate":
            exists = cast(
                "tuple[int] | None",
                db.execute(
                    "SELECT 1 FROM coordination_projects WHERE scope=? AND label=?", (scope, label)
                ).fetchone(),
            )
            new_project = not exists
            if new_project:
                _create(db, scope, label, args, address)
        record = store.project(db, scope, label)
        if action == "unattended-complete":
            github_merge.validate_completion(
                db, record, address, str(args["run_id"]), completion_evidence
            )
        lazy = decisions.apply_due(db, record) if action in LAZY_DEFAULTS else []
        if action in {"coordinate", "join", "grant"} and record["revoked_at"] is not None:
            raise BusError("Project revoked; owner must replace coordinator binding")
        if action == "coordinate":
            _check_resume(db, record, args, address, new=new_project)
        remote_notice = None
        outcome: ActionResult | None = None
        if action in ACTIONS:
            outcome = ACTIONS[action](db, record, args, address)
            notice_id = None
        elif action == "join" and "/" in str(record["coordinator"]):
            grant_id = store.join_session(db, record, address)
            delegation.bind(db, record, grant_id, cast("list[str] | None", args.get("narrow")))
            remote_notice = record_remote(db, mailbox, record, address, grant_id, cwd=cwd)
            notice_id = None
        else:
            notice_id = _transition(db, action, record, args, address)
        runs.reconcile(db, record, now=datetime.now(UTC))
        result = None if outcome is None else outcome[0]
        messages = lazy + ([] if outcome is None else outcome[1])
        if result is None:
            result = store.show(db, store.project(db, scope, label), address)
            if action == "amend":
                result["rejoin_required"] = delegation.rejoin_required(db, record)
        if action in {"coordinate", "unattended"}:
            result["unattended"] = runs.status(db, record, address)
        if remote_notice is not None:
            result["availability"] = remote_notice
    _schedule_default(action, label, result)
    if action == "coordinate":
        stalls.apply_for_coordinator(mailbox, address)
    elif action == "stall-config":
        stalls.schedule(mailbox, record)
    if messages:
        result["wakes"] = notices.deliver(mailbox, messages)
    if notice_id is not None:
        result["availability_wake"] = dispatch_pending(
            mailbox, str(record["coordinator"]), trigger_id=notice_id
        )
    if _teaches_role(action, args, address):
        # Reuse hook guidance text; command success must teach the current role rules.
        result["guidance"] = guidance(mailbox, address, cwd, full=True, delivery=Delivery.RESULT)
    return result


def _teaches_role(action: str, args: dict[str, object], address: str) -> bool:
    """Whether the caller's role or capabilities just changed: it is the new coordinator."""
    if action == "replace":
        return _address(args["coordinator"]) == address
    return action in {"coordinate", "join", "amend"}


def _schedule_default(action: str, label: str, result: dict[str, object]) -> None:
    """After commit, start the default timer for a new decision that has a deadline."""
    if action == "decision-ask" and result.get("deadline_at") is not None:
        decisions.schedule_default(label, str(result["decision_id"]))


def new_project_label(mailbox: Store, cwd: str, label: object) -> str:
    """Validate a label exactly like coordinate and require that the project is new."""
    label = text_reference(label)
    with mailbox.transaction() as db:
        exists = cast(
            "tuple[int] | None",
            db.execute(
                "SELECT 1 FROM coordination_projects WHERE scope=? AND label=?",
                (routing_scope(cwd), label),
            ).fetchone(),
        )
    if exists:
        raise BusError(
            "Project already exists; run `llm-bus coordinate PROJECT` to resume "
            "or use coordination amend"
        )
    return label


def create_for(  # noqa: PLR0913 - explicit setup inputs
    mailbox: Store,
    cwd: str,
    label: str,
    args: dict[str, object],
    coordinator: str,
    *,
    current: str | None = None,
    owner_terminal: bool = False,
) -> dict[str, object]:
    """Create a project and its delegation in one transaction without registering a session.

    The coordinator is enrolled only when it is the caller's own identity ``current``;
    otherwise that session must run ``coordinate PROJECT`` itself.
    """
    # pylint: disable=too-many-arguments
    scope = routing_scope(cwd)
    label = new_project_label(mailbox, cwd, label)
    coordinator = _address(coordinator)
    with mailbox.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        _create(db, scope, label, args, coordinator)
        record = store.project(db, scope, label)
        specs = cast("list[str]", args.get("delegate") or [])
        flags = _flags(args)
        if specs or _flagged(flags):
            delegation.set_project_delegation(
                db, record, delegation.Delegation.from_specs(specs, **flags), coordinator
            )
        enrolled = current is not None and current == coordinator
        if enrolled:
            store.enroll(db, record, coordinator, "coordinator")
        if args.get("unattended"):
            runs.activate(db, record, args, current, terminal=owner_terminal)
        result = store.show(db, record)
        if args.get("unattended"):
            result["unattended"] = (
                runs.status(db, record, coordinator) if enrolled else {runs.ACTIVE: False}
            )
    result["enrolled"] = enrolled
    if not enrolled:
        result["next"] = f"Session {coordinator} must run `llm-bus coordinate {label}`"
    return result


class Flags(TypedDict):
    """Delegation flags, spelled as ``Delegation.from_specs`` keywords."""

    auto_merge: bool
    owner_decisions: bool
    project_execution: bool
    decision_default_minutes: int | None


def _flags(args: dict[str, object]) -> Flags:
    """Delegation flags given on the command line or by the setup wizard."""
    minutes = args.get("decision_default_minutes")
    return {
        "auto_merge": bool(args.get("auto_merge")),
        "owner_decisions": bool(args.get("owner_decisions")),
        "project_execution": bool(args.get("project_execution")),
        "decision_default_minutes": None if minutes is None else cast("int", minutes),
    }


def _flagged(flags: Flags) -> bool:
    return (
        flags["auto_merge"]
        or flags["owner_decisions"]
        or flags["project_execution"]
        or flags["decision_default_minutes"] is not None
    )


def _gate_record(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str
) -> ActionResult:
    return gate_records.submit(
        db,
        record,
        address,
        ticket=str(args["ticket"]),
        branch=str(args["branch"]),
        base=str(args["base"]),
        pr=cast("int", args["pr"]),
        head_sha=str(args["head_sha"]),
        evidence=cast("list[str]", args["review_evidence"]),
        checks=cast("list[str]", args["required_check"]),
        run_id=_optional(args, "run_id"),
        now=datetime.now(UTC),
    )


def _gate_accept(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str
) -> ActionResult:
    return gate_records.accept(
        db,
        record,
        address,
        record_id=str(args["record_id"]),
        head_sha=str(args["head_sha"]),
        admin=bool(args["admin"]),
        now=datetime.now(UTC),
        run_id=_optional(args, "run_id"),
        repository=_optional(args, "repository"),
    )


def _gate_reject(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str
) -> ActionResult:
    return gate_records.reject(
        db, record, address, record_id=str(args["record_id"]), now=datetime.now(UTC)
    )


def _optional(args: dict[str, object], name: str) -> str | None:
    value = args.get(name)
    return None if value is None else str(value)


def _decision_ask(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str
) -> ActionResult:
    return decisions.ask(
        db,
        record,
        address,
        decision_class=str(args["decision_class"]),
        question=str(args["question"]),
        recommended=_optional(args, "recommended"),
        ticket=_optional(args, "ticket"),
        worker=_optional(args, "worker"),
        answer=_optional(args, "answer"),
        now=datetime.now(UTC),
    )


def _decision_answer(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str
) -> ActionResult:
    return decisions.answer(
        db, record, address, str(args["decision_id"]), str(args["answer"]), datetime.now(UTC)
    )


def _decision_show(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str
) -> ActionResult:
    return (
        decisions.status(db, record, address, str(args["decision_id"]), now=datetime.now(UTC)),
        [],
    )


def _decision_resolve(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str
) -> ActionResult:
    return decisions.coordinator_answer(
        db,
        record,
        address,
        str(args["decision_id"]),
        str(args["answer"]),
        run_id=str(args["run_id"]),
        rationale=str(args["rationale"]),
        now=datetime.now(UTC),
    )


def _decision_list(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str
) -> ActionResult:
    return decisions.listing(db, record, address, defaulted=bool(args.get("defaulted"))), []


def _show(
    db: Connection, record: dict[str, object], _args: dict[str, object], address: str
) -> ActionResult:
    result = store.show(db, record, address)
    result["unattended"] = runs.status(db, record, address)
    result["project_execution_allowed"] = project_execution_allowed(
        db, record, address, scope=str(record["scope"]), now=datetime.now(UTC)
    )
    items = work_state.listing(
        db,
        record,
        address,
        coordinator=store.coordinator_enrolled(db, record, address),
        only="attention",
    )
    if items:
        result["directions"] = items
    return result, []


def _direction_list(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str
) -> ActionResult:
    items = work_state.listing(
        db,
        record,
        address,
        coordinator=store.coordinator_enrolled(db, record, address),
        only="open" if args.get("open") else "all",
    )
    return {"directions": items}, []


def _instruction_text(args: dict[str, object]) -> str:
    if args.get("file") is None:
        return str(args["text"])
    try:
        with Path(text_reference(args["file"])).open(encoding="utf-8") as handle:
            return handle.read(instructions_store.MAX_TEXT + 1)
    except (OSError, UnicodeDecodeError) as error:
        raise BusError("Cannot read instructions file as UTF-8 text") from error


def _instructions(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str
) -> ActionResult:
    _require_coordinator(db, record, address, "Only the enrolled coordinator may set instructions")
    instructions_store.add(
        db,
        record,
        audience=str(args["audience"]),
        kind="amend" if args.get("amend") else "set",
        text=_instruction_text(args),
        by=address,
    )
    return None, []


def _unattended(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str
) -> ActionResult:
    if address != record["coordinator"] or not store.coordinator_enrolled(db, record, address):
        raise BusError("Only the enrolled coordinator may configure unattended authority")
    runs.require_owner_origin(db, address)
    if not args.get("confirm"):
        preview = runs.scope_selection(db, record, args, now=datetime.now(UTC))
        return {"preview": preview, "stored": False}, []
    return runs.activate(db, record, args, address), []


def _unattended_end(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str
) -> ActionResult:
    reason = "completed" if args["action"] == "unattended-complete" else "revoked"
    if reason == "completed":
        recovery.complete(db, record, address, str(args["run_id"]))
    return runs.finish(db, record, address, str(args["run_id"]), reason=reason), []


def _secret_action(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str
) -> ActionResult:
    try:
        if args["action"] == "dev-secret-outcome":
            return dev_secrets.outcome(
                db, record, address, str(args["receipt_id"]), str(args["status"])
            ), []
        plan = Path(str(args["plan_file"])).read_bytes()
        if len(plan) > dev_secrets.MAX_PLAN_BYTES:
            raise BusError(dev_secrets.DENIED)
        if args["action"] == "dev-secret-check":
            return dev_secrets.check(db, record, address, str(args["receipt_id"]), plan), []
        payload = dev_secrets.parse(
            cast("object", json.loads(Path(str(args["receipt_file"])).read_bytes()))
        )
        signature = Path(str(args["signature_file"])).read_text(encoding="ascii")
        return dev_secrets.reserve(db, record, address, payload, plan, signature), []
    except (OSError, ValueError, UnicodeError) as exc:
        raise BusError(dev_secrets.DENIED) from exc


def _recovery_action(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str
) -> ActionResult:
    if args["action"] == RUN_RETRY:
        return recovery.retry(db, record, address, args), []
    handlers = {
        "handoff-ready": recovery.cancellation,
        "run-handoff": recovery.handoff,
    }
    return handlers[str(args["action"])](
        db, record, address, args, cast("dict[str, object]", args["observation"])
    ), []


ACTIONS: dict[str, Callable[..., ActionResult]] = {
    RUN_RETRY: _recovery_action,
    "handoff-ready": _recovery_action,
    "run-handoff": _recovery_action,
    "dev-secret-reserve": _secret_action,
    "dev-secret-check": _secret_action,
    "dev-secret-outcome": _secret_action,
    "unattended": _unattended,
    "unattended-revoke": _unattended_end,
    "unattended-complete": _unattended_end,
    "stall-config": _stall_config,
    "show": _show,
    "instructions": _instructions,
    "gate-record": _gate_record,
    "gate-accept": _gate_accept,
    "gate-reject": _gate_reject,
    "decision-ask": _decision_ask,
    "decision-answer": _decision_answer,
    "decision-resolve": _decision_resolve,
    "decision-show": _decision_show,
    "decision-list": _decision_list,
    "direction-list": _direction_list,
}
# Actions that run inside ``run`` and apply due decision defaults first (SC4). ``approve`` and
# the join/coordinate paths are not lazy callers.
LAZY_DEFAULTS = frozenset(
    {
        "show",
        "amend",
        "gate-record",
        "gate-accept",
        "gate-reject",
        "decision-show",
        "decision-list",
    }
)


def _check_resume(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str, *, new: bool
) -> None:
    """A resume may repeat the current objective and delegation, never change them silently."""
    given = args.get("objective")
    if not new and given is not None and given != amendments.effective_objective(db, record):
        raise BusError(
            "Objective differs from the stored objective; "
            "use coordination amend PROJECT --objective"
        )
    if args.get("delegate") or _flagged(_flags(args)):
        _declare_delegation(db, record, args, address, new=new)


def _declare_delegation(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str, *, new: bool
) -> None:
    """Set delegation on a new project; a resume may only repeat it, never change it."""
    wanted = delegation.Delegation.from_specs(
        cast("list[str]", args.get("delegate") or []), **_flags(args)
    )
    if new:
        delegation.set_project_delegation(db, record, wanted, address)
    elif wanted != delegation.project_delegation(db, record):
        raise BusError("Delegation differs; use coordination amend PROJECT")


COORDINATOR_ONLY = {"grant": "grant", "revoke": "revoke", "target": "set direction targets"}
WORKER_REFUSED = {
    **COORDINATOR_ONLY,
    "replace": "replace the coordinator",
    "setup": "set up the project",
    "amend": "amend its delegation",
}


def _owner_gate(db: Connection, action: str, record: dict[str, object], address: str) -> None:
    """Refuse owner actions to sessions without the role for them.

    On a local project only the coordinator may grant, revoke or target; local ``amend``
    checks the same rule itself. ``replace``, and on a remote-coordinated project every
    owner action (no local session is its coordinator), is refused to any session that is
    or was a worker of the current generation. A never-enrolled session still passes, and
    so does a worker of an older generation after ``replace``.
    """
    remote = "/" in str(record["coordinator"])
    if action in COORDINATOR_ONLY and not remote:
        if not _owner_coordinator(db, record, address):
            raise BusError(f"Only the project's coordinator may {COORDINATOR_ONLY[action]}")
    elif action == "replace" or (remote and action in WORKER_REFUSED):
        _refuse_worker(db, action, record, address)


def _refuse_worker(db: Connection, action: str, record: dict[str, object], address: str) -> None:
    if not store.coordinator_enrolled(db, record, address) and store.worker_of_generation(
        db, record, address
    ):
        raise BusError(f"A worker of this project cannot {WORKER_REFUSED[action]}")


def _owner_coordinator(db: Connection, record: dict[str, object], address: str) -> bool:
    """The enrolled coordinator, or the named coordinator before it enrolls if never a worker."""
    if store.coordinator_enrolled(db, record, address):
        return True
    return record["coordinator"] == address and not store.worker_of_generation(db, record, address)


def _existing(db: Connection, scope: str, label: str, project_id: str) -> dict[str, object] | None:
    found = cast(
        "tuple[str] | None",
        db.execute(
            "SELECT label FROM coordination_projects WHERE scope=? AND (label=? OR project_id=?)",
            (scope, label, project_id),
        ).fetchone(),
    )
    return None if found is None else store.project(db, scope, found[0])


def _setup(db: Connection, scope: str, label: str, args: dict[str, object], address: str) -> None:
    """Receiver-owner import of a coordinator binding, with its own gate delegation if given."""
    if args.get("project_execution"):
        raise BusError("Remote setup cannot grant --project-execution")
    current = _existing(db, scope, label, uuid4_string(str(args["project_id"])))
    if current is not None:
        _refuse_worker(db, "setup", current, address)
    coordinator = _address(args["coordinator"])
    _create(db, scope, label, args, coordinator)
    specs = cast("list[str]", args.get("delegate") or [])
    if specs:
        delegation.set_project_delegation(
            db,
            store.project(db, scope, label),
            delegation.Delegation.from_specs(specs),
            address,
        )


def _create(
    db: Connection, scope: str, label: str, args: dict[str, object], coordinator: str
) -> None:
    references = cast("list[object]", args.get("reference", []))
    if len(references) > MAX_REFERENCES:
        raise BusError("At most 32 local references allowed")
    workspace = beads_workspace(args.get("beads_workspace"))
    db.execute(
        "INSERT INTO coordination_projects VALUES (?,?,?,?,?,?,?,?,?,NULL)",
        (
            scope,
            uuid4_string(str(args.get("project_id") or uuid4())),
            label,
            uuid4_string(str(args.get("generation") or uuid4())),
            coordinator,
            text_reference(args.get("objective")),
            workspace,
            text_reference(args.get("memory_ticket")),
            json.dumps([_path(ref) for ref in references]),
        ),
    )
    record = store.project(db, scope, label)
    db.execute(
        "INSERT INTO coordination_generations VALUES (?,?,?)",
        (scope, record["project_id"], record["generation"]),
    )


def _transition(  # noqa: C901, PLR0912 - bounded CLI action dispatch
    db: Connection,
    action: str,
    record: dict[str, object],
    args: dict[str, object],
    address: str,
) -> int | None:
    # pylint: disable=too-many-branches
    key = (record["scope"], record["project_id"])
    _owner_gate(db, action, record, address)
    if action == "coordinate":
        if record["coordinator"] != address:
            raise BusError("Different coordinator; owner must use coordination replace")
        store.enroll(db, record, address, "coordinator")
        if args.get("unattended"):
            runs.activate(db, record, args, address)
    elif action == "join":
        grant_id = store.join_session(db, record, address)
        delegation.bind(db, record, grant_id, cast("list[str] | None", args.get("narrow")))
        if "/" not in str(record["coordinator"]):
            db.execute(
                "INSERT INTO coordination_targets(scope,project_id,recipient,grant_id,generation) "
                "VALUES (?,?,?,?,?) ON CONFLICT(scope,project_id,recipient) DO UPDATE SET "
                "grant_id=excluded.grant_id,generation=excluded.generation",
                (*key, address, grant_id, record["generation"]),
            )
            return record_local(db, record, address, grant_id)
    elif action == "amend":
        _amend(db, record, args, address)
    elif action == "claim":
        _claim(db, record, args, address)
    elif action == "grant":
        expiry = args.get("expires_at")
        if expiry is None and args.get("until_revoked") is not True:
            raise BusError("Select explicit grant lifetime")
        if expiry is not None and timestamp(str(expiry)) <= datetime.now(UTC):
            raise BusError("Expiry must be in future")
        recipient = _address(args["recipient"])
        _revoke(db, record, recipient)
        db.execute(
            "INSERT INTO coordination_grants VALUES (?,?,?,?,?,?,?,?,NULL)",
            (
                *key,
                recipient,
                str(uuid4()),
                record["generation"],
                record["coordinator"],
                json.dumps(OPERATIONS),
                expiry,
            ),
        )
    elif action == "target":
        db.execute(
            "INSERT INTO coordination_targets VALUES (?,?,?,?,?) "
            "ON CONFLICT(scope,project_id,recipient) DO UPDATE SET "
            "grant_id=excluded.grant_id,generation=excluded.generation",
            (
                *key,
                _address(args["recipient"]),
                uuid4_string(str(args["grant_id"])),
                record["generation"],
            ),
        )
    elif action == "leave":
        if (
            db.execute(
                "SELECT 1 FROM coordination_enrollments "
                "WHERE scope=? AND project_id=? AND address=?",
                (*key, address),
            ).fetchone()
            is not None
        ):
            store.end_session_consents(db, address)
        db.execute(
            "DELETE FROM coordination_enrollments WHERE scope=? AND project_id=? AND address=?",
            (*key, address),
        )
        db.execute(
            "DELETE FROM coordination_claims WHERE address=? AND scope=? AND project_id=?",
            (address, *key),
        )
    elif action == "revoke":
        _revoke(db, record, args.get("recipient"))
    elif action == "replace":
        generation = uuid4_string(str(args["generation"]))
        if generation == record["generation"]:
            raise BusError("Replacement requires fresh generation UUIDv4")
        coordinator = _address(args["coordinator"])
        db.execute("INSERT INTO coordination_generations VALUES (?,?,?)", (*key, generation))
        _revoke(db, record, None)
        db.execute("DELETE FROM coordination_targets WHERE scope=? AND project_id=?", key)
        db.execute(
            "UPDATE coordination_projects SET generation=?,coordinator=?,revoked_at=NULL "
            "WHERE scope=? AND project_id=?",
            (generation, coordinator, *key),
        )
        if coordinator == address:
            store.enroll(db, {**record, "generation": generation}, address, "coordinator")
    return None


def _require_coordinator(
    db: Connection, record: dict[str, object], address: str, message: str
) -> None:
    if not store.coordinator_enrolled(db, record, address):
        raise BusError(message)


def _amend(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str
) -> None:
    """Append an objective amendment and/or replace or remove the delegation (applies at once)."""
    specs = cast("list[str]", args.get("delegate") or [])
    flags = _flags(args)
    objective = args.get("objective")
    if "/" in str(record["coordinator"]):
        # No local session is the remote coordinator, so this carries the receiver owner's own
        # authority over gate delegation. Every other record stays on the coordinator machine.
        if _flagged(flags) or objective is not None:
            raise BusError(
                "Remote-coordinated project: amend accepts only --delegate or --no-delegation"
            )
    elif not _owner_coordinator(db, record, address):
        raise BusError("Only the enrolled coordinator may amend delegation")
    if args.get("no_delegation"):
        if _flagged(flags):
            raise BusError("--no-delegation takes no delegation flags")
        change, wanted = True, None
    elif specs or _flagged(flags):
        change, wanted = True, delegation.Delegation.from_specs(specs, **flags)
    elif objective is None:
        raise BusError("amend needs --objective or a delegation change")
    else:
        change, wanted = False, None
    if objective is not None:
        amendments.append(db, record, text_reference(objective), address)
    if change:
        delegation.set_project_delegation(db, record, wanted, address)


def _claim(
    db: Connection, record: dict[str, object], args: dict[str, object], address: str
) -> None:
    """Enrolled worker claims one ticket and branch; no other current worker may hold either."""
    key = (record["scope"], record["project_id"])
    ticket = ticket_text(args["ticket"])
    branch = branch_name(args["branch"])
    enrolled = cast(
        "tuple[int] | None",
        db.execute(
            "SELECT 1 FROM coordination_enrollments WHERE scope=? AND project_id=? AND address=? "
            "AND role='worker' AND generation=?",
            (*key, address, record["generation"]),
        ).fetchone(),
    )
    if enrolled is None:
        raise BusError("Only an enrolled worker may claim a ticket")
    holder = cast(
        "tuple[str] | None",
        db.execute(
            "SELECT c.address FROM coordination_claims c JOIN coordination_enrollments e "
            "ON e.scope=c.scope AND e.project_id=c.project_id AND e.address=c.address "
            "WHERE c.scope=? AND c.project_id=? AND c.address<>? AND e.role='worker' "
            "AND e.generation=? AND (c.ticket=? OR c.branch=?)",
            (*key, address, record["generation"], ticket, branch),
        ).fetchone(),
    )
    if holder is not None:
        raise BusError(f"Ticket or branch already claimed by {holder[0]}")
    db.execute(
        "INSERT OR REPLACE INTO coordination_claims VALUES (?,?,?,?,?,?)",
        (address, *key, ticket, branch, datetime.now(UTC).isoformat()),
    )


def _revoke(db: Connection, record: dict[str, object], recipient: object) -> None:
    key = (record["scope"], record["project_id"])
    clause = " AND recipient=?" if recipient is not None else ""
    values = (*key, _address(recipient)) if recipient is not None else key
    now = datetime.now(UTC).isoformat()
    db.execute(
        # Only fixed internal clauses enter SQL; all owner data remains bound.
        "UPDATE coordination_grants SET revoked_at=? "  # noqa: S608
        "WHERE scope=? AND project_id=?" + clause,
        (now, *values),
    )
    db.execute(
        "UPDATE coordination_session_consents SET ended_at=? WHERE ended_at IS NULL "
        "AND grant_id IN (SELECT grant_id FROM coordination_grants WHERE revoked_at IS NOT NULL)",
        (now,),
    )
    db.execute(
        "DELETE FROM coordination_targets WHERE grant_id IN "
        "(SELECT grant_id FROM coordination_grants WHERE revoked_at IS NOT NULL)"
    )
    if recipient is None:
        db.execute(
            "UPDATE coordination_projects SET revoked_at=? WHERE scope=? AND project_id=?",
            (now, *key),
        )
    db.execute(
        "DELETE FROM coordination_claims WHERE scope=? AND project_id=?"  # noqa: S608
        + (" AND address=?" if recipient is not None else ""),
        values,
    )
    db.execute(
        "DELETE FROM coordination_enrollments WHERE scope=? AND project_id=?"  # noqa: S608
        + (" AND address=?" if recipient is not None else ""),
        values,
    )
