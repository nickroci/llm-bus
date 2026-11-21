"""Snapshots ownership."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from llm_bus.coordination.stalls import observe_message
from llm_bus.errors import BusError
from llm_bus.mailbox.eligibility import cutoff, remote_retained, report_open
from llm_bus.reports.lifecycle import TERMINAL
from llm_bus.reports.protocol import (
    Report,
    parse_report,
    report_deadline,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from llm_bus.policy.config import Config
    from llm_bus.remote.models import Envelope

if TYPE_CHECKING:
    from llm_bus.reports.store import Reports


def deadline(self: Reports, remote_id: str, value: object, retention: object = None) -> None:
    expires = report_deadline(value)
    bound = report_deadline(retention) if retention is not None else None
    with self.remote.store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        if not remote_retained(db, remote_id):
            return
        prior = cast(
            "tuple[str, str | None] | None",
            db.execute(
                "SELECT expires_at,retention_expires_at FROM remote_report_windows WHERE id=?",
                (remote_id,),
            ).fetchone(),
        )
        if prior and prior[0] != expires:
            raise BusError("Relay report deadline changed")
        if prior and prior[1] is not None and bound is not None and prior[1] != bound:
            raise BusError("Relay retention deadline changed")
        db.execute(
            "INSERT INTO remote_report_windows(id,expires_at,retention_expires_at) VALUES (?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET retention_expires_at="
            "COALESCE(remote_report_windows.retention_expires_at,excluded.retention_expires_at)",
            (remote_id, expires, bound),
        )


def pollable_pages(self: Reports, *, now: datetime | None = None) -> Iterator[list[Envelope]]:
    instant = now or datetime.now(UTC)
    after = 0
    while True:
        page = pollable_page(self, after, instant)
        if not page:
            return
        after = page[-1][0]
        result: list[Envelope] = []
        for _local_id, payload, expires in page:
            envelope = cast("Envelope", json.loads(payload))
            current = self.snapshot(envelope["id"])
            if datetime.fromisoformat(report_deadline(expires)) > instant and (
                current is None or current["status"] not in TERMINAL
            ):
                result.append(envelope)
        yield result


def pollable_page(self: Reports, after: int, instant: datetime) -> list[tuple[int, str, str]]:
    with self.remote.store.transaction() as db:
        return cast(
            "list[tuple[int, str, str]]",
            db.execute(
                "SELECT o.local_id,o.envelope,w.expires_at FROM remote_outbox o "
                "JOIN remote_report_windows w ON w.id=o.id JOIN messages m ON m.id=o.local_id "
                "WHERE o.state<>'queued_local' AND o.local_id>? AND m.retention_at>? "
                "AND (w.retention_expires_at IS NULL OR "
                "julianday(w.retention_expires_at)>julianday(?)) "
                "ORDER BY o.local_id LIMIT 100",
                (after, cutoff(instant), instant.isoformat()),
            ).fetchall(),
        )


def snapshot(self: Reports, remote_id: str) -> Report | None:
    with self.remote.store.transaction() as db:
        now = datetime.now(UTC)
        row = cast(
            "tuple[str] | None",
            db.execute(
                "SELECT r.payload FROM remote_reports r JOIN remote_outbox o ON o.id=r.id "
                "JOIN messages m ON m.id=o.local_id LEFT JOIN remote_report_windows w ON w.id=r.id "
                "WHERE r.id=? AND m.retention_at>? "
                "AND (w.expires_at IS NULL OR julianday(w.expires_at)>julianday(?)) "
                "AND (w.retention_expires_at IS NULL OR "
                "julianday(w.retention_expires_at)>julianday(?))",
                (remote_id, cutoff(now), now.isoformat(), now.isoformat()),
            ).fetchone(),
        )
    return parse_report(cast("object", json.loads(row[0])), remote_id) if row else None


def receive(self: Reports, envelope: Envelope, value: object, config: Config) -> None:
    report = parse_report(value, envelope["id"])
    with self.remote.store.transaction() as db:
        binding = cast(
            "tuple[str, str | None] | None",
            db.execute(
                "SELECT o.envelope,m.sender_scope FROM remote_outbox o "
                "JOIN messages m ON m.id=o.local_id WHERE o.id=?",
                (envelope["id"],),
            ).fetchone(),
        )
    repo = next((r for r in config.repositories if r.repo_id == envelope["repo_id"]), None)
    if (
        binding is None
        or json.loads(binding[0]) != envelope
        or repo is None
        or binding[1] != repo.scope
        or config.connection != self.remote.connection
    ):
        raise BusError("Work report message binding conflicts")
    peer = config.peer(envelope["repo_id"], envelope["recipient"].split("/", 1)[0])
    if (
        peer is None
        or not peer.receive_reports
        or (report["status"] is not None and report["status"] not in peer.receive_reports)
    ):
        raise BusError("Local capability policy: report receiving is disabled")
    with self.remote.store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        if not report_open(db, envelope["id"]):
            return
        row = cast(
            "tuple[str] | None",
            db.execute(
                "SELECT payload FROM remote_reports WHERE id=?", (envelope["id"],)
            ).fetchone(),
        )
        previous = parse_report(cast("object", json.loads(row[0])), envelope["id"]) if row else None
        if previous and report != previous:
            stale = report["revision"] <= previous["revision"]
            invalid_transition = previous["status"] in TERMINAL or (
                report["status"] == "received" and previous["status"] not in (None, "received")
            )
            if stale or invalid_transition:
                raise BusError("Work report revision conflict")
        db.execute(
            "INSERT INTO remote_reports VALUES (?,?) ON CONFLICT(id) "
            "DO UPDATE SET payload=excluded.payload",
            (envelope["id"], json.dumps(report)),
        )
        if report != previous:
            local_id = cast(
                "tuple[int]",
                db.execute(
                    "SELECT local_id FROM remote_outbox WHERE id=?", (envelope["id"],)
                ).fetchone(),
            )[0]
            observe_message(db, local_id, now=datetime.now(UTC))
