"""Unattended owner confirmation, additive migration and action-time lifetime boundaries."""

# Independent expected JSON fields and human errors are test outcomes, not policy configuration.
# pylint: disable=clean-code-business-policy-literal

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from llm_bus.cli.app import main
from llm_bus.coordination import decisions, delegation, runs, setup, store
from llm_bus.errors import BusError
from llm_bus.mailbox.schema import SCHEMA_VERSION
from llm_bus.mailbox.store import Store
from llm_bus.routing import routing_scope
from tests.coordination.test_approvals import DELEGATION, Project
from tests.coordination.test_enrollment import invoke
from tests.support import hook_input

if TYPE_CHECKING:
    from pathlib import Path

OWNER = "codex:owner"
WORKER = "claude:worker"
OWNER_PROMPT = "Create the project and activate the reviewed unattended run."
DEADLINE = "2099-01-01T00:00:00+00:00"
SCOPE: tuple[str, ...] = (
    "--objective",
    "Build safely",
    "--ticket",
    "roam-22m",
    "--completion",
    "Reviewed and shipped",
    "--expires-at",
    DEADLINE,
    "--permission",
    "commit:develop",
)


@pytest.fixture(name="project")
def project_fixture(tmp_path: Path) -> Project:
    return Project(tmp_path, *DELEGATION)


def command(project: Project, action: str, *argv: str, caller: str = OWNER) -> dict[str, object]:
    return invoke(
        project.store, project.path, "coordination", action, "demo", *argv, address=caller
    )


def activate(project: Project) -> dict[str, object]:
    decisions.record_turn(project.store, OWNER)
    return command(project, "unattended", *SCOPE, "--confirm")


def current(project: Project) -> dict[str, object]:
    with project.store.transaction() as db:
        return store.project(db, routing_scope(str(project.path)), "demo")


def test_upgrade_from_16_preserves_manual_delegation_without_run(project: Project) -> None:
    with project.store.transaction() as db:
        db.execute("DROP TABLE coordination_runs")
        db.execute("PRAGMA user_version=16")
    reopened = Store(project.path / "bus.db")
    with reopened.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert db.execute("SELECT * FROM coordination_runs").fetchall() == []
        assert delegation.project_delegation(db, current(project)) is not None
    assert command(project, "show")["unattended"] == {"active": False, "run": None, "history": []}


def test_activation_requires_verified_owner_origin_and_confirmation(project: Project) -> None:
    with pytest.raises(BusError, match="owner terminal"):
        command(project, "unattended", *SCOPE, "--confirm")
    decisions.record_turn(project.store, OWNER)
    preview = command(project, "unattended", *SCOPE)
    assert preview["stored"] is False
    assert command(project, "show")["unattended"] == {"active": False, "run": None, "history": []}
    decisions.record_turn(project.store, OWNER, owner_started=False)
    with pytest.raises(BusError, match="recognized wake"):
        command(project, "unattended", *SCOPE, "--confirm")
    with pytest.raises(BusError, match="recognized wake"):
        setup.run_activation_wizard(project.store, "demo", str(project.path), OWNER)
    approved = activate(project)
    assert approved["active"] is True
    assert approved["tickets"] == ["roam-22m"]


@pytest.mark.parametrize(
    ("prompts", "denial"),
    [
        ((OWNER_PROMPT,), None),
        (
            ("llm-bus message #9 from claude:worker. Read and handle it with `llm-bus inbox`.",),
            "recognized wake",
        ),
        ((None,), "needs a verified owner turn"),
        ((OWNER_PROMPT, None), "needs a verified owner turn"),
        ((OWNER_PROMPT, None, OWNER_PROMPT), None),
    ],
)
def test_fresh_unattended_creation_uses_first_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prompts: tuple[str | None, ...],
    denial: str | None,
) -> None:
    (tmp_path / ".beads").mkdir()
    path = tmp_path / "bus.db"
    monkeypatch.setenv("LLM_BUS_DB", str(path))
    for prompt in prompts:
        monkeypatch.setattr(
            "sys.stdin",
            hook_input(
                {
                    "session_id": "owner",
                    "hook_event_name": "UserPromptSubmit",
                    "cwd": str(tmp_path),
                    "prompt": prompt,
                }
            ),
        )
        assert main(["hook", "codex"]) == 0
    mailbox = Store(path)
    args = (
        "coordinate",
        "demo",
        "--beads-workspace",
        str(tmp_path),
        "--memory-ticket",
        "bus-1",
        *DELEGATION,
        "--unattended",
        *SCOPE,
        "--confirm",
    )
    if denial is not None:
        with pytest.raises(BusError, match=denial):
            invoke(mailbox, tmp_path, *args)
        with mailbox.transaction() as db:
            assert db.execute("SELECT label FROM coordination_projects").fetchall() == []
    else:
        result = invoke(mailbox, tmp_path, *args)
        unattended = cast("dict[str, object]", result["unattended"])
        assert unattended["active"] is True


def test_later_terminal_activation_previews_scope_and_can_cancel(project: Project) -> None:
    answers = iter(["", "no", "no", "no", "roam-22m", "Reviewed and shipped", "", DEADLINE, "no"])
    said: list[str] = []
    assert (
        setup.run_activation_wizard(
            project.store,
            "demo",
            str(project.path),
            None,
            ask=lambda _prompt: next(answers),
            say=said.append,
        )
        is None
    )
    assert any("Objective revision:" in line for line in said)
    assert any(DEADLINE in line for line in said)
    assert command(project, "show")["unattended"] == {"active": False, "run": None, "history": []}
    answers = iter(["", "no", "no", "no", "roam-22m", "Reviewed and shipped", "", DEADLINE, "yes"])
    result = setup.run_activation_wizard(
        project.store,
        "demo",
        str(project.path),
        None,
        ask=lambda _prompt: next(answers),
        say=lambda _line: None,
    )
    assert result is not None
    assert result["active"] is True


@pytest.mark.parametrize("change", ["objective", "generation", "wake", "permissions"])
def test_terminal_confirmation_rechecks_changes_without_holding_write_lock(
    project: Project, change: str
) -> None:
    answers = iter(["", "no", "no", "no", "roam-22m", "Reviewed and shipped", "", DEADLINE])

    def ask(prompt: str) -> str:
        if not prompt.startswith("Activate this run?"):
            return next(answers)
        if change == "objective":
            command(project, "amend", "--objective", "New approved objective")
        elif change == "generation":
            command(project, "replace", "--generation", str(uuid4()), "--coordinator", OWNER)
        elif change == "wake":
            decisions.record_turn(project.store, OWNER, owner_started=False)
        else:
            command(project, "amend", "--delegate", "push:develop")
        return "yes"

    with pytest.raises(BusError):
        setup.run_activation_wizard(
            project.store, "demo", str(project.path), OWNER, ask=ask, say=lambda _line: None
        )
    with project.store.transaction() as db:
        assert db.execute("SELECT * FROM coordination_runs").fetchall() == []


def test_unattended_first_wizard_presets_allow_individual_controls(tmp_path: Path) -> None:
    (tmp_path / ".beads").mkdir()
    mailbox = Store(tmp_path / "bus.db")
    mailbox.register("codex", "owner", str(tmp_path))
    answers = iter(
        [
            "",
            "Build safely",
            str(tmp_path),
            "memory-1",
            "develop",
            "",
            "no",
            "no",
            "no",
            "no",
            "",
            "",
            "roam-22m",
            "Reviewed and shipped",
            "",
            DEADLINE,
            "yes",
        ]
    )
    prompts: list[str] = []
    said: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    result = setup.run_wizard(mailbox, "demo", str(tmp_path), OWNER, ask=ask, say=said.append)
    assert result is not None
    assert prompts[0] == "Mode [unattended]/guided: "
    unattended = cast("dict[str, object]", result["unattended"])
    assert unattended["active"] is True
    with mailbox.transaction() as db:
        record = store.project(db, routing_scope(str(tmp_path)), "demo")
        held = runs.require_active(
            db, record, OWNER, ticket="roam-22m", permission="commit:develop"
        )
    assert cast("dict[str, object]", held["permissions"])["project_execution"] is True
    assert f"Objective revision: {held['objective_revision']}" in said
    assert any("Run permissions:" in line for line in said)


@pytest.mark.parametrize("caller", [WORKER, "codex:stranger", "machine/repo/codex:owner"])
def test_unrelated_callers_cannot_activate_read_or_end_run(project: Project, caller: str) -> None:
    run = activate(project)
    with pytest.raises(BusError, match="coordinator"):
        command(project, "unattended", *SCOPE, "--confirm", caller=caller)
    with project.store.transaction() as db:
        record = current(project)
        assert runs.status(db, record, caller) == {"active": False}
        with pytest.raises(BusError, match="coordinator"):
            runs.require_active(db, record, caller)
        with pytest.raises(BusError, match="coordinator"):
            runs.finish(db, record, caller, str(run["run_id"]), reason="revoked")


def test_active_api_binds_run_scope_permissions_and_expiry(project: Project) -> None:
    active = activate(project)
    with project.store.transaction() as db:
        record = current(project)
        assert (
            runs.require_active(db, record, OWNER, run_id=str(active["run_id"]))["active"] is True
        )
        bindings: tuple[dict[str, str], ...] = (
            {"ticket": "other"},
            {"permission": "push:develop"},
            {"run_id": str(uuid4())},
        )
        for binding in bindings:
            with pytest.raises(BusError, match="denied"):
                runs.require_active(
                    db,
                    record,
                    OWNER,
                    ticket=binding.get("ticket"),
                    permission=binding.get("permission"),
                    run_id=binding.get("run_id"),
                )
        with pytest.raises(BusError, match="no active run"):
            runs.require_active(db, record, OWNER, now=datetime(2099, 1, 1, tzinfo=UTC))


@pytest.mark.parametrize(
    "change", ["objective", "generation", "revoke", "permissions", "ended", "scope"]
)
def test_current_state_invalidates_cached_run_authority(project: Project, change: str) -> None:
    active = activate(project)
    cached = current(project)
    if change == "objective":
        command(project, "amend", "--objective", "Build safely")
    elif change == "generation":
        command(project, "replace", "--generation", str(uuid4()), "--coordinator", OWNER)
    elif change == "revoke":
        command(project, "revoke")
    elif change == "permissions":
        command(project, "amend", "--delegate", "push:develop")
        command(project, "amend", *DELEGATION)
    elif change == "ended":
        project.store.end_session("codex", "owner")
    else:
        other = project.path / "elsewhere"
        other.mkdir()
        project.store.register("codex", "owner", str(other))
    with project.store.transaction() as db, pytest.raises(BusError, match="denied"):
        runs.require_active(db, cached, OWNER, run_id=str(active["run_id"]))


@pytest.mark.parametrize("ending", ["unattended-revoke", "unattended-complete"])
def test_end_and_renew_preserve_history_without_old_run_authority(
    project: Project, ending: str
) -> None:
    old = activate(project)
    # Lifetime's internal transition consumes T4's separately verified completion proof.
    if ending == "unattended-complete":
        with project.store.transaction() as db:
            ended = runs.finish(db, current(project), OWNER, str(old["run_id"]), reason="completed")
    else:
        ended = command(project, ending, "--run-id", str(old["run_id"]))
    assert ended["active"] is False
    new = activate(project)
    assert old["run_id"] != new["run_id"]
    status = cast("dict[str, object]", command(project, "show")["unattended"])
    assert len(cast("list[object]", status["history"])) == 2
    with project.store.transaction() as db, pytest.raises(BusError, match="wrong run"):
        runs.require_active(db, current(project), OWNER, run_id=str(old["run_id"]))


def test_expiry_is_retained_as_history_and_cannot_be_completed(project: Project) -> None:
    active = activate(project)
    with project.store.transaction() as db:
        db.execute(
            "UPDATE coordination_runs SET expires_at=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )
    with pytest.raises(BusError, match="no active run"):
        command(project, "unattended-complete", "--run-id", str(active["run_id"]))
    status = cast("dict[str, object]", command(project, "show")["unattended"])
    assert status["active"] is False
    assert cast("list[dict[str, object]]", status["history"])[0]["end_reason"] == "expired"


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--ticket", "*"),
        ("--permission", "force-push"),
        ("--objective", "Expanded"),
        ("--expires-at", "2000-01-01T00:00:00Z"),
    ],
)
def test_invalid_scope_stores_no_run(project: Project, flag: str, value: str) -> None:
    decisions.record_turn(project.store, OWNER)
    args = list(SCOPE)
    args[args.index(flag) + 1] = value
    with pytest.raises(BusError):
        command(project, "unattended", *args, "--confirm")
    assert command(project, "show")["unattended"] == {"active": False, "run": None, "history": []}
