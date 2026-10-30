"""Transport errors remain bounded without requiring external network services."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, cast

import pytest

import llm_bus_collab_transport as transport
from llm_bus_collab_daemon import Daemon
from llm_bus_collab_state import CollabState, Invite, encode_invite, grant
from llm_bus_store import Store

if TYPE_CHECKING:
    from pathlib import Path

    import iroh


class StalledConnection:
    closed: bool = False

    def remote_id(self) -> str:
        return "peer"

    async def open_bi(self) -> StalledConnection:
        return self

    def send(self) -> StalledConnection:
        return self

    def recv(self) -> StalledConnection:
        return self

    async def write_all(self, _body: bytes) -> None:
        pass

    async def finish(self) -> None:
        pass

    async def read_to_end(self, _limit: int) -> bytes:
        await asyncio.Event().wait()
        return b""

    def close(self, _code: int, _reason: bytes) -> None:
        self.closed = True


class Endpoint:
    connection: StalledConnection
    connected: bool

    def __init__(self, connection: StalledConnection) -> None:
        self.connection = connection
        self.connected = False

    def id(self) -> str:
        return "local"

    async def connect(self, _address: object, _alpn: bytes) -> StalledConnection:
        self.connected = True
        return self.connection


class Ticket:
    def endpoint_addr(self) -> Ticket:
        return self

    def id(self) -> str:
        return "peer"


def test_connected_peer_cannot_stall_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def parse(_text: str) -> iroh.EndpointTicket:
        return cast("iroh.EndpointTicket", cast("object", Ticket()))

    connection = StalledConnection()
    endpoint = Endpoint(connection)
    client = transport.Transport(cast("iroh.Endpoint", cast("object", endpoint)))
    monkeypatch.setattr(transport, "_ticket", parse)
    monkeypatch.setattr(transport, "REQUEST_TIMEOUT_SECONDS", 0.01)

    async def request() -> None:
        await asyncio.wait_for(client.request("ticket", {"type": "msg"}), timeout=5)

    with pytest.raises(transport.TransportError, match="timed out"):
        asyncio.run(request())
    assert endpoint.connected
    assert connection.closed


def test_malformed_ticket_fails_join_without_stopping_pump(tmp_path: Path) -> None:
    state = CollabState(tmp_path / "collab")
    state.set_identity("bob", tmp_path / "unused-signing-key")
    invite = Invite(
        v=0,
        ticket="invalid-ticket",
        nonce="n" * 32,
        host_user="alice",
        joiner_user="bob",
        repository_name="repo",
        grant="monitor",
        expires_at="2099-01-01T00:00:00.000Z",
    )
    join_id = state.add_join(encode_invite(invite), str(tmp_path), None)
    state.approve_join(join_id, grant("monitor"))
    daemon = Daemon(state, Store(tmp_path / "bus.sqlite3"))
    endpoint = Endpoint(StalledConnection())
    daemon.transport = transport.Transport(cast("iroh.Endpoint", cast("object", endpoint)))

    async def scenario() -> None:
        task = asyncio.create_task(daemon.pump())
        try:
            await asyncio.sleep(0)
            assert state.join(join_id)[3] == "failed"
            assert not task.done()
            assert not endpoint.connected
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
