"""Owner decision records, the owner-decisions delegation flag and the D24 non-empty rule."""

# Each scenario states its owner commands explicitly.
# pylint: disable=duplicate-code,clean-code-commented-out-code

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from llm_bus.cli.app import main
from llm_bus.cli.guidance import guidance
from llm_bus.cli.parser import build_parser
from llm_bus.coordination import decisions, setup
from llm_bus.coordination.delegation import Delegation
from llm_bus.errors import BusError
from tests.coordination.test_approvals import DELEGATION, Project
from tests.coordination.test_enrollment import invoke
from tests.support import prompt_input

if TYPE_CHECKING:
    from pathlib import Path

OWNER = "codex:owner"
WORKER = "claude:worker"
FLAG = "--owner-decisions"
ANSWER = "Use the existing login form; keep the logout button."
PROVENANCE = ("answer", "answer_sha256", "source_session", "source_turn", "resolved_at")


@pytest.fixture(autouse=True)
def no_lock_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_timer(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("llm_bus.locks.service.schedule_check", no_timer)


def run(project: Project, action: str, *argv: str, address: str = OWNER) -> dict[str, object]:
    return invoke(
        project.store, project.path, "coordination", action, "demo", *argv, address=address
    )


def ask(project: Project, *argv: str, address: str = OWNER) -> dict[str, object]:
    options = argv or ("--class", "scope", "--question", "Which form?")
    return run(project, "decision-ask", *options, address=address)


def show(project: Project, decision_id: object, address: str = WORKER) -> dict[str, object]:
    return run(project, "decision-show", "--decision-id", str(decision_id), address=address)


def rows(project: Project) -> list[dict[str, object]]:
    with project.store.transaction() as db:
        cursor = db.execute("SELECT * FROM coordination_decisions ORDER BY rowid")
        return [
            dict(zip((c[0] for c in cursor.description), row, strict=True))
            for row in cast("list[tuple[object, ...]]", cursor.fetchall())
        ]


def turns(project: Project) -> list[tuple[str, int]]:
    with project.store.transaction() as db:
        return cast(
            "list[tuple[str, int]]",
            db.execute("SELECT address,turn FROM coordination_owner_turns").fetchall(),
        )


def bindings(project: Project) -> list[list[str]]:
    shown = run(project, "show")
    return list(cast("dict[str, list[str]]", shown["bindings"]).values())


@pytest.fixture(name="project")
def project_fixture(tmp_path: Path) -> Project:
    return Project(tmp_path, *DELEGATION, FLAG)


def test_ask_stores_a_pending_row(project: Project) -> None:
    result = ask(
        project,
        "--class",
        "scope",
        "--question",
        "Which form?",
        "--recommended",
        "Reuse it",
        "--ticket",
        "roam-22m",
        "--worker",
        WORKER,
    )
    assert result["status"] == "pending"
    assert "wakes" not in result
    (row,) = rows(project)
    assert row["decision_id"] == result["decision_id"]
    assert (row["class"], row["status"], row["worker"], row["ticket"]) == (
        "scope",
        "pending",
        WORKER,
        "roam-22m",
    )
    assert (row["recommended"], row["answer"], row["deadline_at"]) == ("Reuse it", None, None)


def test_answer_stores_verbatim_text_with_provenance(project: Project) -> None:
    decisions.record_turn(project.store, OWNER)
    decisions.record_turn(project.store, OWNER)
    decision_id = ask(project)["decision_id"]
    result = run(project, "decision-answer", "--decision-id", str(decision_id), "--answer", ANSWER)
    assert result == {"decision_id": decision_id, "status": "answered"}
    (row,) = rows(project)
    assert row["status"] == "answered"
    assert row["answer"] == ANSWER
    assert row["answer_sha256"] == hashlib.sha256(ANSWER.encode()).hexdigest()
    assert (row["source_session"], row["source_turn"]) == (OWNER, 2)
    assert row["resolved_at"]


def test_ask_with_answer_stores_the_same_provenance_fields(project: Project) -> None:
    asked = ask(project, "--class", "scope", "--question", "Q1", "--answer", ANSWER)
    assert asked["status"] == "answered"
    second = ask(project, "--class", "scope", "--question", "Q2")["decision_id"]
    run(project, "decision-answer", "--decision-id", str(second), "--answer", ANSWER)
    direct, later = rows(project)
    for name in PROVENANCE:
        assert (direct[name] is None) == (later[name] is None), name
    assert direct["answer"] == later["answer"]
    assert direct["answer_sha256"] == later["answer_sha256"]
    assert direct["source_session"] == later["source_session"] == OWNER
    assert direct["source_turn"] is None


def test_objective_class_appends_an_amendment_and_scope_class_does_not(project: Project) -> None:
    scope_id = ask(project, "--class", "scope", "--question", "A?", "--answer", "Yes")[
        "decision_id"
    ]
    assert "amendments" not in run(project, "show")
    objective = ask(project, "--class", "objective", "--question", "Aim?", "--answer", "New aim")
    shown = run(project, "show")
    (amendment,) = cast("list[dict[str, object]]", shown["amendments"])
    assert amendment["text"] == "New aim"
    assert amendment["decision_id"] == objective["decision_id"] != scope_id
    assert shown["effective_objective"] == "New aim"
    pending = ask(project, "--class", "objective", "--question", "Again?")["decision_id"]
    assert len(cast("list[object]", run(project, "show")["amendments"])) == 1
    run(project, "decision-answer", "--decision-id", str(pending), "--answer", "Newer aim")
    assert run(project, "show")["effective_objective"] == "Newer aim"


def test_parser_has_no_kind_flag_and_no_record_action() -> None:
    parser = build_parser()
    base = ["coordination", "decision-ask", "demo", "--question", "Q?"]
    for argv in (
        [*base, "--class", "scope", "--kind", "scope"],
        ["coordination", "decision-record", "demo"],
        [*base, "--class", "nonsense"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_worker_must_be_an_enrolled_worker(project: Project) -> None:
    for worker in ("claude:ghost", OWNER):
        with pytest.raises(BusError, match="enrolled worker"):
            ask(project, "--class", "scope", "--question", "Q?", "--worker", worker)
    assert rows(project) == []


def test_only_the_coordinator_records_and_lists(project: Project) -> None:
    decision_id = ask(project)["decision_id"]
    for argv in (
        ("decision-ask", "--class", "scope", "--question", "Q?"),
        ("decision-answer", "--decision-id", str(decision_id), "--answer", ANSWER),
        ("decision-list",),
    ):
        with pytest.raises(BusError, match="Only the enrolled coordinator"):
            run(project, *argv, address=WORKER)
    assert rows(project)[0]["status"] == "pending"
    listed = cast("list[dict[str, object]]", run(project, "decision-list")["decisions"])
    assert [item["decision_id"] for item in listed] == [decision_id]


def test_answer_is_rejected_unless_pending_and_known(project: Project) -> None:
    answered = str(
        ask(project, "--class", "scope", "--question", "Q?", "--answer", "Yes")["decision_id"]
    )
    with pytest.raises(BusError, match="not_pending"):
        run(project, "decision-answer", "--decision-id", answered, "--answer", "Again")
    with pytest.raises(BusError, match="no_decision"):
        run(project, "decision-answer", "--decision-id", uuid4().hex, "--answer", "Yes")
    with pytest.raises(BusError, match="Decision not found"):
        show(project, uuid4().hex)


def test_acceptance_matrix(project: Project) -> None:
    decision_id = str(ask(project, "--class", "scope", "--question", "Q?")["decision_id"])
    pending = show(project, decision_id)
    assert pending == {"decision_id": decision_id, "accepted": False, "reason": "not_answered"}
    run(project, "decision-answer", "--decision-id", decision_id, "--answer", ANSWER)
    worker = show(project, decision_id)
    assert worker["accepted"] is True
    assert worker["answer"] == ANSWER
    assert "reason" not in worker
    assert show(project, decision_id, OWNER)["accepted"] is True


def test_worker_without_the_bound_item_cannot_accept(tmp_path: Path) -> None:
    project = Project(tmp_path, *DELEGATION, FLAG)
    decision_id = str(
        ask(project, "--class", "scope", "--question", "Q?", "--answer", "Yes")["decision_id"]
    )
    invoke(project.store, tmp_path, "join", "demo", "--narrow", "merge:develop", address=WORKER)
    denied = show(project, decision_id)
    assert denied == {"decision_id": decision_id, "accepted": False, "reason": "not_delegated"}
    assert "answer" not in denied


def test_decision_naming_another_worker_is_a_wrong_session(project: Project) -> None:
    decision_id = str(
        ask(project, "--class", "scope", "--question", "Q?", "--answer", "Yes")["decision_id"]
    )
    other = "claude:other"
    project.store.register("claude", "other", str(project.path))
    invoke(project.store, project.path, "join", "demo", address=other)
    with project.store.transaction() as db:
        db.execute("UPDATE coordination_decisions SET worker=?", (other,))
    assert show(project, decision_id)["reason"] == "wrong_session"
    assert show(project, decision_id, other)["accepted"] is True


def test_stale_generation_is_not_accepted(project: Project) -> None:
    decision_id = str(
        ask(project, "--class", "scope", "--question", "Q?", "--answer", "Yes")["decision_id"]
    )
    run(project, "replace", "--generation", str(uuid4()), "--coordinator", OWNER)
    assert show(project, decision_id, OWNER)["reason"] == "stale_generation"
    with pytest.raises(BusError, match="stale_generation"):
        run(project, "decision-answer", "--decision-id", decision_id, "--answer", "Again")


def test_answer_text_is_validated_and_nothing_is_stored(project: Project) -> None:
    bad = {
        "control": f"Use{chr(7)}it",
        "format": f"Use{chr(0x200B)}it",
        "open": "see <<< here",
        "close": "see >>> here",
        "blank": "   ",
        "long": "x" * 4097,
    }
    for text in bad.values():
        with pytest.raises(BusError, match="Decision answer"):
            ask(project, "--class", "scope", "--question", "Q?", "--answer", text)
    pending = str(ask(project)["decision_id"])
    for text in bad.values():
        with pytest.raises(BusError, match="Decision answer"):
            run(project, "decision-answer", "--decision-id", pending, "--answer", text)
    with pytest.raises(BusError, match="Decision question"):
        ask(project, "--class", "scope", "--question", f"Q{chr(7)}")
    assert [row["status"] for row in rows(project)] == ["pending"]
    assert "amendments" not in run(project, "show")


def test_rejected_objective_answer_stores_no_amendment(project: Project) -> None:
    with pytest.raises(BusError, match="Decision answer"):
        ask(project, "--class", "objective", "--question", "Aim?", "--answer", "a >>> b")
    assert rows(project) == []
    assert "amendments" not in run(project, "show")


def test_turn_counter_ignores_unregistered_addresses(project: Project) -> None:
    for address in ("codex:stranger", "claude:nobody"):
        decisions.record_turn(project.store, address)
    assert turns(project) == []
    decisions.record_turn(project.store, OWNER)
    decisions.record_turn(project.store, OWNER)
    assert turns(project) == [(OWNER, 2)]


def test_prompt_hook_tracks_each_registered_main_session(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LLM_BUS_DB", str(project.path / "bus.db"))
    folder = str(project.path.resolve())
    for kind, session in (("claude", "worker"), ("codex", "stranger"), ("codex", "owner")):
        monkeypatch.setattr("sys.stdin", prompt_input(session, folder))
        assert main(["hook", kind]) == 0
        assert capsys.readouterr().out
    assert dict(turns(project)) == {WORKER: 1, "codex:stranger": 1, OWNER: 1}
    monkeypatch.setattr(
        "sys.stdin",
        prompt_input("owner", folder),
    )
    assert main(["hook", "codex"]) == 0
    assert dict(turns(project)) == {WORKER: 1, "codex:stranger": 1, OWNER: 2}
    answered = str(
        ask(project, "--class", "scope", "--question", "Q?", "--answer", "Yes")["decision_id"]
    )
    assert rows(project)[0]["source_turn"] == 2
    assert show(project, answered)["accepted"] is True


def test_amend_adding_the_flag_needs_a_rejoin(tmp_path: Path) -> None:
    project = Project(tmp_path, *DELEGATION)
    decision_id = str(
        ask(project, "--class", "scope", "--question", "Q?", "--answer", "Yes")["decision_id"]
    )
    assert show(project, decision_id)["reason"] == "not_delegated"
    amended = run(project, "amend", *DELEGATION, FLAG)
    assert amended["rejoin_required"] == [WORKER]
    assert show(project, decision_id)["reason"] == "not_delegated"
    invoke(project.store, tmp_path, "join", "demo", address=WORKER)
    assert show(project, decision_id)["accepted"] is True


def test_resume_with_a_differing_flag_raises(tmp_path: Path) -> None:
    project = Project(tmp_path, *DELEGATION)
    with pytest.raises(BusError, match="Delegation differs; use coordination amend PROJECT"):
        project.owner("coordinate", "demo", *DELEGATION, FLAG)
    assert project.owner("coordinate", "demo", *DELEGATION)["delegation"]


def test_resume_with_the_flag_alone_compares_against_the_stored_delegation(tmp_path: Path) -> None:
    project = Project(tmp_path, FLAG)
    assert project.owner("coordinate", "demo", FLAG)["delegation"]
    with pytest.raises(BusError, match="Delegation differs"):
        project.owner("coordinate", "demo", *DELEGATION, FLAG)


def test_delegation_flag_mechanics() -> None:
    plain = Delegation.parse({"version": 1, "gates": {"merge": ["develop"]}})
    flagged = Delegation.from_specs(["merge:develop"], owner_decisions=True)
    assert plain.owner_decisions is False
    assert "owner_decisions" not in plain.wire()
    assert flagged.items() == ("merge:develop", "owner-decisions")
    assert Delegation.parse(flagged.wire()) == flagged
    assert flagged.intersect(plain).owner_decisions is False
    assert flagged.covers(plain)
    assert not plain.covers(flagged)
    assert flagged.narrow(["merge:develop"]).owner_decisions is False
    assert flagged.narrow(["owner-decisions"]).items() == ("owner-decisions",)
    both = Delegation.from_specs(["merge:develop"], auto_merge=True, owner_decisions=True)
    assert both.items() == ("merge:develop", "auto-merge", "owner-decisions")
    with pytest.raises(BusError, match="Invalid delegation flag"):
        Delegation.parse({"version": 1, "gates": {}, "owner_decisions": 1})
    with pytest.raises(BusError, match="Not delegated by the project"):
        plain.narrow(["owner-decisions"])


def test_decisions_only_delegation_is_valid(tmp_path: Path) -> None:
    project = Project(tmp_path, FLAG)
    shown = project.owner("coordination", "show", "demo")
    assert shown["delegation"] == {"version": 1, "gates": {}, "owner_decisions": True}
    assert list(cast("dict[str, list[str]]", shown["bindings"]).values()) == [["owner-decisions"]]
    only = Delegation.from_specs([], owner_decisions=True)
    assert only.items() == ("owner-decisions",)
    assert only.wire()["gates"] == {}
    assert Delegation.parse(only.wire()) == only
    invalid: tuple[dict[str, object], ...] = (
        {"version": 1, "gates": {}},
        {"version": 1, "gates": {}, "auto_merge": False},
        {"version": 1, "gates": []},
    )
    for bad in invalid:
        with pytest.raises(BusError, match="Invalid delegation gates"):
            Delegation.parse(bad)
    with pytest.raises(BusError, match="at least one GATE:BRANCH or --owner-decisions"):
        Delegation.from_specs([])
    with pytest.raises(BusError, match="auto-merge needs a merge or merge-admin gate"):
        Delegation.from_specs([], auto_merge=True, owner_decisions=True)


def test_join_narrow_binds_the_flag_alone_and_denies_every_approval(tmp_path: Path) -> None:
    project = Project(tmp_path, *DELEGATION, FLAG)
    invoke(project.store, tmp_path, "join", "demo", "--narrow", "owner-decisions", address=WORKER)
    assert bindings(project) == [["owner-decisions"]]
    decision_id = str(
        ask(project, "--class", "scope", "--question", "Q?", "--answer", "Yes")["decision_id"]
    )
    assert show(project, decision_id)["accepted"] is True
    for gate in ("merge", "merge-admin"):
        with pytest.raises(BusError, match="Direction denied: gate_not_delegated"):
            project.approve("--gate", gate)


def test_decisions_only_guidance_for_both_roles(tmp_path: Path) -> None:
    project = Project(tmp_path, FLAG)
    worker = guidance(project.store, WORKER, str(tmp_path), full=True)
    assert "Owner decisions via the coordinator: accept a decision only after" in worker
    assert "decision-show reports accepted: true" in worker
    assert "Delegated gates" not in worker
    coordinator = guidance(project.store, OWNER, str(tmp_path), full=True)
    assert "Delegated items: owner-decisions; workers whose binding is narrower: 0." in coordinator
    assert "Delegated gates" not in coordinator


def test_gate_guidance_is_unchanged_without_the_flag(tmp_path: Path) -> None:
    project = Project(tmp_path, *DELEGATION)
    worker = guidance(project.store, WORKER, str(tmp_path), full=True)
    assert "Delegated gates bound by your owner's join" in worker
    assert "Owner decisions" not in worker
    coordinator = guidance(project.store, OWNER, str(tmp_path), full=True)
    assert "Delegated gates: " in coordinator
    assert "Delegated items" not in coordinator


def test_dropping_gates_keeps_the_binding_with_the_flag(tmp_path: Path) -> None:
    project = Project(tmp_path, "--delegate", "merge:develop", FLAG)
    assert bindings(project) == [["merge:develop", "owner-decisions"]]
    amended = run(project, "amend", FLAG)
    assert amended["delegation"] == {"version": 1, "gates": {}, "owner_decisions": True}
    assert amended["rejoin_required"] == []
    assert bindings(project) == [["owner-decisions"]]
    run(project, "amend", "--no-delegation")
    assert "bindings" not in run(project, "show")
    with pytest.raises(BusError, match="--no-delegation takes no delegation flags"):
        run(project, "amend", "--no-delegation", FLAG)


def test_wizard_with_every_gate_disabled_stores_decisions_only(tmp_path: Path) -> None:
    store = Project(tmp_path, claim=False).store
    store.register("codex", "wiz", str(tmp_path))
    replies = iter(
        [
            "guided",
            "Ship",
            str(tmp_path),
            "bus-2",
            "develop",
            "no",
            "no",
            "no",
            "no",
            "",
            "no",
            "",
            "",
            "yes",
        ]
    )
    prompts: list[str] = []

    def ask_next(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    said: list[str] = []
    result = setup.run_wizard(
        store, "wiz", str(tmp_path), "codex:wiz", ask=ask_next, say=said.append
    )
    assert result is not None
    assert any("owner decisions" in prompt for prompt in prompts)
    assert any("Delegations: owner-decisions" in line for line in said)
    with store.transaction() as db:
        (text,) = cast(
            "tuple[str]",
            db.execute(
                "SELECT d.delegation_json FROM coordination_delegations d "
                "JOIN coordination_projects p ON p.project_id=d.project_id WHERE p.label='wiz'"
            ).fetchone(),
        )
    assert json.loads(text) == {"version": 1, "gates": {}, "owner_decisions": True}


def test_wizard_declining_decisions_with_no_gate_stores_nothing_new(tmp_path: Path) -> None:
    store = Project(tmp_path, claim=False).store
    store.register("codex", "wiz", str(tmp_path))
    replies = iter(
        [
            "guided",
            "Ship",
            str(tmp_path),
            "bus-2",
            "develop",
            "no",
            "no",
            "no",
            "no",
            "no",
            "",
            "",
            "yes",
        ]
    )
    result = setup.run_wizard(
        store, "wiz", str(tmp_path), "codex:wiz", ask=lambda _p: next(replies), say=lambda _l: None
    )
    assert result is not None
    with store.transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM coordination_delegations d "
                "JOIN coordination_projects p ON p.project_id=d.project_id WHERE p.label='wiz'"
            ).fetchone()[0]
            == 0
        )


def test_legacy_project_has_no_decision_state(tmp_path: Path) -> None:
    project = Project(tmp_path)
    shown = project.owner("coordination", "show", "demo")
    assert "delegation" not in shown
    assert "decisions" not in shown
    assert rows(project) == []
    decision_id = str(ask(project)["decision_id"])
    assert show(project, decision_id, OWNER)["accepted"] is False
