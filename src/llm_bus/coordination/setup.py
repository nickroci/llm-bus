"""Guided coordinator setup: agent profile and terminal confirmation wizard."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from llm_bus.coordination import cli, delegation
from llm_bus.errors import BusError

if TYPE_CHECKING:
    from collections.abc import Callable

    from llm_bus.mailbox.store import Store

ORIGIN_PREFIX = "origin/"
BRANCH_HINT = (
    "Detected from origin/HEAD; check the repository instructions for a different "
    "integration branch."
)
ORIGIN_HEAD_TIMEOUT = 10
YES = "yes"
DEFAULT_MINUTES = 30
DISCOVERY_STEP = (
    "Give read-only discovery to one native subagent as the llm-bus skill's "
    "`references/coordination/coordinator.md` describes: Beads workspace, memory ticket "
    "readability via `bd show`, reference files, any repository-instruction override of the "
    "detected integration branch, and missing facts. It returns the setup brief and never "
    "runs llm-bus. Confirm the integration branch with the owner in the next step; you ask "
    "the owner and run every command."
)
INSTRUCTIONS_STEP = (
    "Optional: set owner standing instructions with llm-bus coordination instructions "
    "PROJECT --audience coordinator|worker --file PATH"
)
PROJECT_EXECUTION_QUESTION = (
    "Preapprove paid or live QA runs and development deployments for this project?"
)
PROJECT_EXECUTION_REVIEW = "Preapprove paid or live QA runs and development deployments: "
PROJECT_EXECUTION_OWNER_STEP = (
    " Default: No. Use explicit owner interaction; a recognized wake does not "
    "authenticate the owner's words. Add --project-execution only after explicit "
    "owner opt-in. Before storing, review the selection for paid or live QA runs "
    "and development deployments with the owner, including these limits: "
)
PROJECT_EXECUTION_LIMITS = (
    "Actions must advance the confirmed project objective. Approval applies only to active, "
    "verified same-machine members of this project and generation, and is revocable. "
    "Merges need separate valid authorization and passing CI; native and provider permissions "
    "and repository rules still apply. Production operations, credential or secret changes, "
    "force-pushes, and merges with failing or pending CI remain excluded. If a deployment "
    "cannot be identified as development, this approval does not cover it."
)


def detect_integration_branch(cwd: str) -> tuple[str | None, str]:
    """Read the local origin/HEAD symbolic ref; never contacts the network."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed git arguments
            [  # noqa: S607
                "git",
                "-C",
                cwd,
                "symbolic-ref",
                "--quiet",
                "--short",
                "refs/remotes/origin/HEAD",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=ORIGIN_HEAD_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None, "undetected"
    name = done.stdout.strip()
    if done.returncode != 0 or not name.startswith(ORIGIN_PREFIX):
        return None, "undetected"
    return name.removeprefix(ORIGIN_PREFIX), "origin-head"


def _items(branch: str | None) -> list[str]:
    return [] if branch is None else [f"{gate}:{branch}" for gate in delegation.GATES]


def profile(cwd: str) -> dict[str, object]:
    """Default setup for an agent to confirm with the owner; stores nothing."""
    branch, source = detect_integration_branch(cwd)
    flags = [] if branch is None else [delegation.AUTO_MERGE]
    flags += [delegation.OWNER_DECISIONS, delegation.DECISION_DEFAULTS]
    return {
        "delegate": _items(branch),
        "flags": flags,
        "integration_branch": {"name": branch, "source": source},
        "floor": list(delegation.FLOOR),
        "project_execution": {
            "question": PROJECT_EXECUTION_QUESTION,
            "default": False,
            "limits": PROJECT_EXECUTION_LIMITS,
        },
        "steps": [
            DISCOVERY_STEP,
            (
                "Ask the owner through the native question UI: objective, Beads workspace, "
                "memory ticket, integration branch, each delegated gate, and each flag "
                "(auto-merge needs a merge or merge-admin gate; the coordinator still accepts "
                "every gate record; owner-decisions needs no gate; decision-defaults needs "
                "owner-decisions and, after the minutes, applies the recommended option of "
                "an unanswered scope decision, never a security, cost, data, irreversible "
                "or objective one)."
            ),
            "The owner may disable items or change target branches, never add floor items.",
            (
                "After the existing choices, ask the owner through the native question UI: "
                + PROJECT_EXECUTION_QUESTION
                + PROJECT_EXECUTION_OWNER_STEP
                + PROJECT_EXECUTION_LIMITS
            ),
            "Run only the confirmed flags with the command below.",
            INSTRUCTIONS_STEP,
        ],
        "command": (
            "llm-bus --format agent coordinate PROJECT --objective OBJECTIVE "
            "--beads-workspace PATH --memory-ticket TICKET --delegate GATE:BRANCH ..."
            + (" --auto-merge" if delegation.AUTO_MERGE in flags else "")
            + f" --owner-decisions --decision-defaults {DEFAULT_MINUTES}"
        ),
    }


def _gate_enabled(answer: str) -> bool:
    """Default profile is the fullest delegation: Enter keeps the gate enabled."""
    return answer.strip().lower() in {"", "y", YES}


def _default_minutes(ask: Callable[[str], str]) -> int | None:
    """Minutes before an unanswered scope decision takes its recommendation; Enter keeps 30."""
    answer = ask(f"Decision defaults after minutes [{DEFAULT_MINUTES}]/no: ").strip()
    if answer.lower() in {"no", "n"}:
        return None
    if not answer:
        return DEFAULT_MINUTES
    if not answer.isdecimal():
        raise BusError("Enter minutes (1 to 1440) or no")
    return int(answer)


def _coordinator(store: Store, cwd: str, identity: str | None, ask: Callable[[str], str]) -> str:
    if identity is not None:
        answer = ask(f"Coordinator [{identity}]: ").strip()
        return answer or identity
    choices = [agent["address"] for agent in store.agents(cwd)]
    if not choices:
        raise BusError("No registered sessions in this folder; run coordinate inside the agent")
    listing = "\n".join(f"  {number}. {address}" for number, address in enumerate(choices, 1))
    answer = ask(f"Coordinator session:\n{listing}\nNumber: ").strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(choices):
        raise BusError("Pick a listed coordinator number")
    return choices[int(answer) - 1]


def _cancellable(ask: Callable[[str], str]) -> Callable[[str], str]:
    """End of input or Ctrl-C at any prompt is a clean error; nothing is stored."""

    def guarded(prompt: str) -> str:
        try:
            return ask(prompt)
        except (EOFError, KeyboardInterrupt) as error:
            raise BusError("Setup cancelled; nothing stored") from error

    return guarded


def run_wizard(  # noqa: PLR0913 - explicit inputs
    store: Store,
    label: str,
    cwd: str,
    identity: str | None,
    *,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> dict[str, object] | None:
    """Ask, show a review screen and store only after an explicit yes."""
    # pylint: disable=too-many-locals,too-many-arguments
    label = cli.new_project_label(store, cwd, label)
    detected, _source = detect_integration_branch(cwd)
    workspace_default = cwd if (Path(cwd) / ".beads").exists() else ""
    ask = _cancellable(ask)
    objective = cli.text_reference(ask("Objective: ").strip())
    workspace = cli.beads_workspace(
        ask(f"Beads workspace [{workspace_default}]: ").strip() or workspace_default
    )
    ticket = cli.text_reference(ask("Memory ticket: ").strip())
    branch = ask(f"Integration branch [{detected or ''}]: ").strip() or detected or ""
    specs = (
        [
            f"{gate}:{branch}"
            for gate in delegation.GATES
            if _gate_enabled(ask(f"Delegate {gate}? [yes]/no: "))
        ]
        if branch
        else []
    )
    merge_chosen = any(spec.split(":", 1)[0] in delegation.MERGE_GATES for spec in specs)
    auto_merge = merge_chosen and _gate_enabled(ask("Delegate auto-merge? [yes]/no: "))
    # Owner decisions need no gate (D24), so they are asked whatever gates remain (D27).
    owner_decisions = _gate_enabled(ask("Delegate owner decisions? [yes]/no: "))
    minutes = _default_minutes(ask) if owner_decisions else None
    coordinator = _coordinator(store, cwd, identity, ask)
    project_execution = ask(PROJECT_EXECUTION_QUESTION + " yes/[no]: ").strip().lower() == YES
    chosen = (
        delegation.Delegation.from_specs(
            specs,
            auto_merge=auto_merge,
            owner_decisions=owner_decisions,
            decision_default_minutes=minutes,
            project_execution=project_execution,
        )
        if specs or owner_decisions or project_execution
        else None
    )
    args: dict[str, object] = {
        "objective": objective,
        "beads_workspace": workspace,
        "memory_ticket": ticket,
        "delegate": specs,
        "auto_merge": auto_merge,
        "owner_decisions": owner_decisions,
        "decision_default_minutes": minutes,
        "project_execution": project_execution,
    }
    _review(say, label, coordinator, args, chosen)
    if ask("Store this setup? (type yes): ").strip().lower() != YES:
        say("Nothing stored.")
        return None
    created = cli.create_for(store, cwd, label, args, coordinator, current=identity)
    say(INSTRUCTIONS_STEP)
    return created


def _review(
    say: Callable[[str], None],
    label: str,
    coordinator: str,
    args: dict[str, object],
    chosen: delegation.Delegation | None,
) -> None:
    say(f"Project: {label}")
    say(f"Coordinator: {coordinator}")
    say(f"Objective: {args['objective']}")
    say(f"Beads workspace: {args['beads_workspace']}")
    say(f"Memory ticket: {args['memory_ticket']}")
    say(BRANCH_HINT)
    say("Delegations: " + (", ".join(chosen.items()) if chosen else "none"))
    approved = "yes" if args.get("project_execution") else "no"
    say(PROJECT_EXECUTION_REVIEW + approved)
    say(PROJECT_EXECUTION_LIMITS)
    if chosen is not None and chosen.targets("merge-admin"):
        targets = ", ".join(chosen.targets("merge-admin"))
        say(f"merge-admin bypasses only the required-approvals rule on: {targets}")
    if chosen is not None and chosen.auto_merge:
        say(
            "auto-merge: each merge still needs the coordinator to accept the worker's gate "
            "record, which sends the merge approval; the worker checks CI before merging."
        )
    if chosen is not None and chosen.decision_default_minutes is not None:
        say(
            "decision-defaults: an unanswered scope decision with a recommended option takes "
            f"it after {chosen.decision_default_minutes} minutes and the coordinator and the "
            "waiting worker are woken; other decision classes never default."
        )
    say("Never delegated (floor): " + ", ".join(delegation.FLOOR))
