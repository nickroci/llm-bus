"""Durable recipient leases and explicit membership for best-effort native wakes."""

from __future__ import annotations

import shutil
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple, cast

from llm_bus_store import safe_wake_reason
from llm_bus_wake import WakeResult, wake

if TYPE_CHECKING:
    from llm_bus_store import Store

LEASE_SECONDS = 30
GATHER_SECONDS = 0.025
MAX_HOST_ATTEMPTS = 2
RETRY_BASE_SECONDS = 5
RETRY_MAX_SECONDS = 300
COALESCED: WakeResult = {
    "status": "coalesced",
    "via": "wake coordinator",
    "reason": "pending work covered by an active lease, successful request, or scheduled retry",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _capability(store: Store, recipient: str) -> str:
    return (
        store.messaging_socket(recipient) or "no socket"
        if recipient.startswith("claude:")
        else shutil.which("codex") or "no binary"
    )


class Claim(NamedTuple):
    token: str
    attempts: int


class Batch(NamedTuple):
    generation: int
    members: list[tuple[int, str]]


def _claim(store: Store, recipient: str, *, trigger_id: int | None) -> Claim | None:
    now = _now().isoformat()
    capability = _capability(store, recipient)
    with store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        row = cast(
            "tuple[str | None, str | None, str | None, int, str | None, int, int] | None",
            db.execute(
                "SELECT token,lease_until,next_attempt_at,attempts,capability,"
                "requested_generation,dispatched_generation "
                "FROM wake_dispatch WHERE recipient=?",
                (recipient,),
            ).fetchone(),
        )
        if row is None:
            return None
        token, lease, due, attempts, unsupported, requested, dispatched = row
        if token is not None and lease is not None and lease > now:
            return None
        if due is not None and due > now:
            return None
        # Both local and remote admissions durably advance the requested generation.
        if unsupported == capability and trigger_id is None and requested == dispatched:
            return None
        if (
            db.execute(
                "SELECT 1 FROM messages m WHERE m.recipient=? AND m.acknowledged_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM wake_coverage c "
                "WHERE c.message_id=m.id AND c.status='requested') LIMIT 1",
                (recipient,),
            ).fetchone()
            is None
        ):
            return None
        token = str(uuid.uuid4())
        lease = (_now() + timedelta(seconds=LEASE_SECONDS)).isoformat()
        db.execute(
            "UPDATE wake_dispatch SET token=?,lease_until=?,next_attempt_at=? WHERE recipient=?",
            (token, lease, lease, recipient),
        )
        return Claim(token, attempts)


def _snapshot(store: Store, recipient: str, claim: Claim) -> Batch:
    # Read generation before membership: arrivals during this scan remain visible to finalization.
    with store.transaction() as db:
        row = cast(
            "tuple[int] | None",
            db.execute(
                "SELECT requested_generation FROM wake_dispatch WHERE recipient=? AND token=?",
                (recipient, claim.token),
            ).fetchone(),
        )
    if row is None:
        return Batch(0, [])
    members = store.pending_members(recipient)
    with store.transaction() as db:
        covered = {
            item[0]
            for item in cast(
                "list[tuple[int]]",
                db.execute(
                    "SELECT c.message_id FROM wake_coverage c JOIN messages m ON m.id=c.message_id "
                    "WHERE m.recipient=? AND c.status='requested'",
                    (recipient,),
                ).fetchall(),
            )
        }
    return Batch(row[0], [member for member in members if member[0] not in covered])


def _native(store: Store, recipient: str, members: list[tuple[int, str]]) -> WakeResult:
    key, sender = members[0]
    try:
        if recipient.startswith("claude:"):
            return wake(recipient, key, sender, claude_socket=store.messaging_socket(recipient))
        return wake(recipient, key, sender)
    except Exception as error:  # pylint: disable=broad-exception-caught
        return {
            "status": "unknown",
            "via": "host wake",
            "reason": f"wake adapter raised {type(error).__name__}",
        }


def _owns_lease(store: Store, recipient: str, claim: Claim) -> bool:
    with store.transaction() as db:
        return (
            db.execute(
                "SELECT 1 FROM wake_dispatch WHERE recipient=? AND token=? AND lease_until>?",
                (recipient, claim.token, _now().isoformat()),
            ).fetchone()
            is not None
        )


def _finish(
    store: Store,
    recipient: str,
    claim: Claim,
    batch: Batch,
    result: WakeResult,
) -> bool | None:
    now = _now()
    status = result["status"]
    attempts = 0 if status in {"requested", "coalesced"} else claim.attempts + 1
    next_attempt = (
        None
        if status in {"requested", "coalesced"}
        else (
            now
            + timedelta(
                seconds=min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (1 << min(attempts - 1, 6)))
            )
        ).isoformat()
    )
    capability = _capability(store, recipient) if status == "unsupported" else None
    with store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        row = cast(
            "tuple[int] | None",
            db.execute(
                "SELECT requested_generation FROM wake_dispatch WHERE recipient=? AND token=?",
                (recipient, claim.token),
            ).fetchone(),
        )
        if row is None:
            return None  # An expired leader cannot overwrite a newer leader's coverage.
        for key, _ in batch.members:
            db.execute(
                "INSERT INTO wake_coverage(message_id,attempt_token,status,checked_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(message_id) DO UPDATE SET attempt_token=excluded.attempt_token,"
                "status=excluded.status,checked_at=excluded.checked_at",
                (key, claim.token, status, now.isoformat()),
            )
            db.execute(
                "UPDATE messages SET wake_status=?,wake_via=?,wake_reason=?,wake_checked_at=? "
                "WHERE id=?",
                (
                    status,
                    result["via"],
                    safe_wake_reason(status, result["via"], result.get("reason")),
                    now.isoformat(),
                    key,
                ),
            )
        arrived = row[0] != batch.generation
        db.execute(
            "UPDATE wake_dispatch SET token=NULL,lease_until=NULL,dispatched_generation=?,"
            "next_attempt_at=?,attempts=?,capability=? WHERE recipient=? AND token=?",
            (
                batch.generation,
                None if arrived else next_attempt,
                attempts,
                capability,
                recipient,
                claim.token,
            ),
        )
    return arrived


def dispatch_pending(store: Store, recipient: str, *, trigger_id: int | None = None) -> WakeResult:
    """Request at most two host turns; coalescing never asserts receipt or handling."""
    result: WakeResult = COALESCED.copy()
    for _ in range(MAX_HOST_ATTEMPTS):
        claim = _claim(store, recipient, trigger_id=trigger_id)
        if claim is None:
            claim = _claim(store, recipient, trigger_id=trigger_id)
        if claim is None:
            # The write lock serializes claiming and release; a lost race retains durable work.
            return result
        time.sleep(GATHER_SECONDS)
        generation, members = _snapshot(store, recipient, claim)
        ended = store.is_ended(recipient)
        # Every selected member must still have permission immediately before the native call.
        selected = {key for key, _ in members}
        members = [member for member in store.pending_members(recipient) if member[0] in selected]
        if not _owns_lease(store, recipient, claim):
            return COALESCED.copy()
        if ended:
            result = {
                "status": "failed",
                "via": "host wake",
                "reason": "recipient session ended; message remains pending",
            }
        else:
            result = _native(store, recipient, members) if members else COALESCED.copy()
        arrived = _finish(store, recipient, claim, Batch(generation, members), result)
        if arrived is None:
            return COALESCED.copy()
        if not arrived:
            return result
    return result


def dispatch_due(store: Store) -> None:
    """Recover native pending work each bridge pass, independent of relay replay."""
    with store.transaction() as db:
        recipients = cast(
            "list[tuple[str]]",
            db.execute(
                "SELECT DISTINCT m.recipient FROM messages m JOIN wake_dispatch d "
                "ON d.recipient=m.recipient WHERE m.acknowledged_at IS NULL "
                "AND m.recipient NOT IN (SELECT address FROM remote_proxies)"
            ).fetchall(),
        )
    for (recipient,) in recipients:
        dispatch_pending(store, recipient)
