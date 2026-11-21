"""Narrow GitHub action boundary for an unattended, record-bound merge.

Provider queries happen outside the mailbox lock. Authority is reread after those
waits immediately before a head-pinned submission. Native/provider access still
controls callers able to bypass this procedural bus path.
"""

from __future__ import annotations

# Recovery uses deferred provider reads; module initialization remains acyclic.
# pylint: disable=cyclic-import,clean-code-commented-out-code
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from llm_bus.coordination import delivery, recovery, run_actions, store
from llm_bus.coordination import runs as run_lifetime
from llm_bus.coordination.models import REPOSITORY, Approval, timestamp
from llm_bus.errors import BusError
from llm_bus.routing import routing_scope

if TYPE_CHECKING:
    from sqlite3 import Connection

    from llm_bus.mailbox.store import Store

TIMEOUT = 60
GH_REPO_OVERRIDE = "GH_REPO"
GIT_ENV_PREFIX = "GIT_"
OPEN = "OPEN"
APPROVED = "APPROVED"
SUCCESS = "SUCCESS"
MERGED = "MERGED"
CHECKS_DENIED = "Merge denied: required checks missing, pending, failed or unknown"
UNKNOWN_OUTCOME = "Merge outcome unknown; reconcile provider before retry"
UNCONFIRMED_OUTCOME = "Merge not confirmed; reconcile provider before retry"
type CompletionEvidence = dict[str, tuple[str, tuple[Approval, list[str], str]]]


def _executable() -> str:
    executable = shutil.which("gh")
    if executable is None:
        raise BusError("GitHub gate unavailable: gh is required")
    return executable


def _environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key != GH_REPO_OVERRIDE and not key.startswith(GIT_ENV_PREFIX)
    }


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BusError("Merge denied: unknown provider evidence")
    return cast("dict[str, object]", value)


def observe_pr(cwd: str, repository: str, pr: object) -> dict[str, object]:
    """Bounded read used to reconcile a worker's cancellation evidence."""
    local = _object(_gh(cwd, "repo", "view", "--json", "nameWithOwner"))
    if local.get("nameWithOwner") != repository:
        raise BusError("Recovery denied: wrong GitHub repository")
    found = _object(
        _gh(
            cwd,
            "pr",
            "view",
            str(pr),
            "--repo",
            repository,
            "--json",
            "state,headRefOid,headRefName,baseRefName",
        )
    )
    return {key: found.get(key) for key in ("state", "headRefOid", "headRefName", "baseRefName")}


def _array(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise BusError("Merge denied: unknown provider evidence")
    return [_object(item) for item in cast("list[object]", value)]


def _gh(cwd: str, *args: str) -> object:
    try:
        result = subprocess.run(  # noqa: S603 - fixed gh verbs and validated bound fields
            [_executable(), *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT,
            env=_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BusError("GitHub gate unavailable; no merge authority") from error
    if result.returncode:
        # Provider errors can contain sensitive host context; retain bounded status only.
        raise BusError("GitHub gate failed; verify provider access and required evidence")
    try:
        return cast("object", json.loads(result.stdout))
    except ValueError as error:
        raise BusError("GitHub gate returned unknown evidence") from error


def _approval(
    db: Connection, scope: str, label: str, actor: str, message_id: int
) -> tuple[dict[str, object], Approval, list[str]]:
    record = store.project(db, scope, label)
    row = cast(
        "tuple[str] | None",
        db.execute("SELECT recipient FROM messages WHERE id=?", (message_id,)).fetchone(),
    )
    evidence = delivery.verification(db, message_id)
    if (  # pylint: disable=too-many-boolean-expressions
        row != (actor,)
        or evidence is None
        or evidence.get("verified") is not True
        or evidence.get("operation") != "approve-merge"
        or evidence.get("run_id") is None
        or evidence.get("project_id") != record["project_id"]
        or evidence.get("generation") != record["generation"]
    ):
        raise BusError("Merge denied: no current caller-bound unattended approval")
    approval = Approval.parse(evidence.get("approval"), "approve-merge")
    if approval.record_id is None:
        raise BusError("Merge denied: independent gate record required")
    gate = cast(
        "tuple[str] | None",
        db.execute(
            "SELECT checks_json FROM coordination_gate_records WHERE record_id=?",
            (approval.record_id,),
        ).fetchone(),
    )
    checks = cast("object", json.loads(gate[0])) if gate is not None else None
    if (
        not isinstance(checks, list)
        or not checks
        or not all(isinstance(check, str) for check in cast("list[object]", checks))
    ):
        raise BusError("Merge denied: missing gate checks")
    return record, approval, cast("list[str]", checks)


def _pr(cwd: str, repo: str, approval: Approval) -> None:
    pr = _object(
        _gh(
            cwd,
            "pr",
            "view",
            str(approval.pr),
            "--repo",
            repo,
            "--json",
            "state,isDraft,baseRefName,headRefName,headRefOid,reviewDecision,reviews,author",
        )
    )
    if (
        pr.get("state"),
        pr.get("isDraft"),
        pr.get("baseRefName"),
        pr.get("headRefName"),
        pr.get("headRefOid"),
        pr.get("reviewDecision"),
    ) != (OPEN, False, approval.base, approval.branch, approval.head_sha, APPROVED):
        raise BusError("Merge denied: stale PR, head, base or review")
    reviews, author = _array(pr.get("reviews")), _object(pr.get("author"))
    if not isinstance(author.get("login"), str):
        raise BusError("Merge denied: unknown PR author")
    latest: dict[str, dict[str, object]] = {}
    for review in reviews:
        login = _object(review.get("author")).get("login")
        if not isinstance(login, str) or not isinstance(review.get("submittedAt"), str):
            raise BusError("Merge denied: unknown reviewer")
        previous = latest.get(login)
        if previous is None or str(review["submittedAt"]) > str(previous["submittedAt"]):
            latest[login] = review
    if not any(
        login != author.get("login")
        and review.get("state") == APPROVED
        and review.get("commit") == {"oid": approval.head_sha}
        for login, review in latest.items()
    ):
        raise BusError("Merge denied: no current independent review on exact head")


def _live(cwd: str, approval: Approval, required: list[str]) -> str:
    # pylint: disable=too-many-locals
    repository = _object(_gh(cwd, "repo", "view", "--json", "nameWithOwner"))
    repo = repository.get("nameWithOwner")
    if not isinstance(repo, str) or REPOSITORY.fullmatch(repo) is None:
        raise BusError("Merge denied: unsupported GitHub repository")
    if repo != approval.repository:
        raise BusError("Merge denied: wrong GitHub repository")
    _pr(cwd, repo, approval)
    checks = _array(
        _gh(
            cwd,
            "pr",
            "checks",
            str(approval.pr),
            "--repo",
            repo,
            "--required",
            "--json",
            "name,state,bucket",
        )
    )
    if not checks or any(
        check.get("bucket") != "pass"
        or check.get("state") != SUCCESS
        or not isinstance(check.get("name"), str)
        for check in checks
    ):
        raise BusError(CHECKS_DENIED)
    if not set(required) <= {check["name"] for check in checks}:
        raise BusError("Merge denied: gate check does not match required provider checks")
    # Immutable commit evidence prevents attribution of checks to a moved pull request.
    runs = _array(
        _object(
            _gh(cwd, "api", f"repos/{repo}/commits/{approval.head_sha}/check-runs?per_page=100")
        ).get("check_runs")
    )
    statuses = _array(
        _object(
            _gh(cwd, "api", f"repos/{repo}/commits/{approval.head_sha}/status?per_page=100")
        ).get("statuses")
    )
    successes = {
        item.get("name")
        for item in runs
        if item.get("status") == "completed"
        and item.get("conclusion") == "success"
        and item.get("head_sha") == approval.head_sha
        and isinstance(item.get("name"), str)
    }
    successes.update(
        item.get("context")
        for item in statuses
        if item.get("state") == "success" and isinstance(item.get("context"), str)
    )
    if any(check["name"] not in successes for check in checks):
        raise BusError("Merge denied: required checks not green on exact head")
    # Recheck the entire bound PR and review after all preceding provider waits.
    # Head matching at submission cannot pin the base or review state.
    _pr(cwd, repo, approval)
    return repo


def execute(  # noqa: PLR0913 - exact caller, repository and action binding
    mailbox: Store, scope: str, label: str, actor: str, message_id: int, *, cwd: str
) -> dict[str, object]:
    """Reverify after provider waits, then submit only the exact approved head."""
    # pylint: disable=too-many-arguments,too-many-locals
    with mailbox.transaction() as db:
        record, approval, required = _approval(db, scope, label, actor, message_id)
    if routing_scope(cwd) != record["scope"]:
        raise BusError("Merge denied: wrong repository")
    repo = _live(cwd, approval, required)
    with mailbox.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        _record, current, current_required = _approval(db, scope, label, actor, message_id)
        if current != approval or current_required != required:
            raise BusError("Merge denied: approval changed during verification")
        bound = delivery.verification(db, message_id)
        if bound is None or not isinstance(bound.get("run_id"), str):
            raise BusError("Merge denied: missing run binding")
        recovery.reserve_merge(db, approval, message_id, required, str(bound["run_id"]))
    with mailbox.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        _record, current, current_required = _approval(db, scope, label, actor, message_id)
        if current != approval or current_required != required:
            raise BusError("Merge denied: approval changed during verification")
        # The short submission boundary serializes local revocation. A submitted
        # provider operation may finish after revocation; it cannot be recalled.
        try:
            result = subprocess.run(  # noqa: S603 - fixed merge verb, validated bound fields
                [
                    _executable(),
                    "pr",
                    "merge",
                    str(approval.pr),
                    "--repo",
                    repo,
                    "--merge",
                    "--match-head-commit",
                    str(approval.head_sha),
                ],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=TIMEOUT,
                env=_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BusError(UNKNOWN_OUTCOME) from error
        if result.returncode:
            raise BusError(UNCONFIRMED_OUTCOME)
        db.execute(
            "UPDATE coordination_run_merges SET status='submitted' WHERE message_id=?",
            (message_id,),
        )
    return {
        "status": "submitted",
        "pr": approval.pr,
        "head_sha": approval.head_sha,
        "repository": repo,
        "submitted_at": datetime.now(UTC).isoformat(),
    }


def _review_record(  # noqa: PLR0913,PLR0917 - exact result context
    db: Connection,
    record: dict[str, object],
    caller: str,
    run_id: str,
    record_id: str,
    repository: str,
) -> tuple[Approval, list[str], str]:
    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    row = cast(
        "tuple[str, str, str, int, str, str, str, str, str, str] | None",
        db.execute(
            "SELECT ticket,branch,base,pr,head_sha,checks_json,submitted_by,submitted_at,"
            "expires_at,grant_id "
            "FROM coordination_gate_records WHERE record_id=? AND scope=? AND project_id=? "
            "AND generation=? AND status IN ('submitted','accepted')",
            (record_id, record["scope"], record["project_id"], record["generation"]),
        ).fetchone(),
    )
    if row is None:
        raise BusError("Result denied: no current review record")
    ticket, branch, base, pr, head, checks, worker, _submitted, expires, grant_id = row
    run_lifetime.require_active(db, record, caller, run_id=run_id, ticket=ticket)
    recovery.verified_grant(db, record, worker, required_grant=grant_id)
    claimed_branch, claimed_at = run_actions.require_gate(
        db, record, worker, ticket, record_id, run_id=run_id
    )
    if claimed_branch != branch or timestamp(expires) <= datetime.now(UTC):
        raise BusError("Result denied: stale worker review record")
    approval = Approval(ticket, branch, base, expires, pr, head, record_id, repository)
    approval.validate("approve-merge")
    return approval, cast("list[str]", json.loads(checks)), claimed_at


def record_result(  # noqa: PLR0913 - exact native caller and repository
    mailbox: Store, scope: str, label: str, caller: str, args: dict[str, object], *, cwd: str
) -> dict[str, object]:
    """Record exact-head independent review and green CI without granting merge."""
    # pylint: disable=too-many-arguments,too-many-locals
    run_id, record_id, repository = (
        str(args["run_id"]),
        str(args["record_id"]),
        str(args["repository"]),
    )
    with mailbox.transaction() as db:
        record = store.project(db, scope, label)
        before = _review_record(db, record, caller, run_id, record_id, repository)
    approval, required, _claimed = before
    _live(cwd, approval, required)
    with mailbox.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        record = store.project(db, scope, label)
        if _review_record(db, record, caller, run_id, record_id, repository) != before:
            raise BusError("Result denied: evidence changed during verification")
        recovery.create_tables(db)
        payload: dict[str, object] = {
            "ticket": approval.ticket,
            "pr": approval.pr,
            "head_sha": approval.head_sha,
            "record_id": record_id,
            "repository": repository,
            "required_checks": required,
            "independent_review": True,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        db.execute(
            "INSERT INTO coordination_run_reviews VALUES (?,?,?,?) "
            "ON CONFLICT(run_id,record_id) DO UPDATE SET payload=excluded.payload",
            (record_id, run_id, approval.ticket, json.dumps(payload)),
        )
    return {"status": "reviewed", **payload}


def verify_completion(  # noqa: PLR0913 - exact run and native caller
    mailbox: Store, scope: str, label: str, caller: str, run_id: str, *, cwd: str
) -> CompletionEvidence:
    """Refresh still-open reviewed PR proofs before the final run transaction."""
    # pylint: disable=too-many-arguments,too-many-locals
    with mailbox.transaction() as db:
        record = store.project(db, scope, label)
        run = run_lifetime.require_active(db, record, caller, run_id=run_id)
        progress = recovery.summary(db, record, run)
        if not progress["complete"]:
            raise BusError(recovery.COMPLETION_DENIED)
        results = cast("list[dict[str, object]]", progress["results"])
        merged = {str(item["ticket"]) for item in results if item["status"] == "succeeded"}
        current = {
            str(row[0])
            for row in cast(
                "list[tuple[str]]",
                db.execute(
                    "SELECT record_id FROM coordination_gate_records "
                    "WHERE scope=? AND project_id=? "
                    "AND generation=? AND status IN ('submitted','accepted')",
                    (record["scope"], record["project_id"], record["generation"]),
                ).fetchall(),
            )
        }
        selected = {
            str(item["ticket"]): item
            for item in results
            if item["status"] == "reviewed"
            and str(item["record_id"]) in current
            and str(item["ticket"]) not in merged
        }
        if not set(cast("list[str]", run["tickets"])) <= merged | set(selected):
            raise BusError(recovery.COMPLETION_DENIED)
        evidence = {
            str(item["record_id"]): (
                str(item["repository"]),
                _review_record(
                    db, record, caller, run_id, str(item["record_id"]), str(item["repository"])
                ),
            )
            for item in selected.values()
        }
    for _repository, (approval, required, _claimed) in evidence.values():
        _live(cwd, approval, required)
    return evidence


def validate_completion(
    db: Connection,
    record: dict[str, object],
    caller: str,
    run_id: str,
    evidence: CompletionEvidence,
) -> None:
    """Recheck every provider-verified binding under the final completion lock."""
    for record_id, (repository, before) in evidence.items():
        if _review_record(db, record, caller, run_id, record_id, repository) != before:
            raise BusError("Completion denied: evidence changed during verification")


def reconcile(  # noqa: PLR0913 - exact native caller and prior action
    mailbox: Store, scope: str, label: str, caller: str, message_id: int, *, cwd: str
) -> dict[str, object]:
    """Read provider outcome without resubmitting an interrupted merge."""
    # pylint: disable=too-many-arguments
    with mailbox.transaction() as db:
        recovery.create_tables(db)
        record = store.project(db, scope, label)
        row = cast(
            "tuple[str, str] | None",
            db.execute(
                "SELECT run_id,payload FROM coordination_run_merges WHERE message_id=?",
                (message_id,),
            ).fetchone(),
        )
        if row is None:
            raise BusError("No prior merge attempt to reconcile")
        run_id, encoded = row
        payload = cast("dict[str, object]", json.loads(encoded))
        run_lifetime.require_history(db, record, caller, run_id, str(payload["ticket"]))
    pr = _object(
        _gh(
            cwd,
            "pr",
            "view",
            str(payload["pr"]),
            "--repo",
            str(payload["repository"]),
            "--json",
            "state,headRefOid,headRefName,baseRefName",
        )
    )
    if (pr.get("state"), pr.get("headRefOid"), pr.get("headRefName"), pr.get("baseRefName")) != (
        MERGED,
        payload["head_sha"],
        payload["branch"],
        payload["base"],
    ):
        raise BusError("Merge outcome remains unconfirmed; retain blocker without replay")
    with mailbox.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        record = store.project(db, scope, label)
        run_lifetime.require_history(db, record, caller, run_id, str(payload["ticket"]))
        updated = db.execute(
            "UPDATE coordination_run_merges SET status='succeeded' "
            "WHERE message_id=? AND run_id=? AND payload=?",
            (message_id, run_id, encoded),
        )
        if updated.rowcount != 1:
            raise BusError("Merge reconciliation denied: attempt changed during verification")
    return {"message_id": message_id, "status": "succeeded", **payload}
