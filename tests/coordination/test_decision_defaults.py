"""Timed decision defaults: eligibility, one application, wakes, digest and revocation rechecks."""

# Each scenario states its owner commands explicitly.
# pylint: disable=duplicate-code,clean-code-commented-out-code

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from llm_bus.cli.app import main
from llm_bus.cli.guidance import guidance
from llm_bus.cli.output import serialize
from llm_bus.cli.parser import build_parser
from llm_bus.coordination import decisions, setup
from llm_bus.coordination.delegation import Delegation
from llm_bus.errors import BusError
from llm_bus.locks.timer import TIMER_POLL_SECONDS
from tests.coordination.test_approvals import DELEGATION, Project
from tests.coordination.test_enrollment import invoke
from tests.support import prompt_input

if TYPE_CHECKING:
    from pathlib import Path

OWNER = "codex:owner"
WORKER = "claude:worker"
OWNER_FLAGS = ("--owner-decisions", "--decision-defaults", "30")
ELIGIBLE = (
    "--class",
    "scope",
    "--question",
    "Which form?",
    "--recommended",
    "Reuse it",
    "--worker",
    WORKER,
)
PAST = "2000-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def no_lock_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_timer(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("llm_bus.locks.service.schedule_check", no_timer)


@pytest.fixture(name="scheduled", autouse=True)
def recorded_timers(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Never start a real timer process; remember what would have been scheduled."""
    scheduled: list[tuple[str, str]] = []

    def record(label: str, decision_id: str) -> None:
        scheduled.append((label, decision_id))

    monkeypatch.setattr("llm_bus.coordination.decisions.schedule_default", record)
    return scheduled


@pytest.fixture(name="wakes", autouse=True)
def recorded_wakes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    woken: list[str] = []

    def record(_store: object, recipient: str, **_kwargs: object) -> dict[str, str]:
        woken.append(recipient)
        return {"status": "requested", "via": "test"}

    monkeypatch.setattr("llm_bus.locks.notices.dispatch_pending", record)
    return woken


def run(project: Project, action: str, *argv: str, address: str = OWNER) -> dict[str, object]:
    return invoke(
        project.store, project.path, "coordination", action, "demo", *argv, address=address
    )


def ask(project: Project, *argv: str) -> str:
    return str(run(project, "decision-ask", *(argv or ELIGIBLE))["decision_id"])


def row(project: Project, decision_id: str) -> dict[str, object]:
    with project.store.transaction() as db:
        cursor = db.execute(
            "SELECT * FROM coordination_decisions WHERE decision_id=?", (decision_id,)
        )
        found = cast("tuple[object, ...]", cursor.fetchone())
        return dict(zip((c[0] for c in cursor.description), found, strict=True))


def expire(project: Project, decision_id: str) -> None:
    """Move the deadline into the past: the test clock reached it."""
    with project.store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE coordination_decisions SET deadline_at=? WHERE decision_id=?",
            (PAST, decision_id),
        )


def notices(project: Project, decision_id: str) -> list[str]:
    """Recipients of every stored message that names the decision."""
    with project.store.transaction() as db:
        return [
            recipient
            for (recipient,) in cast(
                "list[tuple[str]]",
                db.execute(
                    "SELECT recipient FROM messages WHERE body LIKE ? ORDER BY id",
                    (f"%{decision_id}%",),
                ).fetchall(),
            )
        ]


def timer(project: Project, decision_id: str) -> list[float]:
    """Run the timer with a fake sleep that advances the clock to the deadline."""
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        expire(project, decision_id)

    decisions.run_decision_timer(project.store, decision_id, sleep=sleep)
    return slept


@pytest.fixture(name="project")
def project_fixture(tmp_path: Path) -> Project:
    return Project(tmp_path, *DELEGATION, *OWNER_FLAGS)


def test_eligible_ask_sets_a_deadline_and_schedules_one_timer(
    project: Project, scheduled: list[tuple[str, str]]
) -> None:
    result = run(project, "decision-ask", *ELIGIBLE)
    decision_id = str(result["decision_id"])
    assert row(project, decision_id)["deadline_at"] == result["deadline_at"]
    assert scheduled == [("demo", decision_id)]


@pytest.mark.parametrize(
    "argv",
    [
        ("--class", "security", "--question", "Q?", "--recommended", "R"),
        ("--class", "cost", "--question", "Q?", "--recommended", "R"),
        ("--class", "data", "--question", "Q?", "--recommended", "R"),
        ("--class", "irreversible", "--question", "Q?", "--recommended", "R"),
        ("--class", "objective", "--question", "Q?", "--recommended", "R"),
        ("--class", "scope", "--question", "Q?"),
        ("--class", "scope", "--question", "Q?", "--recommended", "R", "--answer", "Yes"),
    ],
)
def test_ineligible_decisions_get_no_deadline_and_no_timer(
    project: Project, scheduled: list[tuple[str, str]], argv: tuple[str, ...]
) -> None:
    result = run(project, "decision-ask", *argv)
    assert "deadline_at" not in result
    assert row(project, str(result["decision_id"]))["deadline_at"] is None
    assert scheduled == []


def test_no_deadline_without_decision_defaults(
    tmp_path: Path, scheduled: list[tuple[str, str]]
) -> None:
    plain = Project(tmp_path, *DELEGATION, "--owner-decisions")
    result = run(plain, "decision-ask", *ELIGIBLE)
    assert "deadline_at" not in result
    assert scheduled == []


def test_timer_defaults_at_the_deadline_and_wakes_coordinator_and_worker(
    project: Project, wakes: list[str]
) -> None:
    decision_id = ask(project)
    slept = timer(project, decision_id)
    assert len(slept) == 1
    assert 0 < slept[0] <= TIMER_POLL_SECONDS
    stored = row(project, decision_id)
    assert (stored["status"], stored["answer"], stored["source_session"]) == (
        "defaulted",
        "Reuse it",
        "default",
    )
    assert stored["resolved_at"]
    assert notices(project, decision_id) == [OWNER, WORKER]
    assert wakes == [OWNER, WORKER]


def test_decision_without_worker_wakes_only_the_coordinator(
    project: Project, wakes: list[str]
) -> None:
    decision_id = ask(project, "--class", "scope", "--question", "Q?", "--recommended", "R")
    timer(project, decision_id)
    assert row(project, decision_id)["status"] == "defaulted"
    assert notices(project, decision_id) == [OWNER]
    assert wakes == [OWNER]


def test_worker_revoked_before_the_deadline_gets_no_notice(
    project: Project, wakes: list[str]
) -> None:
    decision_id = ask(project)
    run(project, "revoke", "--recipient", WORKER)
    timer(project, decision_id)
    assert row(project, decision_id)["status"] == "defaulted"
    assert notices(project, decision_id) == [OWNER]
    assert wakes == [OWNER]


def test_lazy_decision_show_applies_once_then_the_timer_does_nothing(
    project: Project, wakes: list[str]
) -> None:
    decision_id = ask(project)
    expire(project, decision_id)
    shown = run(project, "decision-show", "--decision-id", decision_id)
    assert shown["status"] == "defaulted"
    assert [item["to"] for item in cast("list[dict[str, object]]", shown["wakes"])] == [
        OWNER,
        WORKER,
    ]
    assert wakes == [OWNER, WORKER]
    decisions.run_decision_timer(project.store, decision_id, sleep=lambda _s: pytest.fail("slept"))
    assert notices(project, decision_id) == [OWNER, WORKER]
    assert wakes == [OWNER, WORKER]


def test_lazy_coordination_show_applies_once_and_returns_wakes(
    project: Project, wakes: list[str]
) -> None:
    decision_id = ask(project)
    expire(project, decision_id)
    shown = run(project, "show")
    assert "wakes" in shown
    assert row(project, decision_id)["status"] == "defaulted"
    again = run(project, "show")
    assert "wakes" not in again
    assert wakes == [OWNER, WORKER]


def test_gate_actions_and_amend_are_lazy_callers_but_approve_is_not(project: Project) -> None:
    for action in ("decision-list", "amend"):
        decision_id = ask(project)
        expire(project, decision_id)
        extra = DELEGATION + OWNER_FLAGS if action == "amend" else ()
        run(project, action, *extra)
        assert row(project, decision_id)["status"] == "defaulted", action


def test_dispatch_error_reports_unknown_and_leaves_both_notices(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise OSError("host unavailable")

    monkeypatch.setattr("llm_bus.locks.notices.dispatch_pending", broken)
    decision_id = ask(project)
    expire(project, decision_id)
    shown = run(project, "decision-show", "--decision-id", decision_id)
    wakes = cast("list[dict[str, dict[str, str]]]", shown["wakes"])
    assert [item["wake"]["status"] for item in wakes] == ["unknown", "unknown"]
    assert notices(project, decision_id) == [OWNER, WORKER]
    with project.store.transaction() as db:
        pending = cast(
            "tuple[int]",
            db.execute(
                "SELECT COUNT(*) FROM messages WHERE body LIKE ? AND acknowledged_at IS NULL",
                (f"%{decision_id}%",),
            ).fetchone(),
        )
    assert pending == (2,)


def test_answering_before_the_deadline_prevents_the_default(
    project: Project, wakes: list[str]
) -> None:
    decision_id = ask(project)
    run(project, "decision-answer", "--decision-id", decision_id, "--answer", "Do it my way")
    expire(project, decision_id)
    decisions.run_decision_timer(project.store, decision_id, sleep=lambda _s: pytest.fail("slept"))
    stored = row(project, decision_id)
    assert (stored["status"], stored["answer"]) == ("answered", "Do it my way")
    assert notices(project, decision_id) == []
    assert wakes == []


def test_ineligible_classes_stay_pending_after_the_deadline(project: Project) -> None:
    ids = [
        ask(project, "--class", name, "--question", "Q?", "--recommended", "R")
        for name in ("security", "cost", "data", "irreversible", "objective")
    ]
    run(project, "show")
    assert {row(project, decision_id)["status"] for decision_id in ids} == {"pending"}


def test_coordinator_hook_applies_once_and_a_worker_hook_applies_nothing(
    project: Project, wakes: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = str(project.path.resolve())
    decision_id = ask(project)
    expire(project, decision_id)
    guidance(project.store, WORKER, folder, full=False)
    guidance(project.store, "codex:stranger", folder, full=False)
    assert row(project, decision_id)["status"] == "pending"
    assert wakes == []
    monkeypatch.setenv("LLM_BUS_DB", str(project.path / "bus.db"))
    monkeypatch.setattr("sys.stdin", prompt_input("owner", folder))
    assert main(["hook", "codex"]) == 0
    assert row(project, decision_id)["status"] == "defaulted"
    assert wakes == [OWNER, WORKER]
    decisions.run_decision_timer(project.store, decision_id, sleep=lambda _s: pytest.fail("slept"))
    guidance(project.store, OWNER, folder, full=False)
    assert notices(project, decision_id) == [OWNER, WORKER]
    assert wakes == [OWNER, WORKER]


def test_timer_finds_the_decision_from_another_working_directory(
    project: Project, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision_id = ask(project)
    monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))
    timer(project, decision_id)
    assert row(project, decision_id)["status"] == "defaulted"


def test_timer_returns_for_a_missing_or_settled_decision(project: Project) -> None:
    decisions.run_decision_timer(project.store, uuid4().hex, sleep=lambda _s: pytest.fail("slept"))
    decision_id = ask(project, "--class", "scope", "--question", "Q?", "--answer", "Yes")
    decisions.run_decision_timer(project.store, decision_id, sleep=lambda _s: pytest.fail("slept"))


def test_narrowed_worker_does_not_accept_a_default_but_accepts_an_answer(project: Project) -> None:
    invoke(
        project.store, project.path, "join", "demo", "--narrow", "owner-decisions", address=WORKER
    )
    defaulted = ask(project)
    timer(project, defaulted)
    answered = ask(project, "--class", "scope", "--question", "Q2?", "--answer", "Yes")
    refused = run(project, "decision-show", "--decision-id", defaulted, address=WORKER)
    assert (refused["accepted"], refused["reason"]) == (False, "default_not_bound")
    assert "answer" not in refused
    accepted = run(project, "decision-show", "--decision-id", answered, address=WORKER)
    assert accepted["accepted"] is True


def test_bound_worker_accepts_a_default_and_the_coordinator_always_does(project: Project) -> None:
    decision_id = ask(project)
    timer(project, decision_id)
    for address in (WORKER, OWNER):
        shown = run(project, "decision-show", "--decision-id", decision_id, address=address)
        assert shown["accepted"] is True
        assert shown["answer"] == "Reuse it"


def test_digest_lists_the_default_for_the_coordinator(project: Project) -> None:
    assert "decision_digest" not in run(project, "show")
    decision_id = ask(project)
    timer(project, decision_id)
    digest = cast("list[dict[str, object]]", run(project, "show")["decision_digest"])
    assert [item["decision_id"] for item in digest] == [decision_id]
    assert "decision_digest" not in run(project, "show", address=WORKER)
    ask(project, "--class", "scope", "--question", "Q2?", "--answer", "Yes")
    listed = run(project, "decision-list", "--defaulted")["decisions"]
    assert [item["decision_id"] for item in cast("list[dict[str, object]]", listed)] == [
        decision_id
    ]
    line = "1 decisions were taken by default; report them to the owner"
    assert line in guidance(project.store, OWNER, str(project.path.resolve()), full=False)
    assert line not in guidance(project.store, WORKER, str(project.path.resolve()), full=False)


@pytest.mark.parametrize("trigger", ["timer", "show", "hook"])
@pytest.mark.parametrize("change", ["amend-off", "replace", "revoke"])
def test_nothing_defaults_after_amend_replace_or_revoke(
    project: Project,
    wakes: list[str],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    trigger: str,
) -> None:
    decision_id = ask(project)
    if change == "amend-off":
        run(project, "amend", *DELEGATION, "--owner-decisions")
    elif change == "replace":
        run(project, "replace", "--generation", str(uuid4()), "--coordinator", OWNER)
    else:
        run(project, "revoke")
    before = len(wakes)
    expire(project, decision_id)
    folder = str(project.path.resolve())
    if trigger == "timer":
        decisions.run_decision_timer(
            project.store, decision_id, sleep=lambda _s: pytest.fail("slept")
        )
    elif trigger == "show":
        run(project, "decision-show", "--decision-id", decision_id)
    else:
        monkeypatch.setenv("LLM_BUS_DB", str(project.path / "bus.db"))
        monkeypatch.setattr("sys.stdin", prompt_input("owner", folder))
        assert main(["hook", "codex"]) == 0
    assert row(project, decision_id)["status"] == "pending"
    assert notices(project, decision_id) == []
    assert wakes[before:] == []


def test_readding_defaults_never_revives_an_old_row(project: Project) -> None:
    decision_id = ask(project)
    run(project, "amend", *DELEGATION, "--owner-decisions")
    expire(project, decision_id)
    run(project, "amend", *DELEGATION, *OWNER_FLAGS)
    assert row(project, decision_id)["status"] == "pending"
    assert row(project, decision_id)["deadline_at"] is None
    decisions.run_decision_timer(project.store, decision_id, sleep=lambda _s: pytest.fail("slept"))
    assert row(project, decision_id)["status"] == "pending"


def test_older_generation_rows_are_never_defaulted(project: Project) -> None:
    old = ask(project)
    run(project, "replace", "--generation", str(uuid4()), "--coordinator", OWNER)
    run(project, "amend", *DELEGATION, *OWNER_FLAGS)
    expire(project, old)
    run(project, "show")
    assert row(project, old)["status"] == "pending"


def test_resume_with_a_different_default_raises_and_amend_widening_needs_rejoin(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path, *DELEGATION, "--owner-decisions")
    with pytest.raises(BusError, match="Delegation differs"):
        invoke(project.store, tmp_path, "coordinate", "demo", *DELEGATION, *OWNER_FLAGS[:2], "45")
    amended = run(project, "amend", *DELEGATION, *OWNER_FLAGS)
    assert amended["rejoin_required"] == [WORKER]
    invoke(project.store, tmp_path, "join", "demo", address=WORKER)
    assert run(project, "amend", *DELEGATION, *OWNER_FLAGS[:2], "45")["rejoin_required"] == []


def test_resume_repeating_the_same_default_is_accepted(project: Project) -> None:
    resumed = invoke(project.store, project.path, "coordinate", "demo", *DELEGATION, *OWNER_FLAGS)
    assert resumed["delegation"]["decision_default_minutes"] == 30  # type: ignore[index]


@pytest.mark.parametrize("minutes", ["0", "1441", "-1"])
def test_default_minutes_outside_range_are_refused(project: Project, minutes: str) -> None:
    with pytest.raises(BusError, match="1 to 1440"):
        run(project, "amend", *DELEGATION, "--owner-decisions", "--decision-defaults", minutes)


def test_default_minutes_need_owner_decisions(project: Project) -> None:
    with pytest.raises(BusError, match="decision-defaults needs owner-decisions"):
        run(project, "amend", *DELEGATION, "--decision-defaults", "30")


def test_delegation_flag_round_trip_and_set_algebra() -> None:
    full = Delegation.from_specs(
        ["merge:develop"], owner_decisions=True, decision_default_minutes=30
    )
    assert full.items() == ("merge:develop", "owner-decisions", "decision-defaults")
    assert full.wire()["decision_default_minutes"] == 30
    assert Delegation.parse(full.wire()) == full
    plain = Delegation.from_specs(["merge:develop"], owner_decisions=True)
    assert plain.wire() == {
        "version": 1,
        "gates": {"merge": ["develop"]},
        "owner_decisions": True,
    }
    other = Delegation.from_specs(
        ["merge:develop"], owner_decisions=True, decision_default_minutes=60
    )
    assert full.intersect(other).decision_default_minutes == 30
    assert full.intersect(plain).decision_default_minutes is None
    assert plain.intersect(full).decision_default_minutes is None
    assert full.covers(other)
    assert not plain.covers(full)
    assert full.narrow(["merge:develop", "owner-decisions"]).decision_default_minutes is None
    assert full.narrow(list(full.items())) == full
    with pytest.raises(BusError, match="needs owner-decisions"):
        full.narrow(["decision-defaults"])
    for bad in (True, 0, 1441, "30", 1.5):
        with pytest.raises(BusError, match="Invalid delegation flag"):
            Delegation.parse({**full.wire(), "decision_default_minutes": bad})


def test_parser_accepts_the_timer_action_and_the_flags() -> None:
    parser = build_parser()
    parsed = vars(
        parser.parse_args(["coordination", "decision-timer", "demo", "--decision-id", "x"])
    )
    assert parsed["decision_id"] == "x"
    amended = vars(parser.parse_args(["coordination", "amend", "demo", "--decision-defaults", "5"]))
    assert amended["decision_default_minutes"] == 5
    listed = vars(parser.parse_args(["coordination", "decision-list", "demo", "--defaulted"]))
    assert listed["defaulted"] is True


def test_profile_lists_the_decision_flags(tmp_path: Path) -> None:
    out = setup.profile(str(tmp_path))
    flags = cast("list[str]", out["flags"])
    assert flags[-2:] == ["owner-decisions", "decision-defaults"]
    assert str(out["command"]).endswith("--owner-decisions --decision-defaults 5")


def test_multiline_decision_text_is_allowed_and_other_controls_refused(project: Project) -> None:
    text = "First line\n\tsecond line"
    decision_id = ask(
        project, "--class", "scope", "--question", text, "--recommended", text, "--answer", text
    )
    assert row(project, decision_id)["answer"] == text
    for char in (chr(13), chr(0), chr(27), chr(0x202E), chr(0x200B)):
        with pytest.raises(BusError, match="control or format"):
            ask(project, "--class", "scope", "--question", f"a{char}b")
    for mark in ("<" * 3, ">" * 3):
        with pytest.raises(BusError, match="must not contain"):
            ask(project, "--class", "scope", "--question", f"a{mark}b")
    with pytest.raises(BusError, match="1 to 4096"):
        ask(project, "--class", "scope", "--question", "x" * 4097)


def test_multiline_objective_answer_renders_as_one_escaped_line(project: Project) -> None:
    objective = "Aim one\nAim two\tdone"
    ask(project, "--class", "objective", "--question", "Aim?", "--answer", objective)
    shown = run(project, "show")
    assert shown["effective_objective"] == objective
    assert json.loads(serialize(shown, "coordination"))["effective_objective"] == objective
    for output_format in ("json", "agent"):
        assert "\n" not in serialize(shown, "coordination", output_format)
    full = guidance(project.store, OWNER, str(project.path.resolve()), full=True)
    assert "Aim two" not in full


def test_a_defaulted_decision_cannot_be_answered_afterwards(project: Project) -> None:
    decision_id = ask(project)
    timer(project, decision_id)
    with pytest.raises(BusError, match="not_pending"):
        run(project, "decision-answer", "--decision-id", decision_id, "--answer", "Actually no")
    assert row(project, decision_id)["status"] == "defaulted"
