"""Polite HTTP fetching with robots.txt compliance, rate limiting and caching.

Every page the agent reads is recorded, because a page's text is what later
proves (or disproves) a claim: the grounded extractor checks each quote against
exactly the text captured here.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from selectolax.parser import HTMLParser

from ..config import Settings
from ..storage import Store
from .base import Blocked, FetchedPage, HostRateLimiter, RateLimited, Timeout

_SKIP_EXT = re.compile(r"\.(pdf|zip|docx?|xlsx?|pptx?|png|jpe?g|gif|svg|webp|mp4|mp3|css|js|ico)(\?|$)", re.I)
_DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "nav", "footer", "form")


def host_of(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def is_fetchable(url: str) -> bool:
    if not url or not url.startswith(("http://", "https://")):
        return False
    return not _SKIP_EXT.search(url)


def extract_text(html: str) -> tuple[str, str]:
    """Return ``(visible_text, title)`` with boilerplate removed.

    The meta description is prepended to the text rather than discarded: it is
    usually the clearest one-line statement of what a company does, and keeping
    it inside the page text means a description claim can still be *grounded*
    against the source like any other.
    """
    tree = HTMLParser(html)
    title = ""
    if tree.css_first("title"):
        title = tree.css_first("title").text(strip=True)

    meta = ""
    for selector in ('meta[name="description"]', 'meta[property="og:description"]',
                     'meta[name="twitter:description"]'):
        node = tree.css_first(selector)
        if node:
            meta = (node.attributes.get("content") or "").strip()
            if len(meta) > 30:
                break
            meta = ""
    for tag in _DROP_TAGS:
        for node in tree.css(tag):
            node.decompose()
    body = tree.body or tree.root
    text = body.text(separator="\n") if body else ""
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    text = text.strip()
    if meta:
        text = f"{meta}\n\n{text}"

    # A great many sites publish the address only inside a mailto: link - the
    # visible text is "Contact", an icon, or the person's name. Reading only the
    # rendered text throws those away, and a real run rejected every company it
    # researched for having no email address on any page it read. The addresses
    # are appended as ordinary sentences so a quote can still be grounded
    # against the page, and the link's own label travels with each one because
    # that label is often the founder's name.
    linked = _mailto_sentences(tree)
    if linked:
        text = f"{text}\n\n" + "\n".join(linked)
    return text, title


def _mailto_sentences(tree: HTMLParser) -> list[str]:
    """One plain sentence per ``mailto:`` link, carrying its visible label."""
    seen: set[str] = set()
    out: list[str] = []
    for a in tree.css('a[href^="mailto:"]'):
        href = (a.attributes.get("href") or "")
        addr = href[7:].split("?")[0].strip().strip("<>").lower()
        if not addr or "@" not in addr or addr in seen:
            continue
        seen.add(addr)
        label = a.text(strip=True) or (a.attributes.get("aria-label") or "").strip()
        label = re.sub(r"\s+", " ", label)[:120]
        if label and label.lower() != addr:
            out.append(f"Email link on this page labelled \u201c{label}\u201d: {addr}")
        else:
            out.append(f"Email link published on this page: {addr}")
        if len(out) >= 20:
            break
    return out


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """``(absolute_url, label)`` for every on-page link.

    Portfolio pages are usually grids of logos, so when a link has no text the
    image's ``alt`` or ``title`` carries the company name. Without this the only
    label available is the domain, and deriving a name from a domain is how
    navigation links become "companies".
    """
    tree = HTMLParser(html)
    out: list[tuple[str, str]] = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        label = a.text(strip=True)
        if not label:
            img = a.css_first("img")
            if img:
                label = (img.attributes.get("alt") or img.attributes.get("title") or "").strip()
        if not label:
            label = (a.attributes.get("title") or a.attributes.get("aria-label") or "").strip()
        out.append((urljoin(base_url, href), label[:200]))
    return out


class Fetcher:
    def __init__(self, client: httpx.AsyncClient, settings: Settings, store: Store | None = None, on_event=None):
        self.client = client
        self.settings = settings
        self.store = store
        self.limiter = HostRateLimiter(settings.per_host_delay_seconds)
        self.on_event = on_event or (lambda *a, **k: None)
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_lock = asyncio.Lock()
        self.pages_fetched = 0
        self.cache_hits = 0
        # One unresponsive host cost a real run roughly four minutes: twelve
        # conventional paths, each waiting out the full timeout. After a few
        # consecutive failures a host is written off for the rest of the run.
        self._host_failures: dict[str, int] = {}
        self.host_failure_limit = 3
        self.hosts_abandoned: set[str] = set()

    # --------------------------------------------------- host health --
    def _note_failure(self, host: str) -> None:
        n = self._host_failures.get(host, 0) + 1
        self._host_failures[host] = n
        if n == self.host_failure_limit and host not in self.hosts_abandoned:
            self.hosts_abandoned.add(host)
            self.on_event(f"Giving up on {host} after {n} consecutive failures.", "debug")

    def _note_success(self, host: str) -> None:
        self._host_failures.pop(host, None)

    def host_is_abandoned(self, url: str) -> bool:
        return host_of(url) in self.hosts_abandoned

    # ------------------------------------------------------------ robots --
    async def _robots_for(self, host: str) -> RobotFileParser | None:
        async with self._robots_lock:
            if host in self._robots:
                return self._robots[host]
            self._robots[host] = None  # pessimistic placeholder against stampedes
        rp = RobotFileParser()
        try:
            r = await self.client.get(f"https://{host}/robots.txt", timeout=8.0)
            if r.status_code == 200 and len(r.text) < 500_000:
                rp.parse(r.text.splitlines())
            else:
                rp = None
        except Exception:
            rp = None  # unreachable robots.txt is treated as "no restrictions stated"
        async with self._robots_lock:
            self._robots[host] = rp
        return rp

    async def allowed(self, url: str) -> bool:
        if not self.settings.respect_robots:
            return True
        rp = await self._robots_for(host_of(url))
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.settings.user_agent, url)
        except Exception:
            return True

    # ------------------------------------------------------------- fetch --
    async def fetch(self, url: str, *, use_cache: bool = True, want_html: bool = False):
        """Return a :class:`FetchedPage`, or ``(page, html)`` when ``want_html``."""
        if not is_fetchable(url):
            page = FetchedPage(url=url, status=0, text="")
            return (page, "") if want_html else page

        if self.host_is_abandoned(url):
            raise Blocked(f"{host_of(url)} abandoned after repeated failures")

        if use_cache and self.store:
            cached = self.store.get_page(url, self.settings.cache_ttl_seconds)
            if cached:
                self.cache_hits += 1
                page = FetchedPage(url=url, status=cached["status"] or 200,
                                   text=cached["text"] or "", title=cached["title"] or "", from_cache=True)
                return (page, "") if want_html else page

        if not await self.allowed(url):
            self.on_event(f"robots.txt disallows {url}", "debug")
            raise Blocked(f"robots.txt disallows {url}")

        await self.limiter.acquire(host_of(url))
        try:
            r = await self.client.get(url, follow_redirects=True,
                                      headers={"User-Agent": self.settings.user_agent,
                                               "Accept": "text/html,application/xhtml+xml"})
        except httpx.TimeoutException as e:
            self._note_failure(host_of(url))
            raise Timeout(f"timeout fetching {url}") from e
        except httpx.HTTPError as e:
            self._note_failure(host_of(url))
            raise Blocked(f"transport error for {url}: {type(e).__name__}") from e

        if r.status_code == 429:
            self._note_failure(host_of(url))
            raise RateLimited(f"429 from {host_of(url)}")
        if r.status_code in (401, 403):
            self._note_failure(host_of(url))
            raise Blocked(f"{r.status_code} from {host_of(url)}")
        self._note_success(host_of(url))

        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype and "text" not in ctype:
            page = FetchedPage(url=str(r.url), status=r.status_code, text="")
            return (page, "") if want_html else page

        html = r.text
        text, title = extract_text(html)
        self.pages_fetched += 1
        if self.store:
            self.store.save_page(url, r.status_code, text, title)
        page = FetchedPage(url=str(r.url), status=r.status_code, text=text, title=title)
        return (page, html) if want_html else page

    async def fetch_safe(self, url: str, *, want_html: bool = False):
        """Never raises - failures become an empty page so one bad URL can't kill a run."""
        try:
            return await self.fetch(url, want_html=want_html)
        except Exception as e:
            self.on_event(f"fetch failed {url}: {type(e).__name__}: {e}", "debug")
            page = FetchedPage(url=url, status=0, text="")
            return (page, "") if want_html else page
