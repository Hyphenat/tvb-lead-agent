"""Company-site reconnaissance.

Given a company, find and read the handful of pages that actually carry the
facts the gates need: what the product is, where the company is based, who runs
it, how to contact them, and - critically for the US-presence rule - whether
there are US offices or US job postings.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from ..providers.base import FetchedPage
from ..providers.fetcher import Fetcher, extract_links, host_of

# Path fragments worth following, in priority order. The label drives which
# validator later consumes the page.
PAGE_INTENTS: list[tuple[str, tuple[str, ...]]] = [
    ("about", ("about", "about-us", "aboutus", "company", "who-we-are", "our-story", "ueber-uns", "a-propos", "quienes-somos")),
    ("team", ("team", "our-team", "leadership", "management", "founders", "people", "equipo", "equipe")),
    ("contact", ("contact", "contact-us", "contactus", "get-in-touch", "kontakt", "contacto",
                 "contatti", "contato", "nous-contacter", "imprint", "impressum", "legal-notice",
                 "mentions-legales", "aviso-legal", "colofon", "legal")),
    ("careers", ("careers", "career", "jobs", "join-us", "work-with-us", "vacancies", "hiring", "openings")),
    ("product", ("product", "products", "platform", "solutions", "features", "how-it-works", "technology", "pricing")),
    ("press", ("press", "news", "media", "newsroom", "blog/press", "announcements")),
    ("locations", ("locations", "offices", "where-we-are", "global")),
]

_MAX_PER_INTENT = 2

# Conventional paths tried even when nothing links to them. Contact and team
# pages get several attempts because they are where a founder's address lives,
# and because legally-mandated disclosure pages (Impressum in German-speaking
# markets, mentions legales in France) are frequently absent from navigation
# while carrying a named contact and a working address.
_FALLBACK_PATHS: dict[str, tuple[str, ...]] = {
    # Imprint spellings are worth several attempts: in run 8 every company that
    # reached the last gate failed it for want of an address, and these pages are
    # where a named representative's address is legally required to appear.
    "contact": ("contact", "contact-us", "impressum", "imprint", "mentions-legales",
                "kontakt", "legal-notice", "de/impressum", "en/imprint", "legal",
                "impressum-datenschutz", "aviso-legal", "colofon", "contatti",
                "contacto", "about/contact", "company/contact"),
    "team": ("team", "about/team", "our-team", "leadership"),
    "about": ("about", "about-us", "company"),
    "careers": ("careers", "jobs"),
    "product": ("product", "platform"),
    "press": ("press", "news"),
    "locations": ("locations", "offices"),
}


@dataclass
class SiteBundle:
    """Everything read from one company's own website."""

    domain: str
    home: FetchedPage | None = None
    pages: dict[str, list[FetchedPage]] = field(default_factory=dict)

    def all_pages(self) -> list[FetchedPage]:
        out = [self.home] if self.home and self.home.ok else []
        for lst in self.pages.values():
            out.extend([p for p in lst if p.ok])
        return out

    def text_for(self, *intents: str) -> str:
        parts: list[str] = []
        for intent in intents:
            for p in self.pages.get(intent, []):
                if p.ok:
                    parts.append(f"<<<SOURCE {p.url}>>>\n{p.text}")
        return "\n\n".join(parts)

    def combined_text(self, limit: int = 60_000) -> str:
        parts = []
        if self.home and self.home.ok:
            parts.append(f"<<<SOURCE {self.home.url}>>>\n{self.home.text}")
        for intent in ("about", "team", "contact", "product", "locations", "careers", "press"):
            for p in self.pages.get(intent, []):
                if p.ok:
                    parts.append(f"<<<SOURCE {p.url}>>>\n{p.text}")
        return "\n\n".join(parts)[:limit]

    def urls(self) -> list[str]:
        return [p.url for p in self.all_pages()]


def classify_link(url: str, anchor: str) -> str | None:
    """Label a link by intent, matching whole path segments only.

    Segment-exact matching matters: a blog post at ``/blog/post-about-ai``
    contains the substring "about" but is not an about page, and wasting a fetch
    slot on it costs a page from the budget.
    """
    segments = [seg for seg in urlparse(url).path.lower().strip("/").split("/") if seg]
    anchor_norm = re.sub(r"[^a-z ]+", " ", (anchor or "").lower()).strip()
    anchor_norm = re.sub(r"\s+", " ", anchor_norm)

    matches: list[tuple[int, str]] = []   # (specificity, intent)
    for intent, keys in PAGE_INTENTS:
        for k in keys:
            k_norm = k.lower()
            for depth, seg in enumerate(segments):
                seg_clean = re.sub(r"\.(html?|php|aspx?)$", "", seg)
                if seg_clean == k_norm or seg_clean == k_norm.replace("-", ""):
                    matches.append((depth + 1, intent))
            # Anchor text must *be* the label, not merely contain the word.
            a_key = k_norm.replace("-", " ")
            if anchor_norm and (anchor_norm == a_key or anchor_norm.startswith(a_key + " ")) and len(anchor_norm) <= len(a_key) + 12:
                matches.append((0, intent))
    if not matches:
        return None
    # The deepest path segment wins: /company/leadership is a team page, not an
    # about page, even though "company" appears earlier in the path.
    return max(matches, key=lambda m: m[0])[1]


async def resolve_company_site(fetcher: Fetcher, domain: str) -> tuple[FetchedPage | None, str]:
    """Try https then http, www and bare. Returns ``(page, html)``."""
    for candidate in (f"https://{domain}", f"https://www.{domain}", f"http://{domain}"):
        page, html = await fetcher.fetch_safe(candidate, want_html=True)
        if page.ok:
            return page, html
    return None, ""


async def crawl_company_site(fetcher: Fetcher, domain: str, *, max_pages: int = 12) -> SiteBundle:
    """Fetch the home page, then the most informative internal pages."""
    bundle = SiteBundle(domain=domain)
    home, html = await resolve_company_site(fetcher, domain)
    if not home:
        return bundle
    bundle.home = home

    site_host = host_of(home.url)
    wanted: dict[str, list[str]] = {}
    for url, anchor in extract_links(html, home.url):
        if host_of(url) != site_host:
            continue
        intent = classify_link(url, anchor)
        if not intent:
            continue
        lst = wanted.setdefault(intent, [])
        if url not in lst and len(lst) < _MAX_PER_INTENT:
            lst.append(url)

    # Conventional paths are guesses, and guesses are only worth making where the
    # site has not already told us the answer. Measured over a real database:
    # 3,568 of 8,015 fetches were 404s on exactly these paths - /team,
    # /our-team, /impressum, /about/team - 45% of all fetching spent on pages
    # that were not there, on sites whose own navigation already linked the
    # page. So an intent the home page has linked is left alone, and only the
    # two intents that carry a founder's address are still guessed at when the
    # navigation is silent.
    _GUESS_WHEN_SILENT = ("contact", "team")
    # The exception: an imprint is legally required in several of the markets
    # this agent leans on, and is routinely absent from the navigation while
    # carrying the one thing hardest to find anywhere else - a named
    # representative and a working address. Those paths are always worth a try,
    # even on a site that does link a contact page.
    _ALWAYS_TRY = ("impressum", "imprint", "mentions-legales", "legal-notice")
    for intent, keys in PAGE_INTENTS:
        existing = wanted.setdefault(intent, [])
        if intent == "contact":
            for path in _ALWAYS_TRY:
                candidate = urljoin(home.url, f"/{path}")
                if candidate not in existing and len(existing) < 5:
                    existing.append(candidate)
            continue
        if existing:
            continue                       # the site linked it; no need to guess
        if intent not in _GUESS_WHEN_SILENT:
            continue
        fallbacks = _FALLBACK_PATHS.get(intent, (keys[0],))
        for path in fallbacks[:3]:
            candidate = urljoin(home.url, f"/{path}")
            if candidate not in existing and len(existing) < 3:
                existing.append(candidate)

    order = [i for i, _ in PAGE_INTENTS]
    todo: list[tuple[str, str]] = []
    for intent in order:
        for url in wanted.get(intent, []):
            todo.append((intent, url))
    todo = todo[:max_pages]

    results = await asyncio.gather(*(fetcher.fetch_safe(u) for _, u in todo), return_exceptions=True)
    for (intent, _url), page in zip(todo, results, strict=False):
        if isinstance(page, Exception) or not getattr(page, "ok", False):
            continue
        bundle.pages.setdefault(intent, []).append(page)
    return bundle
