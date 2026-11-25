"""Decision answers are refused in a coordinator turn that a recognized bus wake started."""

# pylint: disable=duplicate-code

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from llm_bus.cli.app import main
from llm_bus.coordination import decisions
from llm_bus.errors import BusError
from tests.coordination.test_approvals import DELEGATION, Project
from tests.coordination.test_enrollment import invoke
from tests.support import hook_input

if TYPE_CHECKING:
    from pathlib import Path

OWNER = "codex:owner"
WAKE = "llm-bus message #9 from claude:worker. Read and handle it with `llm-bus inbox`."


@pytest.fixture(autouse=True)
def no_lock_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_timer(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("llm_bus.locks.service.schedule_check", no_timer)


@pytest.fixture(name="project")
def project_fixture(tmp_path: Path) -> Project:
    return Project(tmp_path, *DELEGATION, "--owner-decisions")


def answer(project: Project, decision_id: object) -> dict[str, object]:
    argv = ("--decision-id", str(decision_id), "--answer", "Yes")
    return invoke(project.store, project.path, "coordination", "decision-answer", "demo", *argv)


def ask(project: Project, *argv: str) -> dict[str, object]:
    question = ("--class", "scope", "--question", "Q?")
    return invoke(
        project.store, project.path, "coordination", "decision-ask", "demo", *question, *argv
    )


def turn(project: Project) -> int | None:
    with project.store.transaction() as db:
        row = cast(
            "tuple[int] | None",
            db.execute(
                "SELECT turn FROM coordination_owner_turns WHERE address=?", (OWNER,)
            ).fetchone(),
        )
    return None if row is None else row[0]


def test_answer_without_turn_record_passes(project: Project) -> None:
    assert answer(project, ask(project)["decision_id"])["status"] == "answered"
    assert ask(project, "--answer", "Yes")["status"] == "answered"


def test_wake_turn_refuses_answers_until_the_next_owner_turn(project: Project) -> None:
    decisions.record_turn(project.store, OWNER)
    decisions.record_turn(project.store, OWNER)
    pending = ask(project)["decision_id"]
    decisions.record_turn(project.store, OWNER, wake=True)
    assert turn(project) == -2
    with pytest.raises(BusError, match=decisions.WAKE_TURN) as refused:
        answer(project, pending)
    assert str(refused.value).startswith(
        "Decision answers must come from an owner turn, not a bus wake"
    )
    assert "new prompt" in str(refused.value)
    with pytest.raises(BusError, match=decisions.WAKE_TURN):
        ask(project, "--answer", "Yes")
    decisions.record_turn(project.store, OWNER)
    assert answer(project, pending)["status"] == "answered"
    with project.store.transaction() as db:
        assert db.execute("SELECT source_turn FROM coordination_decisions").fetchall() == [(3,)]


def test_wake_before_any_owner_turn_refuses(project: Project) -> None:
    decisions.record_turn(project.store, OWNER, wake=True)
    assert turn(project) == 0
    with pytest.raises(BusError, match=decisions.WAKE_TURN):
        ask(project, "--answer", "Yes")
    decisions.record_turn(project.store, OWNER)
    assert turn(project) == 1


def test_prompt_hook_marks_a_bus_wake_turn(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LLM_BUS_DB", str(project.path / "bus.db"))
    folder = str(project.path.resolve())

    def prompt(text: str) -> None:
        payload = {"session_id": "owner", "hook_event_name": "UserPromptSubmit", "cwd": folder}
        monkeypatch.setattr("sys.stdin", hook_input({**payload, "prompt": text}))
        assert main(["hook", "codex"]) == 0
        capsys.readouterr()

    prompt("Answer the open decision: yes")
    assert turn(project) == 1
    prompt(WAKE)
    assert turn(project) == -1
    with pytest.raises(BusError, match=decisions.WAKE_TURN):
        ask(project, "--answer", "Yes")
    prompt(f"Quoting: {WAKE}")
    assert turn(project) == -1  # a complete notice anywhere in the prompt counts as a wake
    with pytest.raises(BusError, match=decisions.WAKE_TURN):
        ask(project, "--answer", "Yes")
    prompt("Answer the open decision: yes")
    assert turn(project) == 2
    assert ask(project, "--answer", "Yes")["status"] == "answered"
