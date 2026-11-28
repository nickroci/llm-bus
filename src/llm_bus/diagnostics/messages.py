"""Read-only, body-free message and synchronization diagnostics."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, cast

from llm_bus.coordination import work_state
from llm_bus.diagnostics.synchronization import SYNC_KINDS, synchronization_snapshot
from llm_bus.errors import BusError
from llm_bus.mailbox.eligibility import cutoff
from llm_bus.mailbox.retention_schema import normalize_age

if TYPE_CHECKING:
    from pathlib import Path

WAKE_STATUSES = frozenset({"requested", "coalesced", "failed", "unknown", "unsupported"})  # pylint: disable=clean-code-business-policy-literal
WAKE_ADAPTERS = {
    "codex queue": "codex_queue",
    "Claude inbox socket": "claude_inbox_socket",
    "host wake": "host_wake",
    "wake coordinator": "wake_coordinator",
    "none": "none",
}
READ_TRANSACTION = "BEGIN"
SQLITE_MAX_MESSAGE_ID = 2**63 - 1


def safe_wake_reason(status: str | None, via: str | None, reason: str | None) -> str | None:
    """Diagnostics contain fixed categories, never adapter stderr or exception text."""
    if reason is None:
        return None
    outcome = status if status in WAKE_STATUSES else "unknown"
    return f"wake_{outcome}:{WAKE_ADAPTERS.get(via or '', 'unknown_adapter')}"


def message_status(path: Path, message_id: int) -> dict[str, object]:
    """Return body-free local diagnostics, independent of live policy and transport."""
    if isinstance(message_id, bool) or message_id <= 0:
        raise BusError("Message ID must be positive")
    if message_id > SQLITE_MAX_MESSAGE_ID:
        raise BusError("Unknown message ID")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute(READ_TRANSACTION)
        columns = {
            row[1]
            for row in cast(
                "list[tuple[object, str]]", db.execute("PRAGMA table_info(messages)").fetchall()
            )
        }
        names = (
            "id",
            "sender",
            "recipient",
            "reply_to",
            "sent_at",
            "acknowledged_at",
            "wake_status",
            "wake_via",
            "wake_reason",
            "wake_checked_at",
        )
        fields = ",".join(name if name in columns else f"NULL AS {name}" for name in names)
        # Only constant projection names enter SQL; the message ID remains a parameter.
        query = f"SELECT {fields} FROM messages WHERE id=?"  # noqa: S608
        row = cast(
            "tuple[object, ...] | None",
            db.execute(query, (message_id,)).fetchone(),
        )
        if row is None:
            raise BusError("Unknown message ID")
        message: dict[str, object] = dict(zip(names, row, strict=True))
        if normalize_age(cast("str", message["sent_at"])) <= cutoff():
            raise BusError("Unknown message ID")
        return _message_status(db, message)


def _message_status(db: sqlite3.Connection, message: dict[str, object]) -> dict[str, object]:
    tables = work_state.table_names(db)
    key = message["id"]
    outgoing = (
        cast(
            "tuple[str, str] | None",
            db.execute("SELECT id,state FROM remote_outbox WHERE local_id=?", (key,)).fetchone(),
        )
        if "remote_outbox" in tables
        else None
    )
    incoming = (
        cast(
            "tuple[str] | None",
            db.execute("SELECT id FROM remote_imports WHERE local_id=?", (key,)).fetchone(),
        )
        if "remote_imports" in tables
        else None
    )
    remote_id = outgoing[0] if outgoing else incoming[0] if incoming else None
    sync = synchronization_snapshot(db, "status" if outgoing else "receipt", remote_id or "")
    synchronization = {
        name: sync[name]
        for name in (
            "last_attempt_at",
            "last_success_at",
            "next_attempt_at",
            "error_code",
            "polling_suspended_at",
            "stale",
        )
    }
    if remote_id and "remote_sync_work" in tables:
        scheduled = cast(
            "list[tuple[str, str, int | None]]",
            db.execute(
                "SELECT kind,key,NULL FROM remote_sync_work "
                "WHERE key=? AND kind<>'report_submit' ORDER BY kind",
                (remote_id,),
            ).fetchall(),
        )
        if "remote_report_submissions" in tables:
            scheduled.extend(
                cast(
                    "list[tuple[str, str, int | None]]",
                    db.execute(
                        "SELECT w.kind,w.key,s.expected_revision "
                        "FROM remote_report_submissions s JOIN remote_sync_work w "
                        "ON w.kind='report_submit' AND w.key=s.id||':'||s.expected_revision "
                        "WHERE s.id=? ORDER BY s.expected_revision",
                        (remote_id,),
                    ).fetchall(),
                )
            )
        operations = [
            {
                "kind": kind,
                **({"expected_revision": revision} if revision is not None else {}),
                **synchronization_snapshot(db, kind, operation_key),
            }
            for kind, operation_key, revision in scheduled
            if kind in SYNC_KINDS
        ]
        if operations:
            synchronization["operations"] = operations
    coverage = (
        cast(
            "tuple[str] | None",
            db.execute("SELECT status FROM wake_coverage WHERE message_id=?", (key,)).fetchone(),
        )
        if "wake_coverage" in tables
        else None
    )
    retry = (
        cast(
            "tuple[str | None] | None",
            db.execute(
                "SELECT next_attempt_at FROM wake_dispatch WHERE recipient=?",
                (message["recipient"],),
            ).fetchone(),
        )
        if "wake_dispatch" in tables
        else None
    )
    return {
        "id": key,
        "remote_id": remote_id,
        "sender": message["sender"],
        "recipient": message["recipient"],
        "reply_to": message["reply_to"],
        "transport": {
            "status": outgoing[1]
            if outgoing
            else "delivered_local"
            if incoming
            else "stored_local",
            "source": "relay_snapshot" if outgoing and outgoing[1] != "queued_local" else "local",
            "observed_at": sync["last_success_at"]
            if outgoing
            else None
            if incoming
            else message["sent_at"],
        },
        "wake": _wake_status(message, coverage, retry),
        "acknowledged_at": message["acknowledged_at"],
        "work_report": work_state.work_report(
            db,
            tables,
            cast("int", key),
            remote_id=outgoing[0] if outgoing else None,
            imported=incoming is not None,
        ),
        "synchronization": synchronization,
    }


def _wake_status(
    message: dict[str, object],
    coverage: tuple[str] | None,
    retry: tuple[str | None] | None,
) -> dict[str, object]:
    status = cast("str | None", message["wake_status"])
    via = cast("str | None", message["wake_via"])
    return {
        "status": status if status in WAKE_STATUSES else None,
        "via": via if via in WAKE_ADAPTERS else None,
        "reason": safe_wake_reason(status, via, cast("str | None", message["wake_reason"])),
        "checked_at": message["wake_checked_at"],
        "coverage_status": coverage[0] if coverage and coverage[0] in WAKE_STATUSES else None,
        "retry_at": retry[0] if retry and message["acknowledged_at"] is None else None,  # pylint: disable=clean-code-business-policy-literal
    }
