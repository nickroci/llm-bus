"""Remote transport state alongside the native SQLite mailbox."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from llm_bus.coordination import stalls
from llm_bus.diagnostics.synchronization import (
    SYNC_ERRORS,
    SYNC_READ_SECONDS,
)
from llm_bus.errors import BusError
from llm_bus.remote import admission, discovery_store, monitor, outbox, scheduling

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime
    from sqlite3 import Connection

    from llm_bus.coordination.models import Approval, Direction
    from llm_bus.mailbox.models import SenderContext
    from llm_bus.mailbox.store import Store
    from llm_bus.remote.models import Delivery, Envelope, Peer


class RemoteStore:
    """Atomic UUID deduplication and durable outbox; native message IDs stay integers."""

    # This facade owns mailbox-bound transport state and its durable synchronization metadata.
    # pylint: disable=too-many-public-methods

    store: Store
    connection: str

    RETRY_DELAYS: tuple[int, ...] = (5, 10, 20, 40, 80, 160, 300)
    READ_SECONDS: int = SYNC_READ_SECONDS
    PEER_SECONDS: int = 60
    HORIZON_DAYS: int = 30
    SYNC_ERRORS: frozenset[str | None] = SYNC_ERRORS | {None}

    def sync_snapshot(self, kind: str, key: str) -> dict[str, object]:
        return scheduling.sync_snapshot(self, kind, key)

    def work_due(  # pylint: disable=clean-code-boolean-flag-argument
        self, kind: str, key: str, now: datetime, *, force: bool = False
    ) -> bool:
        return scheduling.work_due(self, kind, key, now, force=force)

    def record_sync_result(
        self, kind: str, key: str, now: datetime, *, error_code: str | None
    ) -> None:
        return scheduling.record_sync_result(self, kind, key, now, error_code=error_code)

    def observe_acceptance(self, key: str, now: datetime) -> None:
        return scheduling.observe_acceptance(self, key, now)

    def peer_snapshot(self, repo_id: str, now: datetime) -> dict[str, object]:
        return scheduling.peer_snapshot(self, repo_id, now)

    def record_peer_result(self, repo_id: str, now: datetime, *, error_code: str | None) -> None:
        return scheduling.record_peer_result(self, repo_id, now, error_code=error_code)

    def sleep_seconds(self, now: datetime) -> float:
        return scheduling.sleep_seconds(self, now)

    def __init__(self, store: Store, connection: str, *, db: Connection | None = None) -> None:
        self.store = store
        self.connection = connection
        if db is not None:
            self._bind(db)
        else:
            with store.transaction() as transaction:
                self._bind(transaction)

    def _bind(self, db: Connection) -> None:
        db.execute("INSERT OR IGNORE INTO remote_binding VALUES (1, ?)", (self.connection,))
        row = cast(
            "tuple[str]", db.execute("SELECT connection FROM remote_binding WHERE id=1").fetchone()
        )
        if row[0] != self.connection:
            raise BusError("This mailbox is bound to another relay/machine; use another LLM_BUS_DB")

    def sessions(
        self, repositories: dict[str, str] | None = None
    ) -> list[dict[str, str | int | None]]:
        return discovery_store.sessions(self, repositories)

    def publication_payloads(self) -> dict[str, str]:
        return discovery_store.publication_payloads(self)

    def record_session_role(self, address: str, revision: int, *, child: bool) -> int | None:
        return discovery_store.record_session_role(self, address, revision, child=child)

    def published(self, key: str) -> bool:
        return discovery_store.published(self, key)

    def record_publication(self, key: str, payload: str) -> None:
        return discovery_store.record_publication(self, key, payload)

    def cache(self, repo_id: str, peers: list[Peer]) -> None:
        return discovery_store.cache(self, repo_id, peers)

    def peers(self) -> list[Peer]:
        return discovery_store.peers(self)

    def enqueue(  # noqa: PLR0913  # pylint: disable=too-many-arguments
        self,
        sender: SenderContext,
        recipient: str,
        repo_id: str,
        body: str,
        *,
        scope: str | None = None,
        reply_to: int | None = None,
        direction: Direction | None = None,
        approval: Approval | None = None,
    ) -> dict[str, object]:
        return outbox.enqueue(
            self,
            sender,
            recipient,
            repo_id,
            body,
            scope=scope,
            reply_to=reply_to,
            direction=direction,
            approval=approval,
        )

    def outgoing(self, *, after: int = 0) -> list[tuple[Envelope, str]]:
        return outbox.outgoing(self, after=after)

    def outgoing_pages(self) -> Iterator[list[tuple[Envelope, str]]]:
        return outbox.outgoing_pages(self)

    def source_activity(self, remote_id: str) -> str:
        return outbox.source_activity(self, remote_id)

    def source_scope(self, remote_id: str) -> str | None:
        return outbox.source_scope(self, remote_id)

    def statuses(self) -> list[dict[str, object]]:
        return outbox.statuses(self)

    def set_state(self, remote_id: str, state: str) -> None:
        outbox.set_state(self, remote_id, state)
        if state == "acknowledged":
            stalls.schedule_remote(self.store, remote_id)

    def admit_message(self, item: Delivery, scope: str) -> tuple[str, int | None, bool] | None:
        result = admission.admit_message(self, item, scope)
        if result is not None and result[1] is not None:
            stalls.schedule_messages(self.store, [result[1]])
        return result

    @staticmethod
    def monitor_history(
        store: Store, *, repository: str | None, before: int | None = None, limit: int = 100
    ) -> list[tuple[int, str, str, Delivery]]:
        return monitor.monitor_history(store, repository=repository, before=before, limit=limit)

    def acknowledgements(self) -> list[tuple[str, str]]:
        return admission.acknowledgements(self)

    def permits_ack(self, remote_id: str) -> bool:
        return admission.permits_ack(self, remote_id)

    def mark_acknowledged(self, remote_id: str) -> None:
        return admission.mark_acknowledged(self, remote_id)
