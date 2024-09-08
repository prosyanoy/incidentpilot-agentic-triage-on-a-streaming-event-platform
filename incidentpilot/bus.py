"""Event bus abstraction: in-process for local runs/tests, NATS for the cluster."""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Protocol

Handler = Callable[[bytes], Awaitable[None]]


class Bus(Protocol):
    async def publish(self, data: bytes) -> None: ...
    async def subscribe(self, handler: Handler) -> None: ...
    async def close(self) -> None: ...


class MemoryBus:
    """Delivers synchronously to subscribers in the same event loop."""

    def __init__(self) -> None:
        self._handlers: list[Handler] = []

    async def publish(self, data: bytes) -> None:
        for h in self._handlers:
            await h(data)

    async def subscribe(self, handler: Handler) -> None:
        self._handlers.append(handler)

    async def close(self) -> None:
        self._handlers.clear()


class NatsBus:
    def __init__(self, nc, subject: str):
        self._nc, self._subject = nc, subject

    async def publish(self, data: bytes) -> None:
        await self._nc.publish(self._subject, data)

    async def subscribe(self, handler: Handler) -> None:
        async def cb(msg):
            await handler(msg.data)
        await self._nc.subscribe(self._subject, cb=cb)

    async def close(self) -> None:
        await self._nc.drain()


async def connect_bus(url: str, subject: str, retries: int = 30, delay: float = 2.0) -> Bus:
    if url.startswith("memory://"):
        return MemoryBus()
    if url.startswith("nats://"):
        import nats  # imported lazily so memory:// runs need no broker client
        from urllib.parse import urlparse
        u = urlparse(url)
        last: Exception | None = None
        for _ in range(retries):
            try:
                # nats-py retries an unreachable initial connection for minutes; probe the
                # socket first so this loop owns the retry policy.
                _, w = await asyncio.wait_for(asyncio.open_connection(u.hostname, u.port or 4222), 3)
                w.close()
                return NatsBus(await nats.connect(url, connect_timeout=3), subject)
            except Exception as exc:  # broker still starting
                last = exc
                await asyncio.sleep(delay)
        raise ConnectionError(f"could not reach NATS at {url}: {last}")
    raise ValueError(f"unsupported bus url {url!r}; use memory:// or nats://host:4222")
