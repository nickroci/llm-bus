"""Run checkpoints and cancellation handoffs, not a task or scheduling service.

Beads remains the task history. These local records bind reconciliation to exact
claims and retain bounded outcomes. Callers serialize writes with BEGIN IMMEDIATE.
"""

# ruff: noqa: ISC004 - adjacent SQL fragments form table definitions
# Lifetime and provider readers defer recovery imports to avoid startup cycles.
# pylint: disable=cyclic-import,clean-code-commented-out-code

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from llm_bus.coordination import decisions, run_actions, runs
from llm_bus.coordination.authority import authorize
from llm_bus.coordination.models import REPOSITORY, Approval, Direction
from llm_bus.errors import BusError
from llm_bus.routing import routing_scope

if TYPE_CHECKING:
    from sqlite3 import Connection

MAX_RETRIES = 2
FAILED = "failed"
OPEN = "OPEN"
GIT_HEAD = "HEAD"
NO_PENDING_EFFECTS = "no_pending_effects"
STATES = ("ready", "running", "blocked", FAILED)
DENIED = "Recovery denied: stale claim, evidence or checkpoint"
DEPENDENCY_DENIED = "Recovery dependency outside approved tickets"
RETRY_EXHAUSTED = "Recovery retry unavailable or exhausted"
RETRY_EVIDENCE = "Recovery retry requires unchanged evidence and a read-only operation"
PRIOR_SUBMISSION = "Reconcile prior provider submission; no automatic retry"
NO_CANCELLATION = "Handoff needs explicit cancellation and no pending effects"
PRIOR_ATTEMPT = "Prior merge attempt exists; reconcile provider outcome before retry"
COMPLETION_DENIED = "Completion denied: every approved ticket needs its confirmed PR proof"


def create_tables(db: Connection) -> None:
    for statement in (
        "CREATE TABLE IF NOT EXISTS coordination_run_checkpoints ("
        "run_id TEXT NOT NULL,ticket TEXT NOT NULL,revision INTEGER NOT NULL,"
        "payload TEXT NOT NULL,PRIMARY KEY(run_id,ticket))",
        "CREATE TABLE IF NOT EXISTS coordination_run_handoffs ("
        "handoff_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,ticket TEXT NOT NULL,"
        "prior_worker TEXT NOT NULL,branch TEXT NOT NULL,claimed_at TEXT NOT NULL,"
        "evidence TEXT NOT NULL,replacement TEXT,transferred_at TEXT)",
        "CREATE TABLE IF NOT EXISTS coordination_run_merges ("
        "message_id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,ticket TEXT NOT NULL,"
        "payload TEXT NOT NULL,status TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS coordination_run_reviews ("
        "record_id TEXT NOT NULL,run_id TEXT NOT NULL,ticket TEXT NOT NULL,"
        "payload TEXT NOT NULL,PRIMARY KEY(run_id,record_id))",
    ):
        db.execute(statement)


def observe(worktree: str, *, pr: object = None, repository: object = None) -> dict[str, object]:
    """Read the worktree and optional provider PR without submitting an operation."""
    path = str(Path(worktree).resolve())
    try:

        def git(*args: str) -> str:
            return subprocess.run(  # noqa: S603 - fixed git read verbs
                ["git", "-C", path, *args],  # noqa: S607 - local Git on PATH
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()

        evidence: dict[str, object] = {
            "worktree": path,
            "scope": routing_scope(path),
            "branch": git("symbolic-ref", "--short", GIT_HEAD),
            "head_sha": git("rev-parse", GIT_HEAD),
            "clean": not git("status", "--porcelain"),
        }
        if pr is not None:
            # pylint: disable=import-outside-toplevel
            from llm_bus.coordination.github_merge import observe_pr  # noqa: PLC0415

            if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
                raise BusError("Recovery needs an exact GitHub repository")
            found = observe_pr(path, repository, pr)
            evidence["pr"] = {"number": pr, "repository": repository, **found}
    except (OSError, subprocess.SubprocessError) as error:
        raise BusError("Recovery needs a readable attached Git worktree") from error
    return evidence


def _evidence(record: dict[str, object], branch: str, evidence: dict[str, object]) -> None:
    _location(record, branch, evidence)
    if evidence.get("clean") is not True:
        raise BusError(
            "Recovery requires a clean worktree in the exact claimed branch and repository"
        )


def _location(record: dict[str, object], branch: str, evidence: dict[str, object]) -> None:
    if (evidence.get("scope"), evidence.get("branch")) != (record["scope"], branch):
        raise BusError("Recovery requires the exact claimed branch and repository")


def checkpoint(
    db: Connection,
    record: dict[str, object],
    caller: str,
    args: dict[str, object],
    evidence: dict[str, object],
) -> dict[str, object]:
    """CAS progress. Reordered wakes cannot overwrite a newer checkpoint."""
    # pylint: disable=too-many-locals
    create_tables(db)
    run_id, ticket = str(args["run_id"]), str(args["ticket"])
    active = runs.require_active(db, record, caller, run_id=run_id, ticket=ticket)
    worker = str(args["worker"])
    branch, claimed_at = run_actions.claim(db, record, worker, ticket)
    _location(record, branch, evidence)
    revision = int(str(args["revision"]))
    prior = _checkpoint(db, run_id, ticket)
    if revision != (0 if prior is None else prior["revision"]):
        raise BusError(DENIED)
    dependencies = cast("list[str]", args.get("depends_on") or [])
    if ticket in dependencies or any(
        item not in cast("list[str]", active["tickets"]) for item in dependencies
    ):
        raise BusError(DEPENDENCY_DENIED)
    state = str(args["state"])
    if state not in STATES:
        raise BusError("Invalid recovery state")
    if state == "ready":
        _evidence(record, branch, evidence)
    payload: dict[str, object] = {
        "worker": worker,
        "branch": branch,
        "claimed_at": claimed_at,
        "state": state,
        "dependencies": dependencies,
        "evidence": evidence,
        "attempts": 0 if prior is None else prior["attempts"],
        "reason": str(args.get("reason") or "none"),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    _save(db, run_id, ticket, revision + 1, payload)
    return {"ticket": ticket, "revision": revision + 1, **payload}


def _checkpoint(db: Connection, run_id: str, ticket: str) -> dict[str, object] | None:
    row = cast(
        "tuple[int, str] | None",
        db.execute(
            "SELECT revision,payload FROM coordination_run_checkpoints WHERE run_id=? AND ticket=?",
            (run_id, ticket),
        ).fetchone(),
    )
    return (
        None
        if row is None
        else {"revision": row[0], **cast("dict[str, object]", json.loads(row[1]))}
    )


def _save(
    db: Connection, run_id: str, ticket: str, revision: int, payload: dict[str, object]
) -> None:
    db.execute(
        "INSERT INTO coordination_run_checkpoints VALUES (?,?,?,?) "
        "ON CONFLICT(run_id,ticket) DO UPDATE SET "
        "revision=excluded.revision,payload=excluded.payload",
        (run_id, ticket, revision, json.dumps(payload)),
    )


def retry(
    db: Connection,
    record: dict[str, object],
    caller: str,
    args: dict[str, object],
    evidence: dict[str, object],
) -> dict[str, object]:
    """Authorize only a bounded read-only retry; never replay an external mutation."""
    create_tables(db)
    run_id, ticket = str(args["run_id"]), str(args["ticket"])
    runs.require_active(db, record, caller, run_id=run_id, ticket=ticket)
    prior = _checkpoint(db, run_id, ticket)
    if prior is None or prior["revision"] != int(str(args["revision"])):
        raise BusError(DENIED)
    if prior["state"] != FAILED or int(str(prior["attempts"])) >= MAX_RETRIES:
        raise BusError(RETRY_EXHAUSTED)
    if run_actions.claim(db, record, str(prior["worker"]), ticket) != (
        prior["branch"],
        prior["claimed_at"],
    ):
        raise BusError(DENIED)
    _evidence(record, str(prior["branch"]), evidence)
    if evidence != prior["evidence"] or args.get("read_only") is not True:
        raise BusError(RETRY_EVIDENCE)
    # A prior submission, even an unconfirmed one, excludes automatic retry.
    if (
        db.execute(
            "SELECT 1 FROM coordination_run_merges WHERE run_id=? AND ticket=?", (run_id, ticket)
        ).fetchone()
        is not None
    ):
        raise BusError(PRIOR_SUBMISSION)
    prior["attempts"] = int(str(prior["attempts"])) + 1
    prior["state"] = "ready"
    revision = int(str(prior.pop("revision"))) + 1
    _save(db, run_id, ticket, revision, prior)
    return {"ticket": ticket, "revision": revision, **prior, "replay_authority": "read-only"}


def cancellation(
    db: Connection,
    record: dict[str, object],
    worker: str,
    args: dict[str, object],
    evidence: dict[str, object],
) -> dict[str, object]:
    """Worker explicitly accepts cancellation and promises no outstanding effects."""
    create_tables(db)
    run_id, ticket = str(args["run_id"]), str(args["ticket"])
    runs.require_active(db, record, str(record["coordinator"]), run_id=run_id, ticket=ticket)
    branch, claimed_at = run_actions.claim(db, record, worker, ticket)
    _evidence(record, branch, evidence)
    if args.get(NO_PENDING_EFFECTS) is not True:
        raise BusError(NO_CANCELLATION)
    _pending_effects(db, run_id, ticket)
    _prior_pr(db, record, ticket, evidence)
    handoff_id = str(uuid4())
    db.execute(
        "INSERT INTO coordination_run_handoffs VALUES (?,?,?,?,?,?,?,NULL,NULL)",
        (handoff_id, run_id, ticket, worker, branch, claimed_at, json.dumps(evidence)),
    )
    return {"handoff_id": handoff_id, "ticket": ticket, "status": "cancellation_accepted"}


def _pending_effects(db: Connection, run_id: str, ticket: str) -> None:
    if db.execute(
        "SELECT 1 FROM coordination_run_merges WHERE run_id=? AND ticket=? AND status<>'succeeded'",
        (run_id, ticket),
    ).fetchone():
        raise BusError("Handoff blocked by unknown provider outcome")
    tables = {
        row[0]
        for row in cast(
            "list[tuple[str]]",
            db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
        )
    }
    if (
        "coordination_dev_secrets" in tables
        and db.execute(
            "SELECT 1 FROM coordination_dev_secrets WHERE json_extract(payload,'$.run_id')=? "
            "AND json_extract(payload,'$.ticket')=? AND status='unknown'",
            (run_id, ticket),
        ).fetchone()
    ):
        raise BusError("Handoff blocked by unknown development secret outcome")


def _prior_pr(
    db: Connection, record: dict[str, object], ticket: str, evidence: dict[str, object]
) -> None:
    gate = cast(
        "tuple[int, str, str] | None",
        db.execute(
            "SELECT pr,head_sha,base FROM coordination_gate_records WHERE scope=? AND project_id=? "
            "AND generation=? AND ticket=? ORDER BY rowid DESC LIMIT 1",
            (record["scope"], record["project_id"], record["generation"], ticket),
        ).fetchone(),
    )
    if gate is not None:
        number, _head, base = gate
        pr = cast("dict[str, object]", evidence.get("pr") or {})
        if (
            pr.get("number"),
            pr.get("headRefOid"),
            pr.get("headRefName"),
            pr.get("baseRefName"),
            pr.get("state"),
        ) != (number, evidence["head_sha"], evidence["branch"], base, OPEN):
            raise BusError(
                "Handoff requires current open PR evidence on the reconciled worktree head"
            )


def verified_grant(
    db: Connection, record: dict[str, object], worker: str, *, required_grant: str | None = None
) -> str:
    """A live enrolled local worker's currently authorized clarification grant."""
    for (grant_id,) in cast(
        "list[tuple[str]]",
        db.execute(
            "SELECT grant_id FROM coordination_grants WHERE scope=? AND project_id=? "
            "AND recipient=? AND (? IS NULL OR grant_id=?)",
            (record["scope"], record["project_id"], worker, required_grant, required_grant),
        ).fetchall(),
    ):
        if authorize(
            db,
            sender=str(record["coordinator"]),
            recipient=worker,
            scope=str(record["scope"]),
            direction=Direction(
                1, str(record["project_id"]), str(record["generation"]), grant_id, "clarify"
            ),
            now=datetime.now(UTC),
        ).verified:
            return grant_id
    raise BusError("Handoff requires an enrolled, live replacement with current consent and grant")


def handoff(
    db: Connection,
    record: dict[str, object],
    caller: str,
    args: dict[str, object],
    evidence: dict[str, object],
) -> dict[str, object]:
    """One atomic current-claim transfer after explicit worker cancellation."""
    # pylint: disable=too-many-locals
    create_tables(db)
    row = cast(
        "tuple[str, str, str, str, str, str, str | None] | None",
        db.execute(
            "SELECT run_id,ticket,prior_worker,branch,claimed_at,evidence,replacement "
            "FROM coordination_run_handoffs WHERE handoff_id=?",
            (args["handoff_id"],),
        ).fetchone(),
    )
    if row is None:
        raise BusError(DENIED)
    run_id, ticket, prior, branch, claimed_at, saved, _replacement = row
    if _replacement is not None:
        raise BusError(DENIED)
    runs.require_active(db, record, caller, run_id=run_id, ticket=ticket)
    if evidence != json.loads(saved) or run_actions.claim(db, record, prior, ticket) != (
        branch,
        claimed_at,
    ):
        raise BusError(DENIED)
    _evidence(record, branch, evidence)
    _pending_effects(db, run_id, ticket)
    _prior_pr(db, record, ticket, evidence)
    replacement = str(args["worker"])
    if (
        replacement == prior
        or db.execute(
            "SELECT 1 FROM coordination_claims WHERE address=?", (replacement,)
        ).fetchone()
    ):
        raise BusError("Replacement already owns work or is the prior worker")
    grant_id = verified_grant(db, record, replacement)
    now = datetime.now(UTC).isoformat()
    carried = decisions.transfer(
        db,
        record,
        caller,
        run_id=run_id,
        ticket=ticket,
        prior=prior,
        replacement=replacement,
        grant_id=grant_id,
        branch=branch,
        claimed_at=now,
        handoff_id=str(args["handoff_id"]),
    )
    changed = db.execute(
        "UPDATE coordination_claims SET address=?,claimed_at=? "
        "WHERE address=? AND scope=? AND project_id=? AND ticket=? AND branch=? AND claimed_at=?",
        (
            replacement,
            now,
            prior,
            record["scope"],
            record["project_id"],
            ticket,
            branch,
            claimed_at,
        ),
    )
    if changed.rowcount != 1:
        raise BusError(DENIED)
    db.execute(
        "UPDATE coordination_run_handoffs SET replacement=?,transferred_at=? WHERE handoff_id=?",
        (replacement, now, args["handoff_id"]),
    )
    checkpointed = _checkpoint(db, run_id, ticket)
    if checkpointed is not None:
        revision = int(str(checkpointed.pop("revision"))) + 1
        checkpointed.update(
            worker=replacement, claimed_at=now, state="blocked", reason="fresh_gates_required"
        )
        _save(db, run_id, ticket, revision, checkpointed)
    return {
        "handoff_id": args["handoff_id"],
        "worker": replacement,
        "decisions": carried,
        "fresh_gates_required": True,
    }


def summary(db: Connection, record: dict[str, object], run: dict[str, object]) -> dict[str, object]:
    """Reconcile durable metadata with current claims and reports, without external IO."""
    # pylint: disable=too-many-locals
    create_tables(db)
    run_id = str(run["run_id"])
    results = [
        dict(cast("dict[str, object]", json.loads(payload)), status=status)
        for payload, status in cast(
            "list[tuple[str, str]]",
            db.execute(
                "SELECT payload,status FROM coordination_run_merges "
                "WHERE run_id=? ORDER BY message_id",
                (run_id,),
            ).fetchall(),
        )
    ]
    proof = cast(
        "tuple[str] | None",
        db.execute("SELECT kind FROM coordination_run_proofs WHERE run_id=?", (run_id,)).fetchone(),
    )
    kind = "reviewed-pr" if proof is None else str(proof[0])
    reviews = [
        cast("dict[str, object]", json.loads(row[0]))
        for row in cast(
            "list[tuple[str]]",
            db.execute(
                "SELECT payload FROM coordination_run_reviews WHERE run_id=? ORDER BY rowid",
                (run_id,),
            ).fetchall(),
        )
    ]
    results.extend({**review, "status": "reviewed"} for review in reviews)
    completed_tickets = {
        str(result["ticket"])
        for result in results
        if result["status"] == "succeeded"
        or (kind == "reviewed-pr" and result["status"] == "reviewed")
    }
    checkpoints: list[dict[str, object]] = []
    ready: list[str] = []
    blockers: list[dict[str, object]] = []
    for ticket in cast("list[str]", run["tickets"]):
        prior = _checkpoint(db, run_id, ticket)
        if ticket in completed_tickets:
            continue
        if prior is None:
            blockers.append({"ticket": ticket, "reason": "checkpoint_required"})
            continue
        reason = None
        try:
            if run_actions.claim(db, record, str(prior["worker"]), ticket) != (
                prior["branch"],
                prior["claimed_at"],
            ):
                reason = "stale_claim"
        except BusError:
            reason = "stale_claim"
        if not set(cast("list[str]", prior["dependencies"])) <= completed_tickets:
            reason = reason or "dependencies"
        if prior["state"] in {"blocked", FAILED}:
            reason = reason or str(prior["reason"])
        if reason:
            blockers.append({"ticket": ticket, "reason": reason})
        elif prior["state"] == "ready" and run.get(runs.ACTIVE):
            ready.append(ticket)
        checkpoints.append({"ticket": ticket, **prior})
    handoffs = [
        dict(
            zip(
                ("handoff_id", "ticket", "prior_worker", "replacement", "transferred_at"),
                row,
                strict=True,
            )
        )
        for row in cast(
            "list[tuple[object, ...]]",
            db.execute(
                "SELECT handoff_id,ticket,prior_worker,replacement,transferred_at "
                "FROM coordination_run_handoffs WHERE run_id=? ORDER BY rowid",
                (run_id,),
            ).fetchall(),
        )
    ]
    return {
        "beads_workspace": record["beads_workspace"],
        "memory_ticket": record["memory_ticket"],
        "results": results,
        "checkpoints": checkpoints,
        "ready": ready,
        "blockers": blockers,
        "handoffs": handoffs,
        "decisions": decisions.run_history(db, record, run_id),
        "end_reason": run.get("end_reason"),
        "completion_proof": kind,
        "complete": len(completed_tickets) == len(cast("list[str]", run["tickets"])),
    }


def reserve_merge(
    db: Connection, approval: Approval, message_id: int, required: list[str], run_id: str
) -> None:
    """Commit unknown before submission so interruption never authorizes blind replay."""
    create_tables(db)
    if db.execute(
        "SELECT 1 FROM coordination_run_merges WHERE message_id=?", (message_id,)
    ).fetchone():
        raise BusError(PRIOR_ATTEMPT)
    if db.execute(
        "SELECT 1 FROM coordination_run_merges m JOIN coordination_runs old ON old.run_id=m.run_id "
        "JOIN coordination_runs current ON current.run_id=? AND current.scope=old.scope "
        "AND current.project_id=old.project_id WHERE json_extract(m.payload,'$.repository')=? "
        "AND json_extract(m.payload,'$.pr')=?",
        (run_id, approval.repository, approval.pr),
    ).fetchone():
        raise BusError(PRIOR_ATTEMPT)
    payload = {
        "ticket": approval.ticket,
        "pr": approval.pr,
        "head_sha": approval.head_sha,
        "base": approval.base,
        "branch": approval.branch,
        "repository": approval.repository,
        "record_id": approval.record_id,
        "required_checks": required,
        "independent_review": True,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    db.execute(
        "INSERT INTO coordination_run_merges VALUES (?,?,?,?,?)",
        (message_id, run_id, approval.ticket, json.dumps(payload), "unknown"),
    )


def complete(db: Connection, record: dict[str, object], caller: str, run_id: str) -> None:
    run = runs.require_active(db, record, caller, run_id=run_id)
    if not summary(db, record, run)["complete"]:
        raise BusError(COMPLETION_DENIED)
