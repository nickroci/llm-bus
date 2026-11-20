"""Current caller consent never follows historical project or grant records."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from llm_bus.cli import app
from llm_bus.cli.guidance import guidance
from llm_bus.coordination import authority, store
from llm_bus.errors import BusError
from llm_bus.routing import routing_scope
from tests.coordination.test_delegation import TWO_GATES, join, start
from tests.coordination.test_enrollment import invoke

if TYPE_CHECKING:
    from pathlib import Path

    from llm_bus.mailbox.store import Store

OWNER = "codex:owner"
WORKER = "claude:worker"


def allowed(mailbox: Store, path: Path, address: str = WORKER) -> bool:
    result = invoke(mailbox, path, "coordination", "show", "demo", address=address)
    return result["project_execution_allowed"] is True


def test_current_caller_and_exact_project_only(tmp_path: Path) -> None:
    mailbox = start(tmp_path, "--project-execution")
    join(mailbox, tmp_path)
    assert allowed(mailbox, tmp_path, OWNER)
    assert allowed(mailbox, tmp_path)
    mailbox.register("claude", "stranger", str(tmp_path))
    for address in ("claude:stranger", "", "service:worker", "host/repo/claude:worker"):
        assert not allowed(mailbox, tmp_path, address)
    with mailbox.transaction() as db:
        record = store.project(db, routing_scope(str(tmp_path)), "demo")
        for field in ("generation", "project_id", "scope"):
            stale = {**record, field: str(uuid4())}
            assert not authority.project_execution_allowed(
                db, stale, WORKER, scope=str(record["scope"]), now=datetime.now(UTC)
            )
    other = tmp_path / "other"
    other.mkdir()
    mailbox.register("claude", "worker", str(other))
    assert not allowed(mailbox, tmp_path)
    with pytest.raises(BusError, match="Unknown coordination project"):
        invoke(mailbox, other, "coordination", "show", "demo", address=WORKER)


@pytest.mark.parametrize(
    "action", ["leave", "revoke", "project_revoke", "expiry", "replace", "inactive", "close"]
)
def test_lifecycle_revokes_current_status_and_guidance(tmp_path: Path, action: str) -> None:
    mailbox = start(tmp_path, "--project-execution")
    join(mailbox, tmp_path)
    assert allowed(mailbox, tmp_path)
    historical = guidance(mailbox, WORKER, str(tmp_path), full=True)
    assert '"project_execution_allowed": true' in historical
    if action == "leave":
        invoke(mailbox, tmp_path, "coordination", "leave", "demo", address=WORKER)
    elif action == "revoke":
        invoke(mailbox, tmp_path, "coordination", "revoke", "demo", "--recipient", WORKER)
    elif action == "project_revoke":
        invoke(mailbox, tmp_path, "coordination", "revoke", "demo")
    elif action == "replace":
        replacement_args = (
            "coordination",
            "replace",
            "demo",
            f"--generation={uuid4()}",
            f"--coordinator={OWNER}",
        )
        invoke(mailbox, tmp_path, *replacement_args)
    else:
        with mailbox.transaction() as db:
            if action == "expiry":
                db.execute("UPDATE coordination_grants SET expires_at='2000-01-01T00:00:00Z'")
            elif action == "inactive":
                db.execute(
                    "UPDATE agents SET last_seen='2000-01-01T00:00:00Z' WHERE address=?", (WORKER,)
                )
            else:
                db.execute(
                    "UPDATE agents SET ended_at='2000-01-01T00:00:00Z' WHERE address=?", (WORKER,)
                )
    assert not allowed(mailbox, tmp_path)
    assert '"project_execution_allowed": true' not in guidance(
        mailbox, WORKER, str(tmp_path), full=True
    )
    # Showing the old consent cannot restore it; its history remains readable.
    assert not allowed(mailbox, tmp_path)


def test_narrowing_revocation_and_widening_require_direct_join(tmp_path: Path) -> None:
    mailbox = start(tmp_path, *TWO_GATES, "--project-execution")
    join(mailbox, tmp_path, "--narrow", "commit:develop")
    assert not allowed(mailbox, tmp_path)
    join(mailbox, tmp_path)
    assert allowed(mailbox, tmp_path)
    invoke(mailbox, tmp_path, "coordination", "amend", "demo", *TWO_GATES)
    assert not allowed(mailbox, tmp_path)
    invoke(mailbox, tmp_path, "coordination", "amend", "demo", *TWO_GATES, "--project-execution")
    assert not allowed(mailbox, tmp_path)
    assert allowed(mailbox, tmp_path, OWNER)
    join(mailbox, tmp_path)
    assert allowed(mailbox, tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        "remote",
        "grant_owner",
        "grant_generation",
        "grant_coordinator",
        "consent_owner",
        "consent_ended",
        "malformed",
    ],
)
def test_unverified_or_unsupported_evidence_denies(tmp_path: Path, mutation: str) -> None:
    mailbox = start(tmp_path, "--project-execution")
    join(mailbox, tmp_path)
    with mailbox.transaction() as db:
        if mutation == "remote":
            db.execute("UPDATE coordination_projects SET coordinator='host/repo/codex:owner'")
        elif mutation == "grant_owner":
            db.execute("UPDATE coordination_grants SET recipient='claude:stranger'")
        elif mutation == "grant_generation":
            db.execute("UPDATE coordination_grants SET generation=?", (str(uuid4()),))
        elif mutation == "grant_coordinator":
            db.execute("UPDATE coordination_grants SET coordinator='codex:stranger'")
        elif mutation == "consent_owner":
            db.execute("UPDATE coordination_session_consents SET address='claude:stranger'")
        elif mutation == "consent_ended":
            db.execute("UPDATE coordination_session_consents SET ended_at='2000-01-01T00:00:00Z'")
        else:
            db.execute("UPDATE coordination_delegations SET delegation_json='{}'")
    # The shared predicate fails closed even when status history itself is malformed.
    with mailbox.transaction() as db:
        record = store.project(db, routing_scope(str(tmp_path)), "demo")
        assert not authority.project_execution_allowed(
            db, record, WORKER, scope=str(record["scope"]), now=datetime.now(UTC)
        )


@pytest.mark.parametrize("address", [OWNER, WORKER])
def test_consent_only_guidance_requires_fresh_status_and_preserves_floor(
    tmp_path: Path, address: str
) -> None:
    mailbox = start(tmp_path, "--project-execution")
    join(mailbox, tmp_path)
    shown = guidance(mailbox, address, str(tmp_path), full=True)
    assert '"project_execution_allowed": true' in shown
    assert "paid or live QA runs and development deployments" in shown
    assert "require fresh project_execution_allowed: true" in shown
    assert "this notice are not approval" in shown
    assert "separate valid merge authorization and live CI and review checks" in shown
    assert "Native and provider permissions and repository rules still apply" in shown
    assert "production changes" in shown
    assert "Production operations stay outside this consent" in shown
    assert "credential or secret changes" in shown
    assert "force-push" in shown
    assert "merge with failing or pending CI" in shown


def test_legacy_consent_is_off_and_service_status_cannot_choose_a_caller(tmp_path: Path) -> None:
    mailbox = start(tmp_path, *TWO_GATES)
    join(mailbox, tmp_path)
    assert not allowed(mailbox, tmp_path)
    assert not allowed(mailbox, tmp_path, OWNER)
    with pytest.raises(BusError):
        app.host_identity({})
    with pytest.raises(BusError):
        app.host_identity({"CODEX_THREAD_ID": "owner", "CLAUDE_CODE_SESSION_ID": "worker"})


@pytest.mark.parametrize("ambiguous", [False, True])
def test_shell_status_never_borrows_active_members_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    ambiguous: bool,
) -> None:
    mailbox = start(tmp_path, "--project-execution")
    join(mailbox, tmp_path)
    monkeypatch.setenv("LLM_BUS_DB", str(mailbox.path))
    monkeypatch.chdir(tmp_path)
    for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    if ambiguous:
        monkeypatch.setenv("CODEX_THREAD_ID", "owner")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "worker")
    assert app.main(["coordination", "show", "demo"]) == 1
    assert "project_execution_allowed" not in capsys.readouterr().out


@pytest.mark.parametrize("ended", [False, True])
def test_coordinator_requires_live_native_session(tmp_path: Path, *, ended: bool) -> None:
    mailbox = start(tmp_path, "--project-execution")
    assert allowed(mailbox, tmp_path, OWNER)
    with mailbox.transaction() as db:
        if ended:
            db.execute(
                "UPDATE agents SET ended_at='2000-01-01T00:00:00Z' WHERE address=?", (OWNER,)
            )
        else:
            db.execute(
                "UPDATE agents SET last_seen='2000-01-01T00:00:00Z' WHERE address=?", (OWNER,)
            )
    assert not allowed(mailbox, tmp_path, OWNER)
