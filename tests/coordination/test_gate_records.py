"""Merge gate records: submit, coordinator accept or reject, record-bound approvals, auto-merge."""

# Each scenario states its owner commands and stored rows explicitly.
# pylint: disable=duplicate-code,protected-access,clean-code-commented-out-code

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from llm_bus.cli import commands
from llm_bus.cli.parser import build_parser
from llm_bus.coordination import delegation, delivery, setup
from llm_bus.coordination.delegation import Delegation
from llm_bus.coordination.models import Approval
from llm_bus.errors import BusError
from tests.coordination.test_approvals import APPROVE, DELEGATION, SHA, Project
from tests.coordination.test_enrollment import invoke

if TYPE_CHECKING:
    from pathlib import Path

    from llm_bus.mailbox.store import Store

WORKER = "claude:worker"
OWNER = "codex:owner"
AUTO = (*DELEGATION, "--auto-merge")
NARROW_ALL = ("--narrow", "merge:develop", "--narrow", "merge-admin:develop")


@pytest.fixture(name="wakes", autouse=True)
def recorded_wakes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record wake recipients; no native host wake and no lock timer ever runs."""
    seen: list[str] = []

    def record(_store: object, recipient: str, **_kwargs: object) -> dict[str, str]:
        seen.append(recipient)
        return {"status": "unsupported"}

    monkeypatch.setattr("llm_bus.locks.notices.dispatch_pending", record)
    monkeypatch.setattr(commands, "dispatch_pending", record)

    def no_timer(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("llm_bus.locks.service.schedule_check", no_timer)
    return seen


def submit(
    project: Project,
    *,
    address: str = WORKER,
    evidence: tuple[str, ...] = ("review-1",),
    **fields: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "ticket": "roam-22m",
        "branch": "roam-22m-login",
        "base": "develop",
        "pr": 42,
        "head_sha": SHA,
        **fields,
    }
    argv = ["coordination", "gate-record", "demo"]
    for key, value in values.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    for item in evidence:
        argv += ["--review-evidence", item]
    argv += ["--required-check", "ci"]
    return invoke(project.store, project.path, *argv, address=address)


def accept(
    project: Project, record_id: object, *extra: str, sha: str = SHA, address: str = OWNER
) -> dict[str, object]:
    return invoke(
        project.store,
        project.path,
        "coordination",
        "gate-accept",
        "demo",
        "--record-id",
        str(record_id),
        "--head-sha",
        sha,
        *extra,
        address=address,
    )


def reject(project: Project, record_id: object, address: str = OWNER) -> dict[str, object]:
    return invoke(
        project.store,
        project.path,
        "coordination",
        "gate-reject",
        "demo",
        "--record-id",
        str(record_id),
        address=address,
    )


def status(project: Project, record_id: object) -> str:
    with project.store.transaction() as db:
        return cast(
            "str",
            db.execute(
                "SELECT status FROM coordination_gate_records WHERE record_id=?", (record_id,)
            ).fetchone()[0],
        )


def verification(project: Project, message_id: object) -> dict[str, object]:
    with project.store.transaction() as db:
        found = delivery.verification(db, cast("int", message_id))
    assert found is not None
    return found


def grant_effective(store: Store) -> Delegation | None:
    with store.transaction() as db:
        row = cast(
            "tuple[str, str, str]",
            db.execute("SELECT scope,project_id,generation FROM coordination_projects").fetchone(),
        )
        record: dict[str, object] = dict(
            zip(("scope", "project_id", "generation"), row, strict=True)
        )
        (grant_id,) = cast(
            "tuple[str]", db.execute("SELECT grant_id FROM coordination_grants").fetchone()
        )
        return delegation.effective(db, record, grant_id, now=datetime.now(UTC))


@pytest.mark.parametrize(
    ("extra", "operation"), [((), "approve-merge"), (("--admin",), "approve-merge-admin")]
)
def test_accept_sends_verified_record_bound_approval(
    tmp_path: Path, extra: tuple[str, ...], operation: str
) -> None:
    project = Project(tmp_path, *AUTO)
    submitted = submit(project)
    assert submitted["status"] == "submitted"
    assert submitted["superseded"] == []
    done = accept(project, submitted["record_id"], *extra)
    assert done["status"] == "accepted"
    assert done["operation"] == operation
    shown = project.coordination()
    assert shown is not None
    assert shown["verified"] is True
    assert shown["operation"] == operation
    approval = cast("dict[str, object]", shown["approval"])
    assert approval["record_id"] == submitted["record_id"]
    assert approval["head_sha"] == SHA
    assert approval["pr"] == 42


def test_accept_returns_its_own_result_not_the_project(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    done = accept(project, submit(project)["record_id"])
    assert set(done) >= {"record_id", "status", "message_id", "operation", "expires_at", "wakes"}
    assert "delegation" not in done
    assert "grants" not in done


def test_wakes_follow_each_action(tmp_path: Path, wakes: list[str]) -> None:
    project = Project(tmp_path, *AUTO)
    before = len(wakes)
    first = submit(project)
    assert wakes[before:] == [OWNER]
    assert cast("list[dict[str, object]]", first["wakes"])[0]["to"] == OWNER
    before = len(wakes)
    accept(project, first["record_id"])
    assert wakes[before:] == [WORKER]
    before = len(wakes)
    reject(project, first["record_id"])
    assert wakes[before:] == [WORKER]


def test_coordinator_cannot_submit_and_worker_cannot_accept(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    with pytest.raises(BusError, match="Only the worker that claims the branch"):
        submit(project, address=OWNER)
    record_id = submit(project)["record_id"]
    with pytest.raises(BusError, match="not_coordinator"):
        accept(project, record_id, address=WORKER)
    with pytest.raises(BusError, match="not_coordinator"):
        reject(project, record_id, address=WORKER)
    assert status(project, record_id) == "submitted"


def test_accept_with_wrong_sha(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    record_id = submit(project)["record_id"]
    with pytest.raises(BusError, match="sha_mismatch"):
        accept(project, record_id, sha="b" * 40)
    assert status(project, record_id) == "submitted"


def test_accept_after_deadline_is_expired(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    record_id = submit(project)["record_id"]
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="seconds")
    with project.store.transaction() as db:
        db.execute("UPDATE coordination_gate_records SET expires_at=?", (past,))
    with pytest.raises(BusError, match="expired"):
        accept(project, record_id)


def test_accept_unknown_record_and_after_replace(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    record_id = submit(project)["record_id"]
    with pytest.raises(BusError, match="no_record"):
        accept(project, "0" * 32)
    project.owner(
        "coordination", "replace", "demo", "--generation", str(uuid4()), "--coordinator", OWNER
    )
    with pytest.raises(BusError, match="stale_generation"):
        accept(project, record_id)


def test_accept_twice_is_not_submitted(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    record_id = submit(project)["record_id"]
    accept(project, record_id)
    with pytest.raises(BusError, match="not_submitted"):
        accept(project, record_id)


def test_accept_after_delegation_drops_auto_merge(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    record_id = submit(project)["record_id"]
    project.owner(
        "coordination",
        "amend",
        "demo",
        "--delegate",
        "merge:develop",
        "--delegate",
        "merge-admin:develop",
    )
    with pytest.raises(BusError, match="gate_not_delegated"):
        accept(project, record_id)
    assert status(project, record_id) == "submitted"


def test_accept_after_worker_narrows_away_auto_merge(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    record_id = submit(project)["record_id"]
    invoke(project.store, tmp_path, "join", "demo", *NARROW_ALL, address=WORKER)
    with pytest.raises(BusError, match="gate_not_delegated"):
        accept(project, record_id)


def test_worker_without_auto_merge_cannot_submit(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    invoke(project.store, tmp_path, "join", "demo", *NARROW_ALL, address=WORKER)
    with pytest.raises(BusError, match="gate_not_delegated"):
        submit(project)
    assert project.rows("coordination_gate_records") == 0


def test_submit_needs_the_claim(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    with pytest.raises(BusError, match="wrong_ticket"):
        submit(project, branch="other-branch")


def test_reclaim_after_submit_denies_accept_and_keeps_the_row(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    record_id = submit(project)["record_id"]
    project.claim(WORKER, "other-1", "other-branch")
    before = project.rows("messages")
    with pytest.raises(BusError, match="Direction denied: wrong_ticket"):
        accept(project, record_id)
    assert status(project, record_id) == "submitted"
    assert project.rows("messages") == before


def test_admin_needs_a_merge_admin_gate(tmp_path: Path) -> None:
    project = Project(
        tmp_path, "--delegate", "commit:develop", "--delegate", "merge:develop", "--auto-merge"
    )
    record_id = submit(project)["record_id"]
    with pytest.raises(BusError, match="Direction denied: gate_not_delegated"):
        accept(project, record_id, "--admin")
    assert status(project, record_id) == "submitted"
    assert accept(project, record_id)["operation"] == "approve-merge"


def test_reject_hides_the_sent_approval(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    record_id = submit(project)["record_id"]
    message_id = accept(project, record_id)["message_id"]
    assert verification(project, message_id)["verified"] is True
    assert reject(project, record_id)["status"] == "rejected"
    withdrawn = verification(project, message_id)
    assert withdrawn["verified"] is False
    assert withdrawn["reason"] == "record_withdrawn"
    assert project.coordination() is None
    assert any("was rejected" in item["body"] for item in project.store.inbox(WORKER))
    with pytest.raises(BusError, match="not_submitted"):
        reject(project, record_id)


def test_resubmission_supersedes_the_accepted_record(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    first = submit(project)["record_id"]
    message_id = accept(project, first)["message_id"]
    second = submit(project, head_sha="c" * 40)
    assert second["superseded"] == [first]
    assert status(project, first) == "superseded"
    assert verification(project, message_id)["reason"] == "record_withdrawn"
    assert project.coordination() is None
    with pytest.raises(BusError, match="not_submitted"):
        accept(project, first)


def test_other_worker_cannot_withdraw_a_pr_it_does_not_own(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    first = submit(project)["record_id"]
    message_id = accept(project, first)["message_id"]
    project.store.register("claude", "other", str(tmp_path))
    invoke(project.store, tmp_path, "join", "demo", address="claude:other")
    project.claim("claude:other", "roam-99x", "roam-99x-api")
    submit(project, address="claude:other", ticket="roam-99x", branch="roam-99x-api")
    assert status(project, first) == "accepted"
    assert verification(project, message_id)["verified"] is True


def test_unknown_record_id_is_withdrawn_and_plain_approvals_are_unaffected(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    forged = Approval(
        "roam-22m",
        "roam-22m-login",
        "develop",
        (datetime.now(UTC) + timedelta(hours=1)).isoformat(timespec="seconds"),
        42,
        SHA,
        record_id="0" * 32,
    )
    assert project.authorize(forged, "approve-merge") == "record_withdrawn"
    project.approve()
    shown = project.coordination()
    assert shown is not None
    assert shown["verified"] is True
    assert "record_id" not in cast("dict[str, object]", shown["approval"])


def test_approve_command_has_no_record_flag() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([*APPROVE, "--record-id", "0" * 32])
    parser.parse_args(APPROVE)


@pytest.mark.parametrize("record_id", ["", "A" * 32, "0" * 31, "g" * 32])
def test_record_id_format(record_id: str) -> None:
    approval = Approval(
        "t", "b", "develop", "2030-01-01T00:00:00+00:00", 1, SHA, record_id=record_id
    )
    with pytest.raises(BusError, match="record_id"):
        approval.validate("approve-merge")
    with pytest.raises(BusError, match="record_id"):
        Approval("t", "b", "develop", "2030-01-01T00:00:00+00:00", None, SHA, "0" * 32).validate(
            "approve-push"
        )


def test_submit_validates_fields(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    with pytest.raises(BusError):
        submit(project, head_sha="xyz")
    with pytest.raises(BusError, match="1 to 8"):
        submit(project, evidence=tuple(f"r{n}" for n in range(9)))
    with pytest.raises(BusError, match="Invalid gate record"):
        submit(project, evidence=("bad\x07",))
    assert project.rows("coordination_gate_records") == 0


def test_notices_are_plain_text_and_show_lists_records(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    record_id = submit(project)["record_id"]
    notice = next(item for item in project.store.inbox(OWNER) if "Gate record" in item["body"])
    assert str(record_id) in notice["body"]
    assert "coordination" not in notice
    shown = project.owner("coordination", "show", "demo")
    (listed,) = cast("list[dict[str, object]]", shown["gate_records"])
    assert listed["record_id"] == record_id
    assert listed["status"] == "submitted"
    assert listed["submitted_by"] == WORKER


def test_show_gives_the_coordinator_the_fields_to_verify(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    record_id = submit(project, evidence=("review-1", "https://example.test/r/2"))["record_id"]
    shown = project.owner("coordination", "show", "demo")
    (listed,) = cast("list[dict[str, object]]", shown["gate_records"])
    assert listed["record_id"] == record_id
    assert listed["ticket"] == "roam-22m"
    assert listed["branch"] == "roam-22m-login"
    assert listed["base"] == "develop"
    assert listed["pr"] == 42
    assert listed["head_sha"] == SHA
    assert listed["review_evidence"] == ["review-1", "https://example.test/r/2"]
    assert listed["required_checks"] == ["ci"]
    assert listed["submitted_at"]
    assert "evidence_json" not in listed
    assert "checks_json" not in listed


def test_legacy_project_is_unchanged(tmp_path: Path) -> None:
    project = Project(tmp_path, claim=False)
    shown = project.owner("coordination", "show", "demo")
    assert "gate_records" not in shown
    assert "delegation" not in shown
    assert "wakes" not in shown


def test_amend_adding_auto_merge_needs_a_rejoin(tmp_path: Path) -> None:
    project = Project(tmp_path, *DELEGATION)
    shown = project.owner("coordination", "amend", "demo", *DELEGATION, "--auto-merge")
    assert shown["rejoin_required"] == [WORKER]
    bound = grant_effective(project.store)
    assert bound is not None
    assert bound.auto_merge is False
    invoke(project.store, tmp_path, "join", "demo", address=WORKER)
    rejoined = grant_effective(project.store)
    assert rejoined is not None
    assert rejoined.auto_merge is True


def test_amend_without_flag_removes_it_and_flag_alone_is_rejected(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    shown = project.owner("coordination", "amend", "demo", "--delegate", "merge:develop")
    assert "auto_merge" not in cast("dict[str, object]", shown["delegation"])
    with pytest.raises(BusError, match="auto-merge needs a merge or merge-admin gate"):
        project.owner(
            "coordination", "amend", "demo", "--delegate", "commit:develop", "--auto-merge"
        )
    with pytest.raises(BusError, match="auto-merge needs a merge or merge-admin gate"):
        project.owner("coordination", "amend", "demo", "--auto-merge")
    with pytest.raises(BusError, match="--no-delegation takes no delegation flags"):
        project.owner("coordination", "amend", "demo", "--no-delegation", "--auto-merge")
    with pytest.raises(BusError, match="amend needs"):
        project.owner("coordination", "amend", "demo")


def test_resume_with_differing_flag_raises(tmp_path: Path) -> None:
    project = Project(tmp_path, *DELEGATION)
    with pytest.raises(BusError, match="Delegation differs; use coordination amend PROJECT"):
        project.owner("coordinate", "demo", *DELEGATION, "--auto-merge")
    with pytest.raises(BusError, match="auto-merge needs a merge or merge-admin gate"):
        project.owner("coordinate", "demo", "--auto-merge")
    assert project.owner("coordinate", "demo", *DELEGATION)["delegation"]


def test_join_narrow_binds_flag_and_show_lists_it(tmp_path: Path) -> None:
    project = Project(tmp_path, *AUTO)
    invoke(
        project.store,
        tmp_path,
        "join",
        "demo",
        "--narrow",
        "merge:develop",
        "--narrow",
        "auto-merge",
        address=WORKER,
    )
    shown = project.owner("coordination", "show", "demo")
    assert list(cast("dict[str, list[str]]", shown["bindings"]).values()) == [
        ["merge:develop", "auto-merge"]
    ]
    with pytest.raises(BusError, match="Not delegated by the project"):
        invoke(project.store, tmp_path, "join", "demo", "--narrow", "nope", address=WORKER)


def test_delegation_flag_mechanics() -> None:
    plain = Delegation.parse({"version": 1, "gates": {"merge": ["develop"]}})
    assert plain.auto_merge is False
    assert "auto_merge" not in plain.wire()
    flagged = Delegation.from_specs(["merge:develop"], auto_merge=True)
    assert Delegation.parse(flagged.wire()) == flagged
    assert flagged.items() == ("merge:develop", "auto-merge")
    assert flagged.intersect(plain).auto_merge is False
    assert flagged.covers(plain)
    assert not plain.covers(flagged)
    assert flagged.narrow(["merge:develop"]).auto_merge is False
    with pytest.raises(BusError, match="Invalid delegation flag"):
        Delegation.parse({"version": 1, "gates": {"merge": ["develop"]}, "auto_merge": 1})
    with pytest.raises(BusError, match="Invalid delegation fields"):
        Delegation.parse({"version": 1, "gates": {"merge": ["develop"]}, "other": True})


def test_wizard_stores_auto_merge_without_flag_words_in_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".beads").mkdir()
    store = Project(tmp_path, claim=False).store
    store.register("codex", "wiz", str(tmp_path))
    seen: list[list[str]] = []
    real = Delegation.from_specs

    def spy(specs: list[str], **flags: bool) -> Delegation:
        seen.append(list(specs))
        return real(specs, **flags)

    monkeypatch.setattr(Delegation, "from_specs", staticmethod(spy))
    replies = iter(
        [
            "guided",
            "Ship",
            str(tmp_path),
            "bus-2",
            "develop",
            "",
            "",
            "",
            "",
            "",
            "no",
            "",
            "",
            "yes",
        ]
    )
    said: list[str] = []
    result = setup.run_wizard(
        store,
        "wiz",
        str(tmp_path),
        "codex:wiz",
        ask=lambda _p: next(replies),
        say=said.append,
    )
    assert result is not None
    assert all("auto-merge" not in spec for specs in seen for spec in specs)
    assert any("each merge still needs the coordinator to accept" in line for line in said)
    with store.transaction() as db:
        (text,) = cast(
            "tuple[str]",
            db.execute(
                "SELECT d.delegation_json FROM coordination_delegations d "
                "JOIN coordination_projects p ON p.project_id=d.project_id WHERE p.label='wiz'"
            ).fetchone(),
        )
    assert cast("dict[str, object]", json.loads(text))["auto_merge"] is True


def test_setup_profile_lists_auto_merge_under_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    def detected(_cwd: str) -> tuple[str | None, str]:
        return "develop", "origin-head"

    def undetected(_cwd: str) -> tuple[str | None, str]:
        return None, "undetected"

    monkeypatch.setattr(setup, "detect_integration_branch", detected)
    out = setup.profile("/nowhere")
    assert out["flags"] == ["auto-merge", "owner-decisions", "decision-defaults"]
    assert "auto-merge" not in cast("list[str]", out["delegate"])
    assert "--auto-merge" in str(out["command"])
    monkeypatch.setattr(setup, "detect_integration_branch", undetected)
    assert setup.profile("/nowhere")["flags"] == ["owner-decisions", "decision-defaults"]
