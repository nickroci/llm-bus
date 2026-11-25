"""Exact run authority, decision visibility and offline GitHub merge boundaries."""

# These fixtures intentionally mutate isolated provider responses between action phases.
# pylint: disable=clean-code-output-argument-mutation

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, cast

import pytest

from llm_bus.cli.guidance import guidance
from llm_bus.coordination import decisions, delivery, github_merge, recovery, store
from llm_bus.errors import BusError
from llm_bus.mailbox.models import SenderContext
from llm_bus.routing import routing_scope
from tests.coordination.test_approvals import DELEGATION, SHA, Project
from tests.coordination.test_decisions import ask, show
from tests.coordination.test_enrollment import invoke
from tests.coordination.test_gate_records import accept, submit, verification
from tests.coordination.test_runs import SCOPE, command

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from llm_bus.mailbox.store import Store
OWNER = "codex:owner"
WORKER = "claude:worker"
TICKET = "roam-22m"


@pytest.fixture(autouse=True)
def no_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    def noop(*_args: object, **_kwargs: object) -> None:
        return None

    def no_wake(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"status": "unsupported"}

    monkeypatch.setattr("llm_bus.coordination.decisions.schedule_default", noop)
    monkeypatch.setattr("llm_bus.locks.service.schedule_check", noop)
    monkeypatch.setattr("llm_bus.locks.notices.dispatch_pending", no_wake)


@pytest.fixture(name="project")
def project_fixture(tmp_path: Path) -> Project:
    return Project(
        tmp_path,
        *DELEGATION,
        "--owner-decisions",
        "--auto-merge",
        "--project-execution",
        "--decision-defaults",
        "5",
    )


def activate(project: Project, *extra: str) -> str:
    decisions.record_turn(project.store, OWNER)
    result = command(
        project,
        "unattended",
        *SCOPE[:-2],
        "--permission",
        "owner-decisions",
        "--permission",
        "merge:develop",
        "--permission",
        "commit:develop",
        "--confirm",
        "--permission",
        "auto-merge",
        *extra,
    )
    return str(result["run_id"])


def pending(project: Project, decision_class: str = "scope") -> str:
    return str(
        ask(
            project,
            "--class",
            decision_class,
            "--question",
            "Which implementation?",
            "--worker",
            WORKER,
            "--ticket",
            TICKET,
            "--recommended",
            "Reuse existing form",
        )["decision_id"]
    )


def resolve(project: Project, decision_id: str, run_id: str) -> dict[str, object]:
    return command(
        project,
        "decision-resolve",
        "--decision-id",
        decision_id,
        "--run-id",
        run_id,
        "--answer",
        "Reuse existing form",
        "--rationale",
        "Matches approved acceptance criteria",
    )


def test_wake_resolution_has_distinct_provenance_and_exact_claim(project: Project) -> None:
    run_id = activate(project)
    decision_id = pending(project)
    decisions.record_turn(project.store, OWNER, owner_started=False)
    assert resolve(project, decision_id, run_id)["source"] == "coordinator"
    shown = show(project, decision_id)
    assert shown["accepted"] is True
    assert shown["source"] == "coordinator"
    assert shown["source_session"] == OWNER
    assert cast("int", shown["source_turn"]) < 0
    assert shown["run_id"] == run_id
    project.claim(WORKER, TICKET, "roam-22m-login")
    assert show(project, decision_id)["accepted"] is False
    assert "answer" not in show(project, decision_id)


@pytest.mark.parametrize(
    "decision_class", ["security", "cost", "data", "irreversible", "objective"]
)
def test_coordinator_cannot_resolve_owner_only_classes(
    project: Project, decision_class: str
) -> None:
    run_id = activate(project)
    with pytest.raises(BusError, match="routine scope"):
        resolve(project, pending(project, decision_class), run_id)


def test_pending_worker_sees_only_own_deadline(project: Project) -> None:
    decision_id = pending(project)
    shown = show(project, decision_id)
    assert shown["accepted"] is False
    assert shown["deadline_at"]
    assert "timer wakes" in str(shown["wake_behavior"])
    assert "answer" not in shown
    assert "question" not in shown
    project.store.register("claude", "other", str(project.path))
    invoke(project.store, project.path, "join", "demo", address="claude:other")
    assert "deadline_at" not in show(project, decision_id, "claude:other")


@pytest.mark.parametrize(
    "mutation", ["revoke", "objective", "permission", "worker", "claim", "expiry"]
)
def test_decision_rechecks_authority_after_issuance(project: Project, mutation: str) -> None:
    run_id = activate(project)
    decision_id = pending(project)
    resolve(project, decision_id, run_id)
    assert show(project, decision_id)["accepted"] is True
    if mutation == "revoke":
        command(project, "unattended-revoke", "--run-id", run_id)
    elif mutation == "objective":
        command(project, "amend", "--objective", "Expanded objective")
    elif mutation == "permission":
        command(project, "amend", "--delegate", "merge:develop")
    elif mutation == "worker":
        project.store.end_session("claude", "worker")
    else:
        with project.store.transaction() as db:
            if mutation == "claim":
                db.execute("DELETE FROM coordination_claims")
            else:
                db.execute("UPDATE coordination_runs SET expires_at='2000-01-01T00:00:00Z'")
    assert show(project, decision_id)["accepted"] is False


def bound_merge(project: Project) -> tuple[str, int]:
    run_id = activate(project)
    record_id = submit(project)["record_id"]
    result = accept(project, record_id, "--run-id", run_id, "--repository", "owner/repository")
    return run_id, cast("int", result["message_id"])


def test_run_approval_admission_and_exposure_revalidate(project: Project) -> None:
    run_id, message_id = bound_merge(project)
    assert verification(project, message_id)["verified"] is True
    command(project, "unattended-revoke", "--run-id", run_id)
    assert verification(project, message_id)["verified"] is False
    # Independent manual delegation is retained after the run ends.
    manual = accept(project, submit(project)["record_id"])
    assert verification(project, manual["message_id"])["verified"] is True


def test_direction_is_bound_at_admission_and_after_wait(project: Project) -> None:
    run_id = activate(project)
    scope = routing_scope(str(project.path))
    with project.store.transaction() as db:
        record = store.project(db, scope, "demo")
        direction = delivery.outgoing(
            db,
            sender=OWNER,
            scope=scope,
            label="demo",
            recipient=WORKER,
            operation="assign",
            run_id=run_id,
            ticket=TICKET,
        )
        assert direction.run_id == run_id
        assert direction.ticket == TICKET
        assert direction.claimed_at
        assert record["generation"] == direction.generation
    sender = SenderContext("codex", "owner", str(project.path))
    message = project.store.send(sender, WORKER, "Bound task", direction=direction)
    assert verification(project, message["id"])["verified"] is True
    command(project, "unattended-revoke", "--run-id", run_id)
    assert verification(project, message["id"])["verified"] is False
    with pytest.raises(BusError, match="unattended_authority_denied"):
        project.store.send(sender, WORKER, "Stale action", direction=direction)


def test_merge_admin_and_missing_repository_are_denied(project: Project) -> None:
    run_id = activate(project)
    record_id = submit(project)["record_id"]
    with pytest.raises(BusError, match="repository_binding_required"):
        accept(project, record_id, "--run-id", run_id)
    with pytest.raises(BusError, match="unattended_admin_denied"):
        accept(
            project, record_id, "--run-id", run_id, "--repository", "owner/repository", "--admin"
        )


@pytest.fixture(name="github")
def github_fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    pr: dict[str, object] = {
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "develop",
        "headRefName": "roam-22m-login",
        "headRefOid": SHA,
        "reviewDecision": "APPROVED",
        "author": {"login": "implementer"},
        "reviews": [
            {
                "author": {"login": "reviewer"},
                "state": "APPROVED",
                "commit": {"oid": SHA},
                "submittedAt": "2026-01-01T00:00:00Z",
            }
        ],
    }
    state: dict[str, object] = {
        "pr": pr,
        "checks": [{"name": "ci", "state": "SUCCESS", "bucket": "pass"}],
        "runs": [{"name": "ci", "status": "completed", "conclusion": "success", "head_sha": SHA}],
        "merge": [],
        "after_checks": None,
        "other_prs": {},
    }
    real_run = subprocess.run

    def fake(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[0] != "/fixture/gh":
            # routing_scope's local Git identity check remains real.
            return real_run(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=cast("float", kwargs.get("timeout", 2)),
                env=cast("dict[str, str] | None", kwargs.get("env")),
            )
        if argv[1:3] == ["repo", "view"]:
            data: object = {"nameWithOwner": "owner/repository"}
        elif argv[1:3] == ["pr", "checks"]:
            data = state["checks"]
            callback = cast("Callable[[], None] | None", state["after_checks"])
            if callback is not None:
                callback()
        elif argv[1] == "api":
            data = {"check_runs": state["runs"]} if "check-runs" in argv[2] else {"statuses": []}
        elif argv[1:3] == ["pr", "view"]:
            data = cast("dict[str, object]", state["other_prs"]).get(argv[3], pr)
        else:
            cast("list[list[str]]", state["merge"]).append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")

    monkeypatch.setattr(github_merge, "_executable", lambda: "/fixture/gh")
    monkeypatch.setattr("llm_bus.coordination.github_merge.subprocess.run", fake)
    return state


def merge(project: Project, message_id: int, caller: str = WORKER) -> dict[str, object]:
    return command(project, "gate-merge", "--message-id", str(message_id), caller=caller)


def test_merge_uses_exact_head_and_never_admin(project: Project, github: dict[str, object]) -> None:
    _run_id, message_id = bound_merge(project)
    assert merge(project, message_id)["status"] == "submitted"
    assert github["merge"] == [
        [
            "/fixture/gh",
            "pr",
            "merge",
            "42",
            "--repo",
            "owner/repository",
            "--merge",
            "--match-head-commit",
            SHA,
        ]
    ]


@pytest.fixture(name="observation")
def observation_fixture(project: Project, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    observed: dict[str, object] = {
        "worktree": str(project.path),
        "scope": routing_scope(str(project.path)),
        "branch": "roam-22m-login",
        "head_sha": SHA,
        "clean": True,
    }

    def read(*_args: object, **_kwargs: object) -> dict[str, object]:
        return dict(observed)

    monkeypatch.setattr(recovery, "observe", read)
    return observed


def progress(
    project: Project, run_id: str, revision: int, state: str, *extra: str
) -> dict[str, object]:
    return command(
        project,
        "run-checkpoint",
        "--run-id",
        run_id,
        "--ticket",
        TICKET,
        "--worker",
        WORKER,
        "--worktree",
        str(project.path),
        "--revision",
        str(revision),
        "--state",
        state,
        *extra,
    )


def test_reordered_wakes_do_not_overwrite_progress_and_retries_are_bounded(
    project: Project, observation: dict[str, object]
) -> None:
    run_id = activate(project)
    assert progress(project, run_id, 0, "failed", "--reason", "recoverable")["revision"] == 1
    with pytest.raises(BusError, match="checkpoint"):
        progress(project, run_id, 0, "ready")
    for revision in (1, 3):
        retried = command(
            project,
            "run-retry",
            "--run-id",
            run_id,
            "--ticket",
            TICKET,
            "--worktree",
            str(project.path),
            "--revision",
            str(revision),
            "--read-only",
        )
        assert retried["attempts"] == (revision + 1) // 2
        progress(project, run_id, revision + 1, "failed", "--reason", "recoverable")
    with pytest.raises(BusError, match="exhausted"):
        command(
            project,
            "run-retry",
            "--run-id",
            run_id,
            "--ticket",
            TICKET,
            "--worktree",
            str(project.path),
            "--revision",
            "5",
            "--read-only",
        )
    observation["head_sha"] = "b" * 40
    assert cast("dict[str, object]", command(project, "show")["unattended"])["active"] is True


def replacement(project: Project) -> str:
    worker = "claude:replacement"
    project.store.register("claude", "replacement", str(project.path))
    invoke(project.store, project.path, "join", "demo", address=worker)
    return worker


def cancelled(project: Project, run_id: str) -> str:
    return str(
        command(
            project,
            "handoff-ready",
            "--run-id",
            run_id,
            "--ticket",
            TICKET,
            "--worktree",
            str(project.path),
            "--no-pending-effects",
            caller=WORKER,
        )["handoff_id"]
    )


def move(project: Project, handoff_id: str, worker: str) -> dict[str, object]:
    return command(
        project,
        "run-handoff",
        "--handoff-id",
        handoff_id,
        "--worker",
        worker,
        "--worktree",
        str(project.path),
    )


@pytest.mark.usefixtures("observation")
@pytest.mark.parametrize("source", ["coordinator", "owner", "default"])
def test_handoff_carries_provenance_and_denies_old_worker_and_duplicate_transfer(
    project: Project, source: str
) -> None:
    run_id = activate(project)
    original = pending(project)
    if source == "coordinator":
        resolve(project, original, run_id)
    elif source == "owner":
        command(
            project, "decision-answer", "--decision-id", original, "--answer", "Reuse existing form"
        )
    else:
        with project.store.transaction() as db:
            db.execute("UPDATE coordination_decisions SET deadline_at='2000-01-01T00:00:00Z'")
        command(project, "show")
    worker = replacement(project)
    handoff_id = cancelled(project, run_id)
    moved = move(project, handoff_id, worker)
    assert moved["fresh_gates_required"] is True
    copied = cast("list[str]", moved["decisions"])[0]
    shown = show(project, copied, worker)
    assert shown["accepted"] is True
    assert shown["source"] == source
    assert shown["original_decision_id"] == original
    assert shown["source_session"] == ("default" if source == "default" else OWNER)
    assert show(project, original)["accepted"] is False
    assert show(project, copied)["accepted"] is False
    with pytest.raises(BusError, match="checkpoint"):
        move(project, handoff_id, worker)
    with pytest.raises(BusError, match="already claimed"):
        project.claim(WORKER, TICKET, "roam-22m-login")


@pytest.mark.parametrize("mutation", ["claim", "head", "dirty", "replacement-ended"])
def test_handoff_rechecks_ownership_and_live_evidence(
    project: Project, observation: dict[str, object], mutation: str
) -> None:
    run_id = activate(project)
    worker = replacement(project)
    handoff_id = cancelled(project, run_id)
    if mutation == "claim":
        project.claim(WORKER, TICKET, "roam-22m-login")
    elif mutation == "head":
        observation["head_sha"] = "b" * 40
    elif mutation == "dirty":
        observation["clean"] = False
    else:
        project.store.end_session("claude", "replacement")
    with pytest.raises(BusError):
        move(project, handoff_id, worker)
    with project.store.transaction() as db:
        assert db.execute("SELECT address FROM coordination_claims").fetchall() == [(WORKER,)]


def test_completion_requires_durable_provider_confirmation_and_survives_resume(
    project: Project, github: dict[str, object]
) -> None:
    run_id = activate(project, "--completion-proof", "merged-pr")
    record_id = submit(project)["record_id"]
    message_id = cast(
        "int",
        accept(project, record_id, "--run-id", run_id, "--repository", "owner/repository")[
            "message_id"
        ],
    )
    with pytest.raises(BusError, match="Completion denied"):
        command(project, "unattended-complete", "--run-id", run_id)
    merge(project, message_id)
    with pytest.raises(BusError, match="Prior merge attempt"):
        merge(project, message_id)
    assert len(cast("list[object]", github["merge"])) == 1
    cast("dict[str, object]", github["pr"])["state"] = "MERGED"
    result = command(project, "run-merge-reconcile", "--message-id", str(message_id))
    assert result["status"] == "succeeded"
    ended = command(project, "unattended-complete", "--run-id", run_id)
    assert ended["end_reason"] == "completed"
    resumed = invoke(project.store, project.path, "coordinate", "demo")
    retained = cast(
        "list[dict[str, object]]", cast("dict[str, object]", resumed["unattended"])["history"]
    )[-1]
    summary = cast("dict[str, object]", retained["summary"])
    assert summary["complete"] is True
    assert summary["end_reason"] == "completed"
    assert cast("list[dict[str, object]]", summary["results"])[0]["head_sha"] == SHA


@pytest.mark.usefixtures("github")
def test_interrupted_merge_stays_unknown_and_cannot_replay(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_id, message_id = bound_merge(project)
    prior = cast("Callable[..., subprocess.CompletedProcess[str]]", subprocess.run)

    def interrupted(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ["pr", "merge"]:
            raise subprocess.TimeoutExpired(argv, 60)
        return prior(argv, **kwargs)  # pylint: disable=subprocess-run-check

    monkeypatch.setattr("llm_bus.coordination.github_merge.subprocess.run", interrupted)
    with pytest.raises(BusError, match="outcome unknown"):
        merge(project, message_id)
    with project.store.transaction() as db:
        assert db.execute("SELECT status FROM coordination_run_merges").fetchone() == ("unknown",)
    with pytest.raises(BusError, match="Prior merge attempt"):
        merge(project, message_id)


def test_reviewed_green_pr_completes_without_merge_and_rechecks_provider(
    project: Project, github: dict[str, object]
) -> None:
    command(project, "amend", "--owner-decisions")
    decisions.record_turn(project.store, OWNER)
    run_id = str(
        command(
            project,
            "unattended",
            "--objective",
            "Build safely",
            "--ticket",
            TICKET,
            "--completion",
            "Reviewed PR ready",
            "--expires-at",
            "2099-01-01T00:00:00Z",
            "--permission",
            "owner-decisions",
            "--confirm",
        )["run_id"]
    )
    record_id = str(submit(project, run_id=run_id)["record_id"])
    with pytest.raises(BusError, match="gate_not_delegated"):
        accept(project, record_id, "--run-id", run_id, "--repository", "owner/repository")
    result = command(
        project,
        "run-result",
        "--run-id",
        run_id,
        "--record-id",
        record_id,
        "--repository",
        "owner/repository",
    )
    assert result["status"] == "reviewed"
    cast("dict[str, object]", github["pr"])["headRefOid"] = "b" * 40
    with pytest.raises(BusError, match="stale PR"):
        command(project, "unattended-complete", "--run-id", run_id)
    cast("dict[str, object]", github["pr"])["headRefOid"] = SHA
    assert command(project, "unattended-complete", "--run-id", run_id)["end_reason"] == "completed"
    assert github["merge"] == []


@pytest.mark.usefixtures("github")
def test_completion_revalidates_claim_after_provider_verification(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = activate(project)
    record_id = str(submit(project)["record_id"])
    command(
        project,
        "run-result",
        "--run-id",
        run_id,
        "--record-id",
        record_id,
        "--repository",
        "owner/repository",
    )
    original = github_merge.verify_completion

    def reacquire(  # noqa: PLR0913 - matches the provider verification boundary
        mailbox: Store, scope: str, label: str, caller: str, run_id: str, *, cwd: str
    ) -> github_merge.CompletionEvidence:
        # pylint: disable=too-many-arguments
        result = original(mailbox, scope, label, caller, run_id, cwd=cwd)
        project.claim(WORKER, TICKET, "roam-22m-login")
        return result

    monkeypatch.setattr(github_merge, "verify_completion", reacquire)
    with pytest.raises(BusError, match="stale claim"):
        command(project, "unattended-complete", "--run-id", run_id)
    with project.store.transaction() as db:
        assert db.execute("SELECT ended_at FROM coordination_runs").fetchone() == (None,)


def test_completion_uses_replacement_review_and_retains_history(
    project: Project, github: dict[str, object]
) -> None:
    run_id = activate(project)
    for _ in range(2):
        project.claim(WORKER, TICKET, "roam-22m-login")
        record_id = str(submit(project)["record_id"])
        command(
            project,
            "run-result",
            "--run-id",
            run_id,
            "--record-id",
            record_id,
            "--repository",
            "owner/repository",
        )
    assert command(project, "unattended-complete", "--run-id", run_id)["end_reason"] == "completed"
    with project.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM coordination_run_reviews").fetchone() == (2,)
    assert github["merge"] == []


@pytest.mark.parametrize("ending", ["revoked", "expired"])
def test_submitted_merge_reconciles_after_authority_ends(
    project: Project, github: dict[str, object], ending: str
) -> None:
    run_id, message_id = bound_merge(project)
    merge(project, message_id)
    if ending == "revoked":
        command(project, "unattended-revoke", "--run-id", run_id)
    else:
        with project.store.transaction() as db:
            db.execute("UPDATE coordination_runs SET expires_at='2000-01-01T00:00:00Z'")
    cast("dict[str, object]", github["pr"])["state"] = "MERGED"
    result = command(project, "run-merge-reconcile", "--message-id", str(message_id))
    assert result["status"] == "succeeded"
    with pytest.raises(BusError):
        merge(project, message_id)
    with pytest.raises(BusError, match="no active run"):
        command(project, "unattended-complete", "--run-id", run_id)
    assert len(cast("list[object]", github["merge"])) == 1


@pytest.mark.parametrize("binding", ["caller", "project", "generation", "during-read"])
def test_ended_merge_reconciliation_preserves_original_bindings(
    project: Project, github: dict[str, object], monkeypatch: pytest.MonkeyPatch, binding: str
) -> None:
    run_id, message_id = bound_merge(project)
    merge(project, message_id)
    command(project, "unattended-revoke", "--run-id", run_id)
    cast("dict[str, object]", github["pr"])["state"] = "MERGED"
    original = cast("Callable[..., subprocess.CompletedProcess[str]]", subprocess.run)

    def changed(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        result = original(argv, **kwargs)  # pylint: disable=subprocess-run-check
        with project.store.transaction() as db:
            db.execute("UPDATE coordination_runs SET generation='changed'")
        return result

    if binding == "during-read":
        monkeypatch.setattr("llm_bus.coordination.github_merge.subprocess.run", changed)
    elif binding in {"project", "generation"}:
        with project.store.transaction() as db:
            if binding == "project":
                db.execute("UPDATE coordination_runs SET project_id='other'")
            else:
                db.execute("UPDATE coordination_runs SET generation='changed'")
    with pytest.raises(BusError, match="Run history denied"):
        invoke(
            project.store,
            project.path,
            "coordination",
            "run-merge-reconcile",
            "demo",
            "--message-id",
            str(message_id),
            address=WORKER if binding == "caller" else OWNER,
        )
    with project.store.transaction() as db:
        assert db.execute("SELECT status FROM coordination_run_merges").fetchone() == ("submitted",)
    assert len(cast("list[object]", github["merge"])) == 1


def test_ready_work_remains_actionable_alongside_independent_blocker(
    project: Project, observation: dict[str, object]
) -> None:
    decisions.record_turn(project.store, OWNER)
    run_id = str(
        command(
            project,
            "unattended",
            "--objective",
            "Build safely",
            "--ticket",
            TICKET,
            "--ticket",
            "blocked-2",
            "--completion",
            "PRs ready",
            "--expires-at",
            "2099-01-01T00:00:00Z",
            "--permission",
            "owner-decisions",
            "--confirm",
        )["run_id"]
    )
    progress(project, run_id, 0, "ready")
    other = replacement(project)
    project.claim(other, "blocked-2", "blocked-branch")
    observation["branch"] = "blocked-branch"
    command(
        project,
        "run-checkpoint",
        "--run-id",
        run_id,
        "--ticket",
        "blocked-2",
        "--worker",
        other,
        "--worktree",
        str(project.path),
        "--revision",
        "0",
        "--state",
        "blocked",
        "--reason",
        "owner",
    )
    shown = cast("dict[str, object]", command(project, "show")["unattended"])
    summary = cast("dict[str, object]", cast("dict[str, object]", shown["run"])["summary"])
    assert summary["ready"] == [TICKET]
    assert summary["blockers"] == [{"ticket": "blocked-2", "reason": "owner"}]
    assert summary["complete"] is False
    notice = guidance(project.store, OWNER, str(project.path), full=False)
    assert run_id in notice
    assert '"ready": ["roam-22m"]' in notice


@pytest.mark.usefixtures("observation")
def test_concurrent_handoffs_have_one_owner(project: Project) -> None:
    run_id = activate(project)
    worker = replacement(project)
    handoff_id = cancelled(project, run_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(move, project, handoff_id, worker) for _ in range(2)]
        outcomes: list[str] = []
        for future in futures:
            try:
                outcomes.append(str(future.result()["worker"]))
            except BusError:
                outcomes.append("denied")
    assert sorted(outcomes) == [worker, "denied"]
    with project.store.transaction() as db:
        assert db.execute("SELECT address FROM coordination_claims").fetchall() == [(worker,)]


@pytest.mark.usefixtures("observation")
def test_late_unknown_effect_blocks_handoff_without_changing_claim(project: Project) -> None:
    run_id = activate(project)
    worker = replacement(project)
    handoff_id = cancelled(project, run_id)
    with project.store.transaction() as db:
        db.execute(
            "INSERT INTO coordination_run_merges VALUES (1,?,?,?,'unknown')",
            (run_id, TICKET, json.dumps({"ticket": TICKET})),
        )
    with pytest.raises(BusError, match="unknown provider"):
        move(project, handoff_id, worker)
    with project.store.transaction() as db:
        assert db.execute("SELECT address FROM coordination_claims").fetchall() == [(WORKER,)]


def test_retry_requires_read_only_and_unchanged_side_effect_evidence(
    project: Project, observation: dict[str, object]
) -> None:
    run_id = activate(project)
    progress(project, run_id, 0, "failed")
    retry = (
        "--run-id",
        run_id,
        "--ticket",
        TICKET,
        "--worktree",
        str(project.path),
        "--revision",
        "1",
    )
    with pytest.raises(BusError, match="read-only"):
        command(project, "run-retry", *retry)
    observation["head_sha"] = "b" * 40
    with pytest.raises(BusError, match="unchanged"):
        command(project, "run-retry", *retry, "--read-only")


def test_dirty_work_is_retained_as_blocker_without_becoming_ready(
    project: Project, observation: dict[str, object]
) -> None:
    run_id = activate(project)
    observation["clean"] = False
    assert progress(project, run_id, 0, "blocked", "--reason", "host")["revision"] == 1
    with pytest.raises(BusError, match="clean worktree"):
        progress(project, run_id, 1, "ready")


@pytest.mark.usefixtures("github")
def test_reacquired_claim_requires_fresh_gate_for_unattended_approval_and_result(
    project: Project,
) -> None:
    run_id = activate(project)
    record_id = str(submit(project)["record_id"])
    project.claim(WORKER, TICKET, "roam-22m-login")
    with pytest.raises(BusError, match="stale claim"):
        accept(project, record_id, "--run-id", run_id, "--repository", "owner/repository")
    with pytest.raises(BusError, match="stale claim"):
        command(
            project,
            "run-result",
            "--run-id",
            run_id,
            "--record-id",
            record_id,
            "--repository",
            "owner/repository",
        )


def test_handoff_requires_current_pr_evidence_from_the_worktree_repository(
    project: Project, observation: dict[str, object], github: dict[str, object]
) -> None:
    run_id = activate(project)
    submit(project)
    with pytest.raises(BusError, match="current open PR"):
        cancelled(project, run_id)
    observed = github_merge.observe_pr(str(project.path), "owner/repository", 42)
    observation["pr"] = {"number": 42, "repository": "owner/repository", **observed}
    handoff_id = cancelled(project, run_id)
    worker = replacement(project)
    cast("dict[str, object]", observation["pr"])["headRefOid"] = "b" * 40
    with pytest.raises(BusError, match="checkpoint"):
        move(project, handoff_id, worker)
    with pytest.raises(BusError, match="wrong GitHub repository"):
        github_merge.observe_pr(str(project.path), "other/repository", 42)
    assert github["merge"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "CLOSED"),
        ("baseRefName", "main"),
        ("headRefOid", "b" * 40),
        ("reviewDecision", "REVIEW_REQUIRED"),
        ("reviews", []),
    ],
)
def test_merge_denies_stale_pr_and_review(
    project: Project, github: dict[str, object], field: str, value: object
) -> None:
    _run_id, message_id = bound_merge(project)
    cast("dict[str, object]", github["pr"])[field] = value
    with pytest.raises(BusError, match="Merge denied"):
        merge(project, message_id)
    assert github["merge"] == []


@pytest.mark.parametrize(
    "checks",
    [
        [],
        [{"name": "ci", "state": "PENDING", "bucket": "pending"}],
        [{"name": "ci", "state": "FAILURE", "bucket": "fail"}],
        [{"name": "ci", "state": "UNKNOWN", "bucket": "pass"}],
    ],
)
def test_merge_denies_missing_or_non_green_checks(
    project: Project, github: dict[str, object], checks: object
) -> None:
    _run_id, message_id = bound_merge(project)
    github["checks"] = checks
    with pytest.raises(BusError, match="Merge denied"):
        merge(project, message_id)
    assert github["merge"] == []


def test_merge_denies_checks_for_other_head(project: Project, github: dict[str, object]) -> None:
    _run_id, message_id = bound_merge(project)
    github["runs"] = [
        {"name": "ci", "status": "completed", "conclusion": "success", "head_sha": "b" * 40}
    ]
    with pytest.raises(BusError, match="exact head"):
        merge(project, message_id)
    assert github["merge"] == []


def test_merge_rechecks_run_after_provider_wait(
    project: Project, github: dict[str, object]
) -> None:
    run_id, message_id = bound_merge(project)
    github["after_checks"] = lambda: command(project, "unattended-revoke", "--run-id", run_id)
    with pytest.raises(BusError, match="caller-bound"):
        merge(project, message_id)
    assert github["merge"] == []


def test_other_caller_cannot_use_worker_merge(project: Project, github: dict[str, object]) -> None:
    _run_id, message_id = bound_merge(project)
    with pytest.raises(BusError, match="caller-bound"):
        merge(project, message_id, OWNER)
    assert github["merge"] == []


def test_merge_denies_repository_change(project: Project, github: dict[str, object]) -> None:
    _run_id, message_id = bound_merge(project)
    with project.store.transaction() as db:
        db.execute(
            "UPDATE coordination_approvals SET approval_json=json_set(approval_json,'$.repository','other/repo') "
            "WHERE message_id=?",
            (message_id,),
        )
    with pytest.raises(BusError, match="wrong GitHub repository"):
        merge(project, message_id)
    assert github["merge"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "CLOSED"),
        ("isDraft", True),
        ("baseRefName", "main"),
        ("headRefName", "other-branch"),
        ("headRefOid", "b" * 40),
        ("reviewDecision", "CHANGES_REQUESTED"),
        ("reviews", []),
    ],
)
def test_merge_denies_pr_or_review_change_during_wait(
    project: Project, github: dict[str, object], field: str, value: object
) -> None:
    _run_id, message_id = bound_merge(project)

    def change_pr() -> None:
        cast("dict[str, object]", github["pr"])[field] = value

    github["after_checks"] = change_pr
    with pytest.raises(BusError, match="Merge denied"):
        merge(project, message_id)
    assert github["merge"] == []
