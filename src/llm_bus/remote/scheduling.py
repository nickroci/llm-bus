"""Scheduling ownership."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from llm_bus.diagnostics.synchronization import (
    synchronization_snapshot,
)
from llm_bus.errors import BusError
from llm_bus.mailbox.eligibility import remote_retained

if TYPE_CHECKING:
    from llm_bus.remote.store import RemoteStore


SUSPEND_POLLING_QUERY = (
    "UPDATE remote_sync_work SET polling_suspended_at=COALESCE(polling_suspended_at,?) "
    "WHERE kind=? AND key=?"
)


NEXT_SYNC_QUERY = (
    "SELECT MIN(next_attempt_at) FROM (SELECT next_attempt_at FROM remote_sync_work "
    "WHERE next_attempt_at>? AND polling_suspended_at IS NULL UNION ALL "
    "SELECT next_attempt_at FROM remote_peer_sync WHERE next_attempt_at>?)"
)


def sync_snapshot(self: RemoteStore, kind: str, key: str) -> dict[str, object]:
    with self.store.transaction() as db:
        return synchronization_snapshot(db, kind, key)


def work_due(self: RemoteStore, kind: str, key: str, now: datetime, *, force: bool = False) -> bool:  # pylint: disable=clean-code-boolean-flag-argument
    snapshot = self.sync_snapshot(kind, key)
    accepted = cast("str | None", snapshot["first_accepted_at"])
    if (
        kind == "status"
        and accepted
        and now >= datetime.fromisoformat(accepted) + timedelta(days=self.HORIZON_DAYS)
    ):
        with self.store.transaction() as db:
            db.execute(
                SUSPEND_POLLING_QUERY,
                (now.isoformat(), kind, key),
            )
        return force
    due = cast("str | None", snapshot["next_attempt_at"])
    return force or due is None or now >= datetime.fromisoformat(due)


def record_sync_result(
    self: RemoteStore, kind: str, key: str, now: datetime, *, error_code: str | None
) -> None:
    self.sync_snapshot(kind, key)
    if error_code not in self.SYNC_ERRORS:
        raise BusError("Invalid synchronization error code")
    with self.store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        message_key = key.rsplit(":", 1)[0] if kind == "report_submit" else key
        if not remote_retained(db, message_key, now=now):
            return
        row = cast(
            "tuple[int] | None",
            db.execute(
                "SELECT attempts FROM remote_sync_work WHERE kind=? AND key=?", (kind, key)
            ).fetchone(),
        )
        attempts = (row[0] if row else 0) + 1 if error_code else 0
        delay = (
            self.RETRY_DELAYS[min(attempts - 1, len(self.RETRY_DELAYS) - 1)]
            if error_code
            else (self.READ_SECONDS if kind in {"status", "report_read"} else 0)
        )
        db.execute(
            "INSERT INTO remote_sync_work(kind,key,attempts,next_attempt_at,last_attempt_at,"
            "last_success_at,error_code) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(kind,key) DO UPDATE SET "
            "attempts=excluded.attempts,next_attempt_at=excluded.next_attempt_at,"
            "last_attempt_at=excluded.last_attempt_at,last_success_at="
            "COALESCE(excluded.last_success_at,remote_sync_work.last_success_at),"
            "error_code=excluded.error_code",
            (
                kind,
                key,
                attempts,
                (now + timedelta(seconds=delay)).isoformat(),
                now.isoformat(),
                None if error_code else now.isoformat(),
                error_code,
            ),
        )


def observe_acceptance(self: RemoteStore, key: str, now: datetime) -> None:
    with self.store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        if not remote_retained(db, key, now=now):
            return
        db.execute(
            "INSERT INTO remote_sync_work(kind,key,first_accepted_at) VALUES ('status',?,?) "
            "ON CONFLICT(kind,key) DO UPDATE SET first_accepted_at="
            "COALESCE(remote_sync_work.first_accepted_at,excluded.first_accepted_at)",
            (key, now.isoformat()),
        )


def peer_snapshot(self: RemoteStore, repo_id: str, now: datetime) -> dict[str, object]:
    columns: tuple[str, ...] = (
        "last_attempt_at",
        "last_success_at",
        "next_attempt_at",
        "error_code",
    )
    with self.store.transaction() as db:
        row = cast(
            "tuple[str | None, ...] | None",
            db.execute(
                "SELECT last_attempt_at,last_success_at,next_attempt_at,error_code "
                "FROM remote_peer_sync WHERE repo_id=?",
                (repo_id,),
            ).fetchone(),
        )
    result: dict[str, object] = (
        dict(zip(columns, row, strict=True))
        if row
        else dict.fromkeys(columns, cast("object", None))
    )
    success = cast("str | None", result["last_success_at"])
    result["stale"] = (
        success is None
        or result["error_code"] is not None
        or now >= datetime.fromisoformat(success) + timedelta(seconds=self.PEER_SECONDS)
    )
    return result


def record_peer_result(
    self: RemoteStore, repo_id: str, now: datetime, *, error_code: str | None
) -> None:
    if error_code not in self.SYNC_ERRORS:
        raise BusError("Invalid synchronization error code")
    with self.store.transaction() as db:
        db.execute(
            "INSERT INTO remote_peer_sync VALUES (?,?,?,?,?) "
            "ON CONFLICT(repo_id) DO UPDATE SET "
            "last_attempt_at=excluded.last_attempt_at,last_success_at="
            "COALESCE(excluded.last_success_at,remote_peer_sync.last_success_at),"
            "next_attempt_at=excluded.next_attempt_at,error_code=excluded.error_code",
            (
                repo_id,
                now.isoformat(),
                None if error_code else now.isoformat(),
                (now + timedelta(seconds=self.PEER_SECONDS)).isoformat(),
                error_code,
            ),
        )


def sleep_seconds(self: RemoteStore, now: datetime) -> float:
    with self.store.transaction() as db:
        row = cast(
            "tuple[str | None]",
            db.execute(
                NEXT_SYNC_QUERY,
                (now.isoformat(), now.isoformat()),
            ).fetchone(),
        )
    delay = (datetime.fromisoformat(row[0]) - now).total_seconds() if row[0] else 5
    return max(1, min(30, delay))
