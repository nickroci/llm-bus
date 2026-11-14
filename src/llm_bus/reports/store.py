"""Strict typed work reports, separate from transport and native inboxes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from llm_bus.coordination import stalls
from llm_bus.remote.store import RemoteStore
from llm_bus.reports import snapshots, submissions

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from llm_bus.mailbox.store import Store
    from llm_bus.policy.config import Config
    from llm_bus.remote.models import Delivery, Envelope
    from llm_bus.reports.protocol import Report


class Reports:
    """Durable submissions and validated snapshots; no wake or inbox operations."""

    remote: RemoteStore

    def __init__(self, store: Store, connection: str) -> None:
        self.remote = RemoteStore(store, connection)

    def deadline(self, remote_id: str, value: object) -> None:
        return snapshots.deadline(self, remote_id, value)

    def pollable_pages(self, *, now: datetime | None = None) -> Iterator[list[Envelope]]:
        return snapshots.pollable_pages(self, now=now)

    def statuses(self) -> list[dict[str, object]]:
        return self.remote.statuses()

    def imported(self, local_id: int, native: str | None = None) -> Delivery:
        return submissions.imported(self, local_id, native)

    def permitted(self, item: Delivery, status: str) -> bool:
        return submissions.permitted(self, item, status)

    def enqueue(
        self, local_id: int, native: str, status: object, reason: object
    ) -> dict[str, object]:
        return submissions.enqueue(self, local_id, native, status, reason)

    def pending(self, *, after: int = 0) -> list[tuple[int, dict[str, object]]]:
        return submissions.pending(self, after=after)

    def pending_items(self) -> Iterator[tuple[int, dict[str, object]]]:
        return submissions.pending_items(self)

    def accepted(self, submission: dict[str, object], value: object) -> None:
        return submissions.accepted(self, submission, value)

    def snapshot(self, remote_id: str) -> Report | None:
        return snapshots.snapshot(self, remote_id)

    def receive(self, envelope: Envelope, value: object, config: Config) -> None:
        snapshots.receive(self, envelope, value, config)
        stalls.schedule_remote(self.remote.store, envelope["id"])
