"""Web search behind one interface, with four real providers and a fallback.

Order of preference is Serper -> Tavily -> Brave -> Google CSE -> DuckDuckGo
HTML.  The DuckDuckGo path needs no key at all, which keeps the system usable
for anyone who clones the repo without signing up for anything, but it is the
weakest and is clearly labelled as such in the UI health panel.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from selectolax.parser import HTMLParser

from ..config import Settings
from .base import (
    Blocked,
    NotConfigured,
    ParseFailed,
    ProviderChain,
    QuotaExhausted,
    RateLimited,
    SearchResult,
    Timeout,
    with_retries,
)


def _raise_for_status(status: int, provider: str) -> None:
    if status == 429:
        raise RateLimited(f"{provider}: rate limited")
    if status in (401, 403):
        raise QuotaExhausted(f"{provider}: auth/quota rejected ({status})")
    if status == 402:
        raise QuotaExhausted(f"{provider}: payment required")
    if status >= 500:
        raise Timeout(f"{provider}: upstream {status}")
    if status >= 400:
        raise ParseFailed(f"{provider}: HTTP {status}")


class SearchProvider:
    name = "base"

    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client = client
        self.settings = settings

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:  # pragma: no cover
        raise NotImplementedError


class SerperProvider(SearchProvider):
    name = "serper"

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        key = self.settings.serper_api_key
        if not key:
            raise NotConfigured("serper")

        async def _go() -> list[SearchResult]:
            r = await self.client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                json={"q": query, "num": min(limit, 20)},
            )
            _raise_for_status(r.status_code, "serper")
            data = r.json()
            out = []
            for item in (data.get("organic") or [])[:limit]:
                if item.get("link"):
                    out.append(SearchResult(
                        title=item.get("title", ""), url=item["link"],
                        snippet=item.get("snippet", ""), source="serper"))
            return out

        return await with_retries(_go)


class TavilyProvider(SearchProvider):
    name = "tavily"

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        key = self.settings.tavily_api_key
        if not key:
            raise NotConfigured("tavily")

        async def _go() -> list[SearchResult]:
            r = await self.client.post(
                "https://api.tavily.com/search",
                json={"api_key": key, "query": query, "max_results": min(limit, 20),
                      "search_depth": "basic", "include_answer": False},
            )
            _raise_for_status(r.status_code, "tavily")
            data = r.json()
            return [
                SearchResult(title=i.get("title", ""), url=i.get("url", ""),
                             snippet=i.get("content", "")[:400], source="tavily")
                for i in (data.get("results") or [])[:limit] if i.get("url")
            ]

        return await with_retries(_go)


class BraveProvider(SearchProvider):
    name = "brave"

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        key = self.settings.brave_api_key
        if not key:
            raise NotConfigured("brave")

        async def _go() -> list[SearchResult]:
            r = await self.client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
                params={"q": query, "count": min(limit, 20)},
            )
            _raise_for_status(r.status_code, "brave")
            data = r.json()
            return [
                SearchResult(title=i.get("title", ""), url=i.get("url", ""),
                             snippet=re.sub("<[^>]+>", "", i.get("description", "")), source="brave")
                for i in ((data.get("web") or {}).get("results") or [])[:limit] if i.get("url")
            ]

        return await with_retries(_go)


class GoogleCSEProvider(SearchProvider):
    name = "google_cse"

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        key, cx = self.settings.google_cse_key, self.settings.google_cse_cx
        if not (key and cx):
            raise NotConfigured("google_cse")

        async def _go() -> list[SearchResult]:
            r = await self.client.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": key, "cx": cx, "q": query, "num": min(limit, 10)},
            )
            _raise_for_status(r.status_code, "google_cse")
            data = r.json()
            return [
                SearchResult(title=i.get("title", ""), url=i.get("link", ""),
                             snippet=i.get("snippet", ""), source="google_cse")
                for i in (data.get("items") or [])[:limit] if i.get("link")
            ]

        return await with_retries(_go)


class DuckDuckGoProvider(SearchProvider):
    """Keyless fallback: parses the no-JS HTML endpoint. Fragile by nature."""

    name = "duckduckgo"

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        if not self.settings.allow_ddg_fallback:
            raise NotConfigured("duckduckgo disabled")

        async def _go() -> list[SearchResult]:
            r = await self.client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers={"Referer": "https://html.duckduckgo.com/", "Content-Type": "application/x-www-form-urlencoded"},
            )
            if r.status_code in (202, 403):
                raise Blocked("duckduckgo: challenge page")
            _raise_for_status(r.status_code, "duckduckgo")
            return self._parse(r.text, limit)

        return await with_retries(_go)

    @staticmethod
    def _parse(html: str, limit: int) -> list[SearchResult]:
        tree = HTMLParser(html)
        out: list[SearchResult] = []
        for node in tree.css("div.result, div.web-result")[: limit * 2]:
            a = node.css_first("a.result__a")
            if not a:
                continue
            href = a.attributes.get("href", "")
            # DDG wraps targets in /l/?uddg=<encoded>
            if "uddg=" in href:
                qs = parse_qs(urlparse(href).query)
                href = unquote(qs.get("uddg", [""])[0]) or href
            if not href.startswith("http"):
                continue
            snip = node.css_first("a.result__snippet") or node.css_first(".result__snippet")
            out.append(SearchResult(title=a.text(strip=True), url=href,
                                    snippet=snip.text(strip=True) if snip else "", source="duckduckgo"))
            if len(out) >= limit:
                break
        return out


class FixtureSearchProvider(SearchProvider):
    """Deterministic provider used by the test-suite; never touches the network."""

    name = "fixture"

    def __init__(self, results_by_query: dict[str, list[SearchResult]] | None = None, default: list[SearchResult] | None = None):
        self.results_by_query = results_by_query or {}
        self.default = default or []
        self.calls: list[str] = []

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        self.calls.append(query)
        for pat, res in self.results_by_query.items():
            if pat.lower() in query.lower():
                return res[:limit]
        return self.default[:limit]


class SearchService:
    """The thing the rest of the system talks to.

    Enforces its own call ceiling.  Search credits are the scarcest resource in
    a free-tier deployment, and per-company probe searches would otherwise spend
    them without ever appearing in the run budget.
    """

    def __init__(self, chain: ProviderChain[SearchProvider], on_event=None, max_calls: int = 10_000):
        self.chain = chain
        self.on_event = on_event or (lambda *a, **k: None)
        self.calls = 0
        self.max_calls = max_calls
        self._budget_warned = False
        self.attempts = 0        # queries asked for
        self.failures = 0        # queries that returned nothing because of an error
        self.last_error = ""

    @classmethod
    def build(cls, client: httpx.AsyncClient, settings: Settings, on_event=None) -> SearchService:
        provs: list[SearchProvider] = []
        if settings.serper_api_key:
            provs.append(SerperProvider(client, settings))
        if settings.tavily_api_key:
            provs.append(TavilyProvider(client, settings))
        if settings.brave_api_key:
            provs.append(BraveProvider(client, settings))
        if settings.google_cse_key and settings.google_cse_cx:
            provs.append(GoogleCSEProvider(client, settings))
        if settings.allow_ddg_fallback:
            provs.append(DuckDuckGoProvider(client, settings))
        return cls(ProviderChain(providers=provs), on_event=on_event,
                   max_calls=settings.budget.max_searches)

    @property
    def active_provider(self) -> str:
        return self.chain.active_name

    @property
    def budget_exhausted(self) -> bool:
        return self.calls >= self.max_calls

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        if self.budget_exhausted:
            if not self._budget_warned:
                self.on_event(f"Search budget of {self.max_calls} queries reached; "
                              f"no further searches will be made this run.", "warn")
                self._budget_warned = True
            return []
        self.attempts += 1
        errors: list[str] = []
        for provider in self.chain.available():
            try:
                res = await provider.search(query, limit=limit)
                self.calls += 1
                return res
            except NotConfigured:
                self.chain.disable(provider.name)
            except QuotaExhausted as e:
                self.on_event(f"Search provider '{provider.name}' out of quota - falling back.", "warn")
                self.chain.disable(provider.name)
                errors.append(str(e))
            except (RateLimited, Blocked, Timeout, ParseFailed) as e:
                errors.append(f"{provider.name}: {e}")
            except Exception as e:  # pragma: no cover - defensive
                errors.append(f"{provider.name}: unexpected {type(e).__name__}: {e}")
        if errors:
            self.failures += 1
            self.last_error = errors[0]
            self.on_event(f"Search failed for {query!r}: {'; '.join(errors[:3])}", "warn")
        elif not self.chain.available():
            # Every provider was disabled by an earlier failure, so this query
            # never reached anything. Silently returning [] here would make the
            # run look like empty results rather than a broken provider.
            self.failures += 1
            if not self.last_error:
                self.last_error = "no search provider available"
        return []

    @property
    def all_searches_failed(self) -> bool:
        """Every query we asked for came back as an error, not as no results."""
        return self.attempts > 0 and self.failures >= self.attempts
