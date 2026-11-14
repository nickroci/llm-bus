"""Snapshots ownership."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from llm_bus.coordination.stalls import observe_message
from llm_bus.errors import BusError
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


def deadline(self: Reports, remote_id: str, value: object) -> None:
    expires = report_deadline(value)
    with self.remote.store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = cast(
            "tuple[str] | None",
            db.execute(
                "SELECT expires_at FROM remote_report_windows WHERE id=?", (remote_id,)
            ).fetchone(),
        )
        if prior and prior[0] != expires:
            raise BusError("Relay report deadline changed")
        db.execute("INSERT OR IGNORE INTO remote_report_windows VALUES (?,?)", (remote_id, expires))


def pollable_pages(self: Reports, *, now: datetime | None = None) -> Iterator[list[Envelope]]:
    instant = now or datetime.now(UTC)
    after = 0
    while True:
        page = pollable_page(self, after)
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


def pollable_page(self: Reports, after: int) -> list[tuple[int, str, str]]:
    with self.remote.store.transaction() as db:
        return cast(
            "list[tuple[int, str, str]]",
            db.execute(
                "SELECT o.local_id,o.envelope,w.expires_at FROM remote_outbox o "
                "JOIN remote_report_windows w ON w.id=o.id "
                "WHERE o.state<>'queued_local' AND o.local_id>? "
                "ORDER BY o.local_id LIMIT 100",
                (after,),
            ).fetchall(),
        )


def snapshot(self: Reports, remote_id: str) -> Report | None:
    with self.remote.store.transaction() as db:
        row = cast(
            "tuple[str] | None",
            db.execute("SELECT payload FROM remote_reports WHERE id=?", (remote_id,)).fetchone(),
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
