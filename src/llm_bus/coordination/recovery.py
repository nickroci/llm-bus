"""Bounded retries, verified handoffs and PR outcomes for unattended runs.

Callers serialize writes with BEGIN IMMEDIATE. Beads owns task history.
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

from llm_bus.coordination import decisions, run_actions, runs, work_state
from llm_bus.coordination.authority import authorize
from llm_bus.coordination.models import REPOSITORY, Approval, Direction
from llm_bus.errors import BusError
from llm_bus.routing import routing_scope

if TYPE_CHECKING:
    from sqlite3 import Connection

MAX_RETRIES = 2
OPEN = "OPEN"
GIT_HEAD = "HEAD"
NO_PENDING_EFFECTS = "no_pending_effects"
DENIED = "Handoff denied: stale claim, cancellation or evidence"
RETRY_EXHAUSTED = "Recovery retry unavailable or exhausted"
RETRY_READ_ONLY = "Recovery retry requires a read-only operation"
PRIOR_SUBMISSION = "Reconcile prior provider submission; no automatic retry"
NO_CANCELLATION = "Handoff needs explicit cancellation and no pending effects"
PRIOR_ATTEMPT = "Prior merge attempt exists; reconcile provider outcome before retry"
COMPLETION_DENIED = "Completion denied: every approved ticket needs its confirmed PR proof"


def create_tables(db: Connection) -> None:
    for statement in (
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


def _ensure_retry_counts(db: Connection) -> None:
    """Import valid legacy counts once. Leave other legacy data inert for rollback."""
    tables = work_state.table_names(db)
    if "coordination_run_retries" in tables:
        return
    db.execute(
        "CREATE TABLE coordination_run_retries ("
        "run_id TEXT NOT NULL,ticket TEXT NOT NULL,attempts INTEGER NOT NULL,"
        "PRIMARY KEY(run_id,ticket))"
    )
    if "coordination_run_checkpoints" in tables:
        db.execute(
            "INSERT INTO coordination_run_retries "
            "SELECT run_id,ticket,min(attempts,?) FROM ("
            "SELECT run_id,ticket,CASE WHEN json_valid(payload) "
            "THEN json_extract(payload,'$.attempts') END AS attempts "
            "FROM coordination_run_checkpoints) "
            "WHERE typeof(attempts)='integer' AND attempts>0",
            (MAX_RETRIES,),
        )


def retry(
    db: Connection,
    record: dict[str, object],
    caller: str,
    args: dict[str, object],
) -> dict[str, object]:
    """Count a read-only retry for the current claim without performing the operation."""
    create_tables(db)
    run_id, ticket = str(args["run_id"]), str(args["ticket"])
    runs.require_active(db, record, caller, run_id=run_id, ticket=ticket)
    run_actions.claim(db, record, str(args["worker"]), ticket)
    if args.get("read_only") is not True:
        raise BusError(RETRY_READ_ONLY)
    # A prior submission, even an unconfirmed one, excludes automatic retry.
    if (
        db.execute(
            "SELECT 1 FROM coordination_run_merges WHERE run_id=? AND ticket=?", (run_id, ticket)
        ).fetchone()
        is not None
    ):
        raise BusError(PRIOR_SUBMISSION)
    _ensure_retry_counts(db)
    row = cast(
        "tuple[int] | None",
        db.execute(
            "INSERT INTO coordination_run_retries VALUES (?,?,1) "
            "ON CONFLICT(run_id,ticket) DO UPDATE SET attempts=attempts+1 "
            "WHERE attempts<? RETURNING attempts",
            (run_id, ticket, MAX_RETRIES),
        ).fetchone(),
    )
    if row is None:
        raise BusError(RETRY_EXHAUSTED)
    return {"ticket": ticket, "attempts": row[0], "replay_authority": "read-only"}


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
    if (
        "coordination_dev_secrets" in work_state.table_names(db)
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
    return {
        "handoff_id": args["handoff_id"],
        "worker": replacement,
        "decisions": carried,
        "fresh_gates_required": True,
    }


def summary(db: Connection, record: dict[str, object], run: dict[str, object]) -> dict[str, object]:
    """Summarize retained PR proofs, handoffs and decisions without external IO."""
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
        "handoffs": handoffs,
        "decisions": decisions.run_history(db, record, run_id),
        "end_reason": run.get("end_reason"),
        "completion_proof": kind,
        "complete": set(cast("list[str]", run["tickets"])) <= completed_tickets,
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
