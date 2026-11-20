"""Project delegation, worker bindings, amendment and rejoin semantics."""

# Each scenario states its owner commands and stored rows explicitly.
# pylint: disable=duplicate-code

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from llm_bus.coordination import cli, delegation
from llm_bus.coordination.delegation import Delegation
from llm_bus.errors import BusError
from llm_bus.mailbox.store import Store
from tests.coordination.test_enrollment import invoke

if TYPE_CHECKING:
    from pathlib import Path

TWO_GATES = ("--delegate", "commit:develop", "--delegate", "push:develop")


def start(tmp_path: Path, *extra: str) -> Store:
    mailbox = Store(tmp_path / "bus.db")
    mailbox.register("codex", "owner", str(tmp_path))
    mailbox.register("claude", "worker", str(tmp_path))
    (tmp_path / ".beads").mkdir(exist_ok=True)
    invoke(
        mailbox,
        tmp_path,
        "coordinate",
        "demo",
        "--objective",
        "Build safely",
        "--beads-workspace",
        str(tmp_path),
        "--memory-ticket",
        "bus-1",
        *extra,
    )
    return mailbox


def join(mailbox: Store, tmp_path: Path, *extra: str) -> dict[str, object]:
    return invoke(mailbox, tmp_path, "join", "demo", *extra, address="claude:worker")


def state(mailbox: Store) -> tuple[Delegation | None, Delegation | None, list[str]]:
    with mailbox.transaction() as db:
        scope, project_id, generation = cast(
            "tuple[str, str, str]",
            db.execute("SELECT scope,project_id,generation FROM coordination_projects").fetchone(),
        )
        record: dict[str, object] = {
            "scope": scope,
            "project_id": project_id,
            "generation": generation,
        }
        (grant_id,) = cast(
            "tuple[str]", db.execute("SELECT grant_id FROM coordination_grants").fetchone()
        )
        return (
            delegation.project_delegation(db, record),
            delegation.effective(db, record, grant_id, now=datetime.now(UTC)),
            delegation.rejoin_required(db, record),
        )


def test_project_without_delegation_writes_no_rows(tmp_path: Path) -> None:
    mailbox = start(tmp_path)
    shown = join(mailbox, tmp_path)
    assert "delegation" not in shown
    assert "bindings" not in shown
    with mailbox.transaction() as db:
        for table in ("coordination_delegations", "coordination_grant_delegations"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608


def test_join_binds_every_delegated_item(tmp_path: Path) -> None:
    mailbox = start(tmp_path, *TWO_GATES)
    shown = join(mailbox, tmp_path)
    assert shown["delegation"] == {
        "version": 1,
        "gates": {"commit": ["develop"], "push": ["develop"]},
    }
    (grant,) = cast("list[dict[str, str]]", shown["grants"])
    assert shown["bindings"] == {grant["grant_id"]: ["commit:develop", "push:develop"]}
    project, effective, rejoin = state(mailbox)
    assert effective == project
    assert rejoin == []


def test_narrow_binds_subset_and_rejects_undelegated_item(tmp_path: Path) -> None:
    mailbox = start(tmp_path, *TWO_GATES)
    shown = join(mailbox, tmp_path, "--narrow", "commit:develop")
    assert list(cast("dict[str, list[str]]", shown["bindings"]).values()) == [["commit:develop"]]
    assert state(mailbox)[1] == Delegation.from_specs(["commit:develop"])
    with pytest.raises(BusError, match="Not delegated by the project: merge:main"):
        join(mailbox, tmp_path, "--narrow", "merge:main")
    assert state(mailbox)[1] == Delegation.from_specs(["commit:develop"])


def test_narrow_without_project_delegation_is_rejected(tmp_path: Path) -> None:
    mailbox = start(tmp_path)
    with pytest.raises(BusError, match="--narrow requires a project delegation"):
        join(mailbox, tmp_path, "--narrow", "commit:develop")
    with mailbox.transaction() as db:
        assert db.execute("SELECT count(*) FROM coordination_grants").fetchone()[0] == 0


def test_amend_narrower_is_immediate_and_wider_needs_rejoin(tmp_path: Path) -> None:
    mailbox = start(tmp_path, *TWO_GATES)
    join(mailbox, tmp_path)
    narrower = invoke(
        mailbox, tmp_path, "coordination", "amend", "demo", "--delegate", "commit:develop"
    )
    assert narrower["rejoin_required"] == []
    project, effective, _rejoin = state(mailbox)
    assert effective == project == Delegation.from_specs(["commit:develop"])

    wider = invoke(
        mailbox,
        tmp_path,
        "coordination",
        "amend",
        "demo",
        "--delegate",
        "commit:develop",
        "--delegate",
        "merge:main",
    )
    assert wider["rejoin_required"] == ["claude:worker"]
    project, effective, _rejoin = state(mailbox)
    assert project == Delegation.from_specs(["commit:develop", "merge:main"])
    assert effective == Delegation.from_specs(["commit:develop"])

    rejoined = join(mailbox, tmp_path)
    assert list(cast("dict[str, list[str]]", rejoined["bindings"]).values()) == [
        ["commit:develop", "merge:main"]
    ]
    project, effective, rejoin = state(mailbox)
    assert effective == project
    assert rejoin == []


def test_dropped_items_do_not_return_without_rejoin(tmp_path: Path) -> None:
    mailbox = start(tmp_path, *TWO_GATES)
    join(mailbox, tmp_path)
    invoke(mailbox, tmp_path, "coordination", "amend", "demo", "--delegate", "commit:develop")
    invoke(mailbox, tmp_path, "coordination", "amend", "demo", *TWO_GATES)
    _project, effective, rejoin = state(mailbox)
    assert effective == Delegation.from_specs(["commit:develop"])
    assert rejoin == ["claude:worker"]


def test_no_delegation_clears_project_and_bindings(tmp_path: Path) -> None:
    mailbox = start(tmp_path, *TWO_GATES)
    join(mailbox, tmp_path)
    shown = invoke(mailbox, tmp_path, "coordination", "amend", "demo", "--no-delegation")
    assert "delegation" not in shown
    assert shown["rejoin_required"] == []
    with mailbox.transaction() as db:
        assert db.execute("SELECT count(*) FROM coordination_delegations").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM coordination_grant_delegations").fetchone()[0] == 0


def test_amend_requires_the_enrolled_coordinator(tmp_path: Path) -> None:
    mailbox = start(tmp_path, *TWO_GATES)
    join(mailbox, tmp_path)
    with pytest.raises(BusError, match="Only the enrolled coordinator"):
        invoke(
            mailbox,
            tmp_path,
            "coordination",
            "amend",
            "demo",
            "--delegate",
            "push:develop",
            address="claude:worker",
        )
    assert state(mailbox)[0] == Delegation.from_specs(["commit:develop", "push:develop"])


def old_grant_effective(
    mailbox: Store, now: datetime | None = None, *, current_generation: bool = True
) -> Delegation | None:
    """Effective delegation of the only grant, against the project's current record."""
    with mailbox.transaction() as db:
        row = cast(
            "tuple[str, str, str]",
            db.execute("SELECT scope,project_id,generation FROM coordination_projects").fetchone(),
        )
        record: dict[str, object] = dict(
            zip(("scope", "project_id", "generation"), row, strict=True)
        )
        if not current_generation:
            record["generation"] = str(uuid4())
        (grant_id,) = cast(
            "tuple[str]", db.execute("SELECT grant_id FROM coordination_grants").fetchone()
        )
        return delegation.effective(db, record, grant_id, now=now or datetime.now(UTC))


def test_effective_ends_with_revoke(tmp_path: Path) -> None:
    mailbox = start(tmp_path, *TWO_GATES)
    join(mailbox, tmp_path)
    assert old_grant_effective(mailbox) is not None
    invoke(mailbox, tmp_path, "coordination", "revoke", "demo", "--recipient", "claude:worker")
    assert old_grant_effective(mailbox) is None


def test_effective_ends_with_leave(tmp_path: Path) -> None:
    mailbox = start(tmp_path, *TWO_GATES)
    join(mailbox, tmp_path)
    invoke(mailbox, tmp_path, "coordination", "leave", "demo", address="claude:worker")
    assert old_grant_effective(mailbox) is None


def test_effective_ends_with_coordinator_replacement(tmp_path: Path) -> None:
    mailbox = start(tmp_path, *TWO_GATES)
    join(mailbox, tmp_path)
    invoke(
        mailbox,
        tmp_path,
        "coordination",
        "replace",
        "demo",
        "--generation",
        str(uuid4()),
        "--coordinator",
        "codex:owner",
    )
    invoke(mailbox, tmp_path, "coordinate", "demo")
    invoke(mailbox, tmp_path, "coordination", "amend", "demo", *TWO_GATES)
    assert old_grant_effective(mailbox) is None


def test_effective_ends_with_grant_expiry(tmp_path: Path) -> None:
    mailbox = start(tmp_path, *TWO_GATES)
    join(mailbox, tmp_path)
    soon = datetime.now(UTC) + timedelta(hours=1)
    with mailbox.transaction() as db:
        db.execute("UPDATE coordination_grants SET expires_at=?", (soon.isoformat(),))
    assert old_grant_effective(mailbox, soon - timedelta(minutes=1)) is not None
    assert old_grant_effective(mailbox, soon) is None


def test_effective_rejects_other_generation_and_ended_consent(tmp_path: Path) -> None:
    mailbox = start(tmp_path, *TWO_GATES)
    join(mailbox, tmp_path)
    assert old_grant_effective(mailbox, current_generation=False) is None
    with mailbox.transaction() as db:
        db.execute("UPDATE coordination_session_consents SET ended_at=?", ("2026-01-01T00:00:00Z",))
    assert old_grant_effective(mailbox) is None


def test_replace_generation_starts_without_delegation(tmp_path: Path) -> None:
    mailbox = start(tmp_path, *TWO_GATES)
    join(mailbox, tmp_path)
    shown = invoke(
        mailbox,
        tmp_path,
        "coordination",
        "replace",
        "demo",
        "--generation",
        str(uuid4()),
        "--coordinator",
        "codex:owner",
    )
    assert "delegation" not in shown
    with mailbox.transaction() as db:
        assert db.execute("SELECT count(*) FROM coordination_delegations").fetchone()[0] == 1


def test_resume_with_different_delegation_errors_and_same_is_idempotent(tmp_path: Path) -> None:
    mailbox = start(tmp_path, *TWO_GATES)
    again = invoke(
        mailbox,
        tmp_path,
        "coordinate",
        "demo",
        "--delegate",
        "push:develop",
        "--delegate",
        "commit:develop",
    )
    assert cast("dict[str, object]", again["delegation"])["version"] == 1
    for extra in (("--delegate", "commit:develop"), ("--delegate", "merge:main")):
        with pytest.raises(BusError, match="Delegation differs; use coordination amend PROJECT"):
            invoke(mailbox, tmp_path, "coordinate", "demo", *extra)
    plain = invoke(mailbox, tmp_path, "coordinate", "demo")
    assert plain["delegation"] == again["delegation"]


@pytest.mark.parametrize(
    "spec",
    [
        "force-push:main",
        "force-push",
        "Deploys or production changes",
        "credential or secret changes:main",
        "paid or live runs",
        "merge with failing or pending CI",
    ],
)
def test_floor_actions_are_rejected(spec: str) -> None:
    with pytest.raises(BusError, match="floor action"):
        Delegation.from_specs([spec])


def test_project_execution_round_trip_narrow_and_remote_cap() -> None:
    approved = Delegation.from_specs([], project_execution=True)
    assert approved.items() == ("project-execution",)
    assert approved.wire() == {"version": 1, "gates": {}, "project_execution": True}
    assert Delegation.parse(approved.wire()) == approved
    assert approved.narrow(["project-execution"]) == approved
    legacy = Delegation.from_specs(["commit:main"])
    assert not legacy.project_execution
    assert "project_execution" not in legacy.wire()
    assert not legacy.covers(approved)
    assert approved.only_gates(delegation.GATES).items() == ()


@pytest.mark.parametrize("bad", [None, 0, 1, "true", [], {}])
def test_project_execution_wire_rejects_non_boolean(bad: object) -> None:
    with pytest.raises(BusError, match="Invalid delegation flag"):
        Delegation.parse({"version": 1, "gates": {}, "project_execution": bad})


def test_project_execution_revoke_preserves_gates_and_reenable_requires_rejoin(
    tmp_path: Path,
) -> None:
    mailbox = start(tmp_path, *TWO_GATES, "--project-execution")
    join(mailbox, tmp_path)
    effective = state(mailbox)[1]
    assert effective is not None
    assert effective.project_execution
    invoke(mailbox, tmp_path, "coordination", "amend", "demo", *TWO_GATES)
    project, effective, _ = state(mailbox)
    assert project == effective == Delegation.from_specs(["commit:develop", "push:develop"])
    invoke(mailbox, tmp_path, "coordination", "amend", "demo", *TWO_GATES, "--project-execution")
    project, effective, rejoin = state(mailbox)
    assert project is not None
    assert project.project_execution
    assert effective is not None
    assert not effective.project_execution
    assert rejoin == ["claude:worker"]
    join(mailbox, tmp_path, "--narrow", "project-execution")
    assert state(mailbox)[1] == Delegation.from_specs([], project_execution=True)


def test_project_execution_only_can_be_revoked(tmp_path: Path) -> None:
    mailbox = start(tmp_path, "--project-execution")
    join(mailbox, tmp_path)
    invoke(mailbox, tmp_path, "coordination", "amend", "demo", "--no-delegation")
    assert state(mailbox) == (None, None, [])


def test_remote_setup_and_amend_refuse_project_execution(tmp_path: Path) -> None:
    mailbox = start(tmp_path)
    with mailbox.transaction() as db:
        db.execute("UPDATE coordination_projects SET coordinator='remote/peer/codex:owner'")
    with pytest.raises(BusError, match="Remote-coordinated"):
        invoke(mailbox, tmp_path, "coordination", "amend", "demo", "--project-execution")
    with pytest.raises(BusError, match="Remote setup"):
        cli.run(
            mailbox,
            {
                "command": "coordination",
                "action": "setup",
                "project": "other",
                "project_id": str(uuid4()),
                "generation": str(uuid4()),
                "coordinator": "remote/peer/codex:owner",
                "project_execution": True,
            },
            "codex:owner",
            str(tmp_path),
        )
    with mailbox.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM coordination_projects").fetchone() == (1,)


@pytest.mark.parametrize(
    "specs",
    [
        [],
        ["deploy:main"],
        ["commit"],
        ["commit:"],
        ["commit:-x"],
        ["commit:a..b"],
        ["commit:bad name"],
        [f"commit:b{index}" for index in range(9)],
    ],
)
def test_invalid_specs_are_rejected(specs: list[str]) -> None:
    with pytest.raises(BusError):
        Delegation.from_specs(specs)


def test_wire_round_trip_is_strict() -> None:
    value = Delegation.from_specs(["push:b", "commit:a", "commit:a", "merge-admin:main"])
    assert value.items() == ("commit:a", "merge-admin:main", "push:b")
    assert Delegation.parse(value.wire()) == value
    invalid: tuple[object, ...] = (
        {"version": 1},
        {"version": 1, "gates": {"commit": ["a"]}, "extra": 1},
        {"version": True, "gates": {"commit": ["a"]}},
        {"version": 1, "gates": {"commit": []}},
        {"version": 1, "gates": {"deploy": ["a"]}},
        {"version": 1, "gates": {"commit": ["-a"]}},
        [],
    )
    for bad in invalid:
        with pytest.raises(BusError):
            Delegation.parse(bad)


def test_set_algebra() -> None:
    wide = Delegation.from_specs(["commit:a", "commit:b", "push:a"])
    small = Delegation.from_specs(["commit:b", "merge:main"])
    assert wide.intersect(small) == Delegation.from_specs(["commit:b"])
    assert wide.covers(Delegation.from_specs(["commit:a", "push:a"]))
    assert not wide.covers(small)
    assert wide.all_targets() == {"a", "b"}
    assert wide.targets("merge") == ()
    assert wide.narrow(["push:a"]) == Delegation.from_specs(["push:a"])


def test_revoked_grant_stays_ineffective_after_rejoin(tmp_path: Path) -> None:
    mailbox = start(tmp_path, *TWO_GATES)
    join(mailbox, tmp_path)
    invoke(mailbox, tmp_path, "coordination", "revoke", "demo", "--recipient", "claude:worker")
    join(mailbox, tmp_path)
    with mailbox.transaction() as db:
        row = cast(
            "tuple[str, str, str]",
            db.execute("SELECT scope,project_id,generation FROM coordination_projects").fetchone(),
        )
        record: dict[str, object] = dict(
            zip(("scope", "project_id", "generation"), row, strict=True)
        )
        grants = cast(
            "list[tuple[str, str | None]]",
            db.execute("SELECT grant_id,revoked_at FROM coordination_grants").fetchall(),
        )
        by_state = {revoked is None: grant_id for grant_id, revoked in grants}
        now = datetime.now(UTC)
        assert delegation.effective(db, record, by_state[False], now=now) is None
        assert delegation.effective(db, record, by_state[True], now=now) == (
            Delegation.from_specs(["commit:develop", "push:develop"])
        )
