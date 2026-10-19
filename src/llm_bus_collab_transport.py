"""Point-to-point frames over iroh: one JSON request and one JSON response per connection.

Everything llm-bus relies on is the authenticated remote endpoint ID; the rest of the
collaboration protocol is transport-independent.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, cast

import iroh

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

ALPN = b"llm-bus/collab/0"
MAX_FRAME_BYTES = 256 * 1024
CONNECT_TIMEOUT_SECONDS = 30
LOCAL_ONLY_ENV = "LLM_BUS_COLLAB_LOCAL_ONLY"

type Frame = dict[str, object]
type Handler = Callable[[str, Frame], Awaitable[Frame]]


class TransportError(OSError):
    """Peer unreachable or protocol violation."""


def _decode(raw: bytes) -> Frame:
    try:
        data = cast("object", json.loads(raw))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TransportError("Malformed frame") from exc
    if not isinstance(data, dict):
        raise TransportError("Malformed frame")
    return cast("Frame", data)


def _secret_key(path: Path) -> bytes:
    """Persist the endpoint key so peers can pin this machine across restarts."""
    if path.exists():
        return path.read_bytes()
    key = iroh.SecretKey.generate().to_bytes()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(key)
    return key


def _bind_addresses(port_path: Path) -> list[str]:
    saved = port_path.read_text(encoding="ascii").strip() if port_path.exists() else ""
    return ([f"0.0.0.0:{saved}"] if saved.isdigit() else []) + ["0.0.0.0:0"]


def _save_port(port_path: Path, sockets: list[str]) -> None:
    ipv4 = [s for s in sockets if s.startswith("0.0.0.0:")]
    if ipv4:
        _ = port_path.write_text(ipv4[0].rsplit(":", 1)[1], encoding="ascii")


class Transport:
    """An iroh endpoint whose remote IDs are authenticated by QUIC/TLS."""

    endpoint: iroh.Endpoint

    def __init__(self, endpoint: iroh.Endpoint) -> None:
        self.endpoint = endpoint

    @classmethod
    async def bind(cls, key_path: Path, port_path: Path) -> Transport:
        """Bind with a persistent key and, when free, the previous port so tickets stay valid."""
        local_only = os.environ.get(LOCAL_ONLY_ENV) == "1"
        key = await asyncio.to_thread(_secret_key, key_path)
        endpoint = None
        for address in await asyncio.to_thread(_bind_addresses, port_path):
            options = iroh.EndpointOptions(
                preset=iroh.preset_n0_disable_relay() if local_only else iroh.preset_n0(),
                secret_key=key,
                alpns=[ALPN],
                bind_addr=address,
            )
            try:
                endpoint = await iroh.Endpoint.bind(options)
                break
            except iroh.IrohError:
                continue
        if endpoint is None:
            raise TransportError("Could not bind a collaboration endpoint")
        await asyncio.to_thread(_save_port, port_path, endpoint.bound_sockets())
        if not local_only:
            await asyncio.wait_for(endpoint.online(), CONNECT_TIMEOUT_SECONDS)
        return cls(endpoint)

    @property
    def endpoint_id(self) -> str:
        return str(self.endpoint.id())

    def ticket(self) -> str:
        return str(iroh.EndpointTicket.from_addr(self.endpoint.addr()))

    @staticmethod
    def ticket_id(ticket: str) -> str:
        return str(iroh.EndpointTicket.from_string(ticket).endpoint_addr().id())

    async def request(self, ticket: str, frame: Frame) -> Frame:
        """Send one frame to the endpoint in `ticket` and return its response."""
        address = iroh.EndpointTicket.from_string(ticket).endpoint_addr()
        try:
            connection = await asyncio.wait_for(
                self.endpoint.connect(address, ALPN), CONNECT_TIMEOUT_SECONDS
            )
        except (TimeoutError, iroh.IrohError) as exc:
            raise TransportError(f"Peer unreachable: {exc}") from exc
        try:
            if str(connection.remote_id()) != str(address.id()):
                raise TransportError("Connected to an unexpected endpoint")
            stream = await connection.open_bi()
            outgoing = stream.send()
            await outgoing.write_all(json.dumps(frame).encode())
            await outgoing.finish()
            return _decode(await stream.recv().read_to_end(MAX_FRAME_BYTES))
        except iroh.IrohError as exc:
            raise TransportError(f"Peer connection failed: {exc}") from exc
        finally:
            connection.close(0, b"done")

    async def serve(self, handler: Handler) -> None:
        """Accept connections forever; each gets one request and one response."""
        while True:
            incoming = await self.endpoint.accept_next()
            if incoming is None:
                return
            _ = asyncio.create_task(self._handle(incoming, handler))  # noqa: RUF006

    @staticmethod
    async def _handle(incoming: iroh.Incoming, handler: Handler) -> None:
        try:
            connection = await (await incoming.accept()).connect()
            remote = str(connection.remote_id())
            stream = await connection.accept_bi()
            request = _decode(await stream.recv().read_to_end(MAX_FRAME_BYTES))
            response = await handler(remote, request)
            outgoing = stream.send()
            await outgoing.write_all(json.dumps(response).encode())
            await outgoing.finish()
            await connection.closed()
        except (iroh.IrohError, TransportError):
            return

    async def close(self) -> None:
        await self.endpoint.close()
