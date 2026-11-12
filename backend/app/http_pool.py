from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

try:  # httpx internals, used only to mirror how it reads the environment
    from httpx._utils import URLPattern, get_environment_proxies
except ImportError:  # pragma: no cover - older httpx
    URLPattern = None
    get_environment_proxies = None


def _entry_is_usable(entry: str) -> bool:
    """True when httpx can turn this proxy-bypass entry into a mount pattern."""
    if get_environment_proxies is None or URLPattern is None:
        return ":" not in entry
    original = os.environ.get("NO_PROXY")
    try:
        os.environ["NO_PROXY"] = entry
        for key in get_environment_proxies():
            try:
                URLPattern(key)
            except httpx.InvalidURL:
                return False
        return True
    finally:
        if original is None:
            os.environ.pop("NO_PROXY", None)
        else:
            os.environ["NO_PROXY"] = original


@contextmanager
def _usable_no_proxy() -> Iterator[None]:
    """Drop proxy-bypass entries httpx cannot parse, such as a bracketed IPv6 host."""
    original = os.environ.get("NO_PROXY")
    if not original:
        yield
        return
    entries = [entry.strip() for entry in original.split(",") if entry.strip()]
    usable = [entry for entry in entries if _entry_is_usable(entry)]
    if len(usable) == len(entries):
        yield
        return
    os.environ["NO_PROXY"] = ",".join(usable)
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("NO_PROXY", None)
        else:
            os.environ["NO_PROXY"] = original


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
            self._client = self._build()
            self._loop = loop
        return self._client

    def _build(self) -> httpx.AsyncClient:
        try:
            return httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            )
        except httpx.InvalidURL:
            # A malformed proxy-bypass entry stops httpx from building any
            # client at all, which would take down every provider call.
            with _usable_no_proxy():
                try:
                    return httpx.AsyncClient(
                        timeout=self._timeout,
                        transport=self._transport,
                    )
                except httpx.InvalidURL:
                    return httpx.AsyncClient(
                        timeout=self._timeout,
                        transport=self._transport,
                        trust_env=False,
                    )

    async def aclose(self) -> None:
        client, self._client = self._client, None
        self._loop = None
        if client is not None:
            await client.aclose()
