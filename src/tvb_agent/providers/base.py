"""Shared plumbing for every external dependency.

Each capability (search, LLM, email verification) is expressed as a small
interface with several implementations chained behind it.  A provider that is
rate-limited or out of quota is skipped and the next one is tried, so a dead key
degrades the run instead of ending it.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# Typed failures - the caller decides what to do based on *why* something failed
# --------------------------------------------------------------------------- #
class ProviderError(Exception):
    retryable = False
    fall_through = True   # should the chain try the next provider?


class NotConfigured(ProviderError):
    """No API key for this provider - silently skip."""


class QuotaExhausted(ProviderError):
    """Free tier spent. Never retry; move to the next provider."""


class RateLimited(ProviderError):
    retryable = True
    fall_through = False  # backing off is usually better than switching


class Blocked(ProviderError):
    """403/robots/paywall. Not retryable, not the provider's fault."""
    fall_through = False


class Timeout(ProviderError):
    retryable = True


class ParseFailed(ProviderError):
    fall_through = False


class BudgetExceeded(Exception):
    """Raised by the orchestrator's budget guard; ends the run cleanly."""


# --------------------------------------------------------------------------- #
@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""

    def __hash__(self) -> int:
        return hash(self.url)


@dataclass
class FetchedPage:
    url: str
    status: int
    text: str
    title: str = ""
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and bool(self.text)


# --------------------------------------------------------------------------- #
class HostRateLimiter:
    """Per-host token bucket so we stay a polite citizen of every site we read."""

    def __init__(self, delay_seconds: float = 1.0):
        self.delay = delay_seconds
        self._next_ok: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, host: str) -> asyncio.Lock:
        async with self._guard:
            if host not in self._locks:
                self._locks[host] = asyncio.Lock()
            return self._locks[host]

    async def acquire(self, host: str) -> None:
        lock = await self._lock_for(host)
        async with lock:
            now = time.monotonic()
            wait = self._next_ok.get(host, 0.0) - now
            if wait > 0:
                await asyncio.sleep(wait)
            # jitter avoids lockstep bursts when many hosts are hit at once
            self._next_ok[host] = time.monotonic() + self.delay * random.uniform(0.8, 1.3)


async def with_retries(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 12.0,
) -> T:
    """Exponential backoff with jitter for the retryable failure classes."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await fn()
        except ProviderError as e:
            last = e
            if not e.retryable or i == attempts - 1:
                raise
            await asyncio.sleep(min(max_delay, base_delay * (2**i)) * random.uniform(0.7, 1.3))
        except Exception as e:  # unexpected - one retry then give up
            last = e
            if i >= 1:
                raise
            await asyncio.sleep(base_delay)
    raise last if last else RuntimeError("unreachable")


@dataclass
class ProviderChain(Generic[T]):
    """Try each provider in order; skip the ones that are unconfigured or spent."""

    providers: list[T] = field(default_factory=list)
    _disabled: set[str] = field(default_factory=set)

    def available(self) -> list[T]:
        return [p for p in self.providers if getattr(p, "name", "?") not in self._disabled]

    def disable(self, name: str) -> None:
        self._disabled.add(name)

    @property
    def active_name(self) -> str:
        av = self.available()
        return getattr(av[0], "name", "none") if av else "none"
