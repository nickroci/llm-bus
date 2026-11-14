from __future__ import annotations

from contextlib import AbstractContextManager, closing
from typing import TYPE_CHECKING

from llm_bus.coordination import stalls
from llm_bus.diagnostics.messages import message_status
from llm_bus.mailbox import agents, connection, history, labels, pending, writes
from llm_bus.mailbox.models import MAX_BATCH, Agent, HistoryMessage, Message, SenderContext
from llm_bus.routing import routing_scope

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from llm_bus.coordination.models import Approval, Direction
    from llm_bus.hosts.processes import HostProcess


class Store:  # pylint: disable=too-many-public-methods
    """Durable messages; inbox reads leave items pending until acknowledged."""

    path: Path

    @classmethod
    def read_only(cls, path: Path) -> Store:
        """Open retained diagnostics without mailbox creation, migration, or chmod."""
        store = cls.__new__(cls)
        store.path = path
        return store

    def message_status(self, message_id: int) -> dict[str, object]:
        """Return body-free local diagnostics, independent of live policy and transport."""
        return message_status(self.path, message_id)

    def __init__(self, path: Path) -> None:
        self.path = path
        connection.initialize(self.path)

    def _connect(self) -> sqlite3.Connection:
        return connection.connect(self.path)

    def transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        return connection.transaction(self._connect)

    def register(  # noqa: PLR0913 - explicit optional registration metadata
        self,
        kind: str,
        session_id: str,
        project: str,
        *,
        messaging_socket: str | None = None,
        label: str | None = None,
        host: HostProcess | None = None,
    ) -> str:
        """Upsert session address without deleting its pending messages."""
        # pylint: disable=too-many-arguments
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            return agents.register(
                db,
                kind,
                session_id,
                project,
                messaging_socket=messaging_socket,
                label=label,
                host=host,
            )

    def resolve_label(self, label: str, project: str) -> str:
        """Expire and resolve a label atomically in the selected routing scope."""
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            agents.expire_idle_agents(db)
            return labels.lookup(db, routing_scope(project), label)

    def messaging_socket(self, agent_address: str) -> str | None:
        """Read the native endpoint reported by the recipient's own hook."""
        with closing(self._connect()) as db:
            return agents.read_messaging_socket(db, agent_address)

    def end_session(self, kind: str, session_id: str) -> None:
        """Mark a registered session closed without consuming its pending messages."""
        with self.transaction() as db:
            return agents.end_session(db, kind, session_id)

    def agents(self, project: str | None = None) -> list[Agent]:
        """List sessions active within the last day; presence is not guaranteed."""
        with self.transaction() as db:
            return agents.agents(db, project)

    def all_agents(self) -> list[Agent]:
        """List all registered sessions, including ended ones."""
        with self.transaction() as db:
            return agents.all_agents(db)

    def is_ended(self, agent_address: str) -> bool:
        """Whether a session ended or has been idle for a day."""
        with self.transaction() as db:
            return agents.is_ended(db, agent_address)

    def send(  # noqa: PLR0913 - supported optional send metadata
        self,
        sender_context: SenderContext,
        recipient: str,
        body: str,
        *,
        cross_folder: bool = False,
        reply_to: int | None = None,
        direction: Direction | None = None,
        approval: Approval | None = None,
    ) -> Message:
        """Atomically register sender, check folder, and persist message."""
        # pylint: disable=too-many-arguments
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            sender = agents.upsert_agent(
                db,
                sender_context.kind,
                sender_context.session_id,
                sender_context.project,
                routing_scope(sender_context.project),
            )
            sent = writes.send_in_transaction(
                db,
                sender,
                recipient,
                body,
                cross_folder=cross_folder,
                reply_to=reply_to,
                direction=direction,
                approval=approval,
            )
        if reply_to is not None:
            stalls.schedule_messages(self, [sent["id"]])
        return sent

    def send_in_transaction(  # noqa: PLR0913  # pylint: disable=too-many-arguments
        self,
        db: sqlite3.Connection,
        sender: str,
        recipient: str,
        body: str,
        *,
        cross_folder: bool = False,
        reply_to: int | None = None,
        direction: Direction | None = None,
        approval: Approval | None = None,
    ) -> Message:
        """Persist for an already registered sender without changing its activity or lifecycle."""
        return writes.send_in_transaction(
            db,
            sender,
            recipient,
            body,
            cross_folder=cross_folder,
            reply_to=reply_to,
            direction=direction,
            approval=approval,
        )

    @staticmethod
    def validate_reply(
        db: sqlite3.Connection,
        parent_id: int | None,
        sender: tuple[str, str],
        recipient: tuple[str, str],
    ) -> None:
        """Bind a retained parent to the same participants and their individual scopes."""
        return writes.validate_reply(db, parent_id, sender, recipient)

    @staticmethod
    def request_dispatch(db: sqlite3.Connection, recipient: str) -> None:
        """Count a new native inbox member in the same insertion transaction."""
        return writes.request_dispatch(db, recipient)

    def pending_members(self, recipient: str) -> list[tuple[int, str]]:
        """Read permitted pending identities across bounded pages, without message bodies."""
        with closing(self._connect()) as db:
            return pending.pending_members(db, recipient)

    def guidance_needed(self, recipient: str, version: int, *, reset: bool) -> bool:
        """Remember only guidance format, never permissions or peer policy."""
        with self.transaction() as db:
            return agents.guidance_needed(db, recipient, version, reset=reset)

    def message_acknowledged(self, recipient: str, message_id: int, sender: str) -> bool:
        """Verify a wake's full identity against durable acknowledgement state."""
        with closing(self._connect()) as db:
            return writes.message_acknowledged(db, recipient, message_id, sender)

    def pending_allowed(self, message_id: int) -> bool:
        """Recheck remote pending exposure against fresh machine-owned policy."""
        with closing(self._connect()) as db:
            return pending.pending_allowed(db, message_id)

    def deliver_collab(self, sender: str, recipient: str, body: str) -> int:
        """Store daemon-delivered collaboration text for a registered local session.

        The sender row is marked ended so it never appears in `list` and cannot be addressed.
        """
        with self.transaction() as db:
            return writes.deliver_collab(db, sender, recipient, body)

    def inbox(self, recipient: str, limit: int = MAX_BATCH) -> list[Message]:
        """Read pending messages without consuming them."""
        with closing(self._connect()) as db:
            return pending.inbox(db, recipient, limit)

    def history(  # noqa: PLR0913 - independent public history filters compose here
        self,
        *,
        folder: str | None,
        repository: str | None = None,
        before: int | None = None,
        after: int | None = None,
        limit: int = MAX_BATCH,
        conversation: int | None = None,
        peer: str | None = None,
    ) -> list[HistoryMessage]:
        """Read stored messages, newest page first or new items after a cursor."""
        # Optional public filters preserve the existing keyword API and compose one SQL query.
        # pylint: disable=too-many-arguments,too-many-locals
        with closing(self._connect()) as db:
            return history.history(
                db,
                folder=folder,
                repository=repository,
                before=before,
                after=after,
                limit=limit,
                conversation=conversation,
                peer=peer,
            )

    def pending_count(self, recipient: str, *, limit: int | None = None) -> int:
        """Count permitted pending metadata, optionally stopping at a saturation limit."""
        with closing(self._connect()) as db:
            return pending.pending_count(db, recipient, limit=limit)

    def acknowledge(self, recipient: str, ids: list[int]) -> int:
        """Acknowledge only own messages; reject mixed or unknown batches atomically."""
        with self.transaction() as db:
            count = writes.acknowledge(db, recipient, ids)
        stalls.schedule_messages(self, ids)
        return count
