"""Offline lifecycle acceptance through public commands and the native hook callback."""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, cast

import pytest

from llm_bus.cli.app import main
from llm_bus.coordination import recovery
from llm_bus.errors import BusError
from llm_bus.mailbox.models import SenderContext
from llm_bus.mailbox.store import Store
from tests.coordination.test_approvals import SHA, Project
from tests.coordination.test_decisions import show
from tests.coordination.test_enrollment import invoke
from tests.coordination.test_gate_records import submit
from tests.coordination.test_runs import SCOPE, command
from tests.coordination.test_unattended_actions import (
    OWNER,
    TICKET,
    WORKER,
    activate,
    cancelled,
    github_fixture,
    move,
    no_timers,
    observation_fixture,
    pending,
    progress,
    project_fixture,
    replacement,
    resolve,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# Re-export the existing isolated fixtures rather than introducing a second provider harness.
__all__ = ["github_fixture", "no_timers", "observation_fixture", "project_fixture"]


@pytest.fixture(name="public")
def public_fixture(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Callable[..., object]:
    monkeypatch.chdir(project.path)
    monkeypatch.setenv("LLM_BUS_DB", str(project.path / "bus.db"))
    monkeypatch.setenv("LLM_BUS_CONFIG", str(project.path / "missing.toml"))
    monkeypatch.setenv("CODEX_THREAD_ID", "owner")

    def run(*argv: str, stdin: str = "") -> object:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        assert main(list(argv)) == 0
        output = capsys.readouterr().out
        return output if argv[-1] == "guidance" else cast("object", json.loads(output))

    return run


def run_command(
    project: Project, action: str, run_id: str, *flags: str, **fields: str
) -> dict[str, object]:
    options = [
        part for key, value in fields.items() for part in (f"--{key.replace('_', '-')}", value)
    ]
    return command(project, action, "--run-id", run_id, *flags, *options)


def checkpoint(project: Project, run_id: str, worker: str, revision: str, state: str) -> None:
    run_command(
        project,
        "run-checkpoint",
        run_id,
        ticket="blocked-2",
        worker=worker,
        worktree=str(project.path),
        revision=revision,
        state=state,
        reason="owner" if state == "blocked" else "none",
    )


def reviewed(project: Project, run_id: str, record_id: str) -> dict[str, object]:
    return run_command(
        project, "run-result", run_id, record_id=record_id, repository="owner/repository"
    )


def test_offline_acceptance_resumes_wake_handoff_and_reviewed_completion(  # noqa: PLR0915
    project: Project,
    github: dict[str, object],
    observation: dict[str, object],
    public: Callable[..., object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host callback is real; native queue delivery and provider responses are fixtures."""
    # pylint: disable=too-many-locals,too-many-statements
    observed = observation.copy()

    def read(*_args: object, **_kwargs: object) -> dict[str, object]:
        return dict(observed)

    monkeypatch.setattr(recovery, "observe", read)
    profile = cast("dict[str, object]", public("coordinate", "demo", "--setup"))
    assert cast("dict[str, object]", profile["mode"])["choices"] == ["unattended", "guided"]
    assert cast("dict[str, object]", command(project, "show")["unattended"])["active"] is False
    run_id = activate(project, "--ticket", "blocked-2")
    original = pending(project)
    blocker = replacement(project)
    project.claim(blocker, "blocked-2", "blocked-branch")
    observed["branch"] = "blocked-branch"
    checkpoint(project, run_id, blocker, "0", "blocked")
    observed["branch"] = "roam-22m-login"
    progress(project, run_id, 0, "failed", "--reason", "recoverable")

    # A fresh Store and the normal app entrypoint reopen the persisted mailbox.
    project.store = Store(project.path / "bus.db")
    sent = project.store.send(
        SenderContext("claude", "worker", str(project.path)), OWNER, "Retry safe read-only work"
    )
    callback = cast(
        "dict[str, object]",
        public(
            "hook",
            "codex",
            stdin=json.dumps(
                {
                    "session_id": "owner",
                    "hook_event_name": "UserPromptSubmit",
                    "cwd": str(project.path),
                    "prompt": f"llm-bus message #{sent['id']} from {WORKER}. "
                    "Read and handle it with `llm-bus inbox`.",
                }
            ),
        ),
    )
    assert (
        cast("dict[str, object]", callback["hookSpecificOutput"])["hookEventName"]
        == "UserPromptSubmit"
    )
    assert run_id in str(public("--format", "agent", "guidance"))
    inbox = cast("list[dict[str, object]]", public("inbox"))
    assert any(row["body"] == "Retry safe read-only work" for row in inbox)
    assert resolve(project, original, run_id)["source"] == "coordinator"
    with pytest.raises(BusError, match="recognized wake"):
        command(project, "unattended", *SCOPE, "--confirm")
    retried = run_command(
        project,
        "run-retry",
        run_id,
        "--read-only",
        ticket=TICKET,
        worktree=str(project.path),
        revision="1",
    )
    assert retried["attempts"] == 1
    progress(project, run_id, 2, "ready")
    shown = cast("dict[str, object]", command(project, "show")["unattended"])
    summary = cast("dict[str, object]", cast("dict[str, object]", shown["run"])["summary"])
    assert summary["ready"] == [TICKET]
    assert summary["blockers"] == [{"ticket": "blocked-2", "reason": "owner"}]

    successor = "claude:successor"
    project.store.register("claude", "successor", str(project.path))
    invoke(project.store, project.path, "join", "demo", address=successor)
    moved = move(project, cancelled(project, run_id), successor)
    copied = cast("list[str]", moved["decisions"])[0]
    assert moved["fresh_gates_required"] is True
    assert show(project, copied, successor)["original_decision_id"] == original
    assert show(project, copied, successor)["source"] == "coordinator"
    assert show(project, copied)["accepted"] is False
    with pytest.raises(BusError, match="wrong_ticket"):
        submit(project, run_id=run_id)
    result = reviewed(
        project, run_id, str(submit(project, address=successor, run_id=run_id)["record_id"])
    )
    assert result["status"] == "reviewed"
    assert result["head_sha"] == SHA
    with pytest.raises(BusError, match="Completion denied"):
        command(project, "unattended-complete", "--run-id", run_id)

    # Resolve the independent blocker only after the first ticket has a reviewed result.
    observed["branch"] = "blocked-branch"
    checkpoint(project, run_id, blocker, "1", "ready")
    cast("dict[str, object]", github["other_prs"])["43"] = {
        **cast("dict[str, object]", github["pr"]),
        "headRefName": "blocked-branch",
    }
    second = str(
        submit(
            project,
            address=blocker,
            ticket="blocked-2",
            branch="blocked-branch",
            pr=43,
            run_id=run_id,
        )["record_id"]
    )
    reviewed(project, run_id, second)
    ended = cast(
        "dict[str, object]",
        public("coordination", "unattended-complete", "demo", "--run-id", run_id),
    )
    assert ended["end_reason"] == "completed"
    resumed = invoke(Store(project.path / "bus.db"), project.path, "coordinate", "demo")
    history = cast(
        "list[dict[str, object]]", cast("dict[str, object]", resumed["unattended"])["history"]
    )
    final = cast("dict[str, object]", history[-1]["summary"])
    assert final["complete"] is True
    assert final["end_reason"] == "completed"
    assert len(cast("list[object]", final["handoffs"])) == 1
    assert len(cast("list[object]", final["decisions"])) == 2
    results = cast("list[dict[str, object]]", final["results"])
    assert {(row["pr"], row["head_sha"]) for row in results} == {(42, SHA), (43, SHA)}
    assert all(
        row["independent_review"] is True and row["required_checks"] == ["ci"] for row in results
    )
    assert github["merge"] == []
