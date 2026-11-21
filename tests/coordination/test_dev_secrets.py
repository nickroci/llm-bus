"""Exact detached owner consent, local authority, replay and interruption boundaries."""

# Scenarios deliberately change isolated signed metadata and host policy between phases.
# pylint: disable=clean-code-output-argument-mutation

from __future__ import annotations

import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from llm_bus.coordination import decisions, dev_secrets, run_actions, secret_policy, store
from llm_bus.errors import BusError
from llm_bus.routing import routing_scope
from tests.coordination.test_approvals import DELEGATION, Project
from tests.coordination.test_runs import SCOPE, command

if TYPE_CHECKING:
    from pathlib import Path

OWNER = "codex:owner"
WORKER = "claude:worker"
TICKET = "roam-22m"
PLAN = b'{"effects":"replace named development resource","preconditions":{"exists":true}}'


class Consent:
    project: Project
    key: Path
    payload: dict[str, str]

    def __init__(self, project: Project, key: Path, payload: dict[str, str]) -> None:
        self.project = project
        self.key = key
        self.payload = payload

    def sign(self) -> str:
        result = subprocess.run(
            [
                "/usr/bin/ssh-keygen",
                "-Y",
                "sign",
                "-q",
                "-f",
                str(self.key),
                "-n",
                "llm-bus-dev-secret-v1",
            ],
            input=dev_secrets.canonical(self.payload),
            capture_output=True,
            check=True,
        )
        return result.stdout.decode()

    def reserve(self, signature: str | None = None) -> dict[str, object]:
        receipt = self.project.path / "receipt.json"
        signed = self.project.path / "receipt.sig"
        plan = self.project.path / "plan.json"
        receipt.write_bytes(dev_secrets.canonical(self.payload))
        signed.write_text(self.sign() if signature is None else signature)
        plan.write_bytes(PLAN)
        return command(
            self.project,
            "dev-secret-reserve",
            "--receipt-file",
            str(receipt),
            "--signature-file",
            str(signed),
            "--plan-file",
            str(plan),
            caller=WORKER,
        )

    def check(self, caller: str = WORKER) -> dict[str, object]:
        return command(
            self.project,
            "dev-secret-check",
            "--receipt-id",
            self.payload["receipt_id"],
            "--plan-file",
            str(self.project.path / "plan.json"),
            caller=caller,
        )

    def outcome(self, status: str) -> dict[str, object]:
        return command(
            self.project,
            "dev-secret-outcome",
            "--receipt-id",
            self.payload["receipt_id"],
            "--status",
            status,
            caller=WORKER,
        )


@pytest.fixture(name="consent")
def consent_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Consent:
    def noop(*_args: object, **_kwargs: object) -> None:
        return None

    def no_wake(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"status": "unsupported"}

    monkeypatch.setattr("llm_bus.coordination.decisions.schedule_default", noop)
    monkeypatch.setattr("llm_bus.locks.service.schedule_check", noop)
    monkeypatch.setattr("llm_bus.cli.commands.dispatch_pending", no_wake)
    project = Project(tmp_path, *DELEGATION, "--project-execution")
    decisions.record_turn(project.store, OWNER)
    active = command(
        project,
        "unattended",
        *SCOPE[:-2],
        "--permission",
        "project-execution",
        "--confirm",
    )
    key = tmp_path / "fixture-owner"
    subprocess.run(
        ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
    )
    public = " ".join(key.with_suffix(".pub").read_text().split()[:2])
    policy = {
        "version": 1,
        "machine": "fixture-machine",
        "owners": {"owner": public},
        "targets": [
            {
                "provider": "fixture",
                "account": "account-1",
                "provider_project": "app-1",
                "target": "dev-1",
                "environment": "development",
            }
        ],
        "revoked": [],
    }
    # Only the protected filesystem boundary is replaced; SSH verification remains real.
    monkeypatch.setattr(secret_policy, "_protected_bytes", lambda: json.dumps(policy).encode())
    with project.store.transaction() as db:
        record = store.project(db, routing_scope(str(tmp_path)), "demo")
        branch, claimed_at = run_actions.claim(db, record, WORKER, TICKET)
        grant = cast(
            "tuple[str]",
            db.execute(
                "SELECT grant_id FROM coordination_session_consents WHERE address=?", (WORKER,)
            ).fetchone(),
        )[0]
    payload = {
        "version": "1",
        "receipt_id": str(uuid4()),
        "owner": "owner",
        "repository": str(record["scope"]),
        "project": str(record["project_id"]),
        "run": str(active["run_id"]),
        "generation": str(record["generation"]),
        "ticket": TICKET,
        "actor_machine": "fixture-machine",
        "actor_session": WORKER,
        "branch": branch,
        "claimed_at": claimed_at,
        "grant_id": grant,
        "provider": "fixture",
        "account": "account-1",
        "provider_project": "app-1",
        "target": "dev-1",
        "secret": "API_KEY",
        "operation": "replace",
        "source_ref": "vault://immutable-resource#version-1",
        "plan_sha256": hashlib.sha256(PLAN).hexdigest(),
        "expires_at": "2098-01-01T00:00:00Z",
    }
    return Consent(project, key, payload)


def test_exact_receipt_one_check_and_bounded_outcome(consent: Consent) -> None:
    assert consent.reserve()["status"] == "reserved"
    assert consent.check()["status"] == "ready"
    with pytest.raises(BusError):
        consent.check()
    assert consent.outcome("succeeded") == {
        "receipt_id": consent.payload["receipt_id"],
        "status": "succeeded",
        "retry_allowed": False,
    }
    with pytest.raises(BusError):
        consent.reserve()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target", "production"),
        ("target", "unknown"),
        ("owner", "untrusted"),
        ("actor_machine", "other-machine"),
        ("actor_session", OWNER),
        ("expires_at", "2000-01-01T00:00:00Z"),
        ("expires_at", "2100-01-01T00:00:00Z"),
        ("plan_sha256", "f" * 64),
        ("run", str(uuid4())),
        ("generation", str(uuid4())),
        ("grant_id", str(uuid4())),
        ("claimed_at", "2000-01-01T00:00:00Z"),
        ("secret", "*"),
        ("source_ref", "unversioned"),
        ("extra", "sentinel-secret-value"),
    ],
)
def test_signed_but_invalid_binding_denied(consent: Consent, field: str, value: str) -> None:
    consent.payload[field] = value
    with pytest.raises(BusError):
        consent.reserve()


def test_signature_binds_bytes_and_rejects_missing_or_wrong_signer(
    consent: Consent, tmp_path: Path
) -> None:
    signed = consent.sign()
    consent.payload["secret"] = "OTHER_KEY"  # noqa: S105 - resource identifier, not a value
    for signature in (signed, "", "invalid"):
        with pytest.raises(BusError):
            consent.reserve(signature)
    other = tmp_path / "other-owner"
    subprocess.run(
        ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True
    )
    consent.key = other
    with pytest.raises(BusError):
        consent.reserve()


@pytest.mark.parametrize(
    "change", ["plan", "claim", "run", "grant", "anchor", "revoked", "caller", "expiry"]
)
def test_action_rechecks_current_authority(
    consent: Consent, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    consent.reserve()
    if change == "plan":
        (consent.project.path / "plan.json").write_bytes(b"changed plan")
    elif change in {"anchor", "revoked"}:
        policy = secret_policy.load()
        if change == "anchor":
            policy["owners"] = {}
        else:
            policy["revoked"] = [consent.payload["receipt_id"]]
        monkeypatch.setattr(secret_policy, "_protected_bytes", lambda: json.dumps(policy).encode())
    elif change == "run":
        command(consent.project, "unattended-revoke", "--run-id", consent.payload["run"])
    elif change == "expiry":
        with consent.project.store.transaction() as db:
            db.execute("UPDATE coordination_runs SET expires_at='2000-01-01T00:00:00Z'")
    elif change in {"claim", "grant"}:
        with consent.project.store.transaction() as db:
            if change == "claim":
                db.execute("UPDATE coordination_claims SET claimed_at='2000-01-01T00:00:00Z'")
            else:
                db.execute(
                    "UPDATE coordination_session_consents SET ended_at='2000-01-01T00:00:00Z'"
                )
    with pytest.raises(BusError):
        consent.check(OWNER if change == "caller" else WORKER)


def test_restart_never_reissues_reserved_or_unknown_action(consent: Consent) -> None:
    consent.reserve()
    with pytest.raises(BusError):
        consent.reserve()
    consent.check()
    assert consent.outcome("unknown")["retry_allowed"] is False
    with pytest.raises(BusError):
        consent.check()
    assert consent.outcome("fresh_approval_required")["status"] == "fresh_approval_required"


@pytest.mark.parametrize("phase", ["reserve", "check"])
def test_concurrent_duplicate_consumption_only_one_wins(consent: Consent, phase: str) -> None:
    signature = consent.sign()
    if phase == "check":
        consent.reserve(signature)

    def reserve(_index: int) -> str:
        try:
            with consent.project.store.transaction() as db:
                db.execute("BEGIN IMMEDIATE")
                record = store.project(db, routing_scope(str(consent.project.path)), "demo")
                if phase == "check":
                    dev_secrets.check(db, record, WORKER, consent.payload["receipt_id"], PLAN)
                else:
                    dev_secrets.reserve(db, record, WORKER, consent.payload, PLAN, signature)
        except BusError:
            return "denied"
        return "reserved"

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(reserve, range(2))) == ["denied", "reserved"]


@pytest.mark.parametrize("operation", ["create", "delete"])
def test_provider_permission_can_deny_and_no_secret_is_retained(
    consent: Consent, operation: str
) -> None:
    consent.payload["operation"] = operation
    if operation == "delete":
        consent.payload["source_ref"] = ""
    reserved = consent.reserve()
    ready = consent.check()
    denied = consent.outcome("denied")
    with consent.project.store.transaction() as db:
        records = db.execute(
            "SELECT payload,signature,status FROM coordination_dev_secrets"
        ).fetchall()
        messages = db.execute("SELECT body FROM messages").fetchall()
    assert denied["status"] == "denied"
    assert "sentinel-secret-value" not in json.dumps([reserved, ready, denied, records, messages])
    assert set(cast("dict[str, str]", ready["action"])) == dev_secrets.FIELDS
    with pytest.raises(BusError):
        consent.check()


def test_protected_policy_denies_root_missing_or_writable_anchor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(secret_policy, "POLICY_PATH", tmp_path / "missing")
    monkeypatch.setattr("os.geteuid", lambda: 0)
    with pytest.raises(BusError):
        secret_policy.load()
    monkeypatch.setattr("os.geteuid", lambda: 501)
    with pytest.raises(BusError):
        secret_policy.load()
    anchor = tmp_path / "policy"
    anchor.write_text("{}")
    monkeypatch.setattr(secret_policy, "POLICY_PATH", anchor)
    monkeypatch.setenv("LLM_BUS_COLLAB_TEST_KEYS_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_BUS_DEV_SECRET_POLICY", str(anchor))
    with pytest.raises(BusError):
        secret_policy.load()


def test_unknown_secret_field_is_not_logged_or_retained(consent: Consent) -> None:
    consent.payload["secret_value"] = "sentinel-secret-value"  # noqa: S105 - rejected test sentinel
    with pytest.raises(BusError) as error:
        consent.reserve()
    assert "sentinel-secret-value" not in str(error.value)
    with consent.project.store.transaction() as db:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE name='coordination_dev_secrets'"
        ).fetchall()
    assert rows == []


def test_policy_change_during_signature_wait_denies(
    consent: Consent, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = secret_policy.load()
    changed = {**original, "revoked": [consent.payload["receipt_id"]]}
    calls = iter([original, changed])
    monkeypatch.setattr(secret_policy, "load", lambda: next(calls))
    with pytest.raises(BusError):
        consent.reserve()
