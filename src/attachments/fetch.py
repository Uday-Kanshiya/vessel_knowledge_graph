"""
HTTP fetch of mail bodies and attachment bytes from assets-prod.siya.com.

httpx (already a dependency of the openai SDK), bounded retries with
exponential backoff on connection errors, timeouts and 5xx. A 4xx is final:
the file is gone or forbidden, and retrying will not change that.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from src.config import FETCH_MAX_BYTES, FETCH_RETRIES, FETCH_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


class FetchError(Exception):
    def __init__(self, url: str, message: str, status: Optional[int] = None):
        super().__init__(f"{message} ({url})")
        self.url = url
        self.status = status


@dataclass
class Fetched:
    url: str
    status: int
    content_type: str
    data: bytes


class Fetcher:
    """One httpx client for the run; safe to share between tasks."""

    def __init__(
        self,
        timeout: float = FETCH_TIMEOUT_SECONDS,
        retries: int = FETCH_RETRIES,
        max_bytes: int = FETCH_MAX_BYTES,
        concurrency: int = 8,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.retries = max(1, retries)
        self.max_bytes = max_bytes
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "vessel-knowledge-graph/0.1"},
        )
        self._sem = asyncio.Semaphore(concurrency)

    async def aclose(self) -> None:
        if self._own_client:
            await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def get(self, url: str) -> Fetched:
        last: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                async with self._sem:
                    resp = await self.client.get(url)
                if resp.status_code >= 400:
                    raise FetchError(url, f"HTTP {resp.status_code}", resp.status_code)
                data = resp.content
                if len(data) > self.max_bytes:
                    raise FetchError(url, f"response {len(data)} bytes exceeds FETCH_MAX_BYTES")
                return Fetched(
                    url=url,
                    status=resp.status_code,
                    content_type=resp.headers.get("content-type", ""),
                    data=data,
                )
            except FetchError as e:
                if e.status is not None and 400 <= e.status < 500:
                    raise
                last = e
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last = e
            if attempt < self.retries:
                delay = 1.5 * (2 ** (attempt - 1))
                logger.warning(
                    "fetch retry %d/%d in %.1fs: %s (%s)", attempt, self.retries, delay, url, last
                )
                await asyncio.sleep(delay)
        raise FetchError(url, f"failed after {self.retries} attempts: {last}")
