from __future__ import annotations

import asyncio

import httpx


class HttpClientPool:
    """One keep-alive httpx client per event loop.

    Building a client per request pays a fresh TLS handshake every time,
    which measured ~190 ms per call against the speech and planner hosts.
    The client is rebuilt if the running loop changes, so tests that drive
    the same client from several ``asyncio.run`` calls stay correct.
    """

    def __init__(
        self,
        *,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._timeout = timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def get(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            )
            self._loop = loop
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        self._loop = None
        if client is not None:
            await client.aclose()
