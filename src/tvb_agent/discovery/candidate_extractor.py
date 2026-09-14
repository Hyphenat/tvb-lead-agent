"""Turning search results and article text into candidate companies.

Two channels feed the frontier:

* **SERP channel** - headlines like "Acme raises $2.5M" name a company directly.
* **Link-expansion channel** - a VC portfolio page, accelerator cohort page or
  "top 20 startups" listicle links out to many company sites at once.  This is
  how the agent discovers *sources it was never given*, and it is usually the
  richest vein per page fetched.

A candidate is only a lead to investigate.  Nothing extracted here is treated as
evidence of anything; every fact is re-established from scratch during research.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..providers.base import SearchResult
from ..providers.fetcher import host_of

# Hosts that are never the candidate itself.  Kept broad on purpose: a false
# candidate costs a research budget slot, which is the scarcest resource.
NON_COMPANY_HOSTS: set[str] = {
    # press / media
    "techcrunch.com", "venturebeat.com", "forbes.com", "bloomberg.com", "reuters.com",
    "businessinsider.com", "wsj.com", "ft.com", "cnbc.com", "theguardian.com", "bbc.com",
    "economictimes.indiatimes.com", "indiatimes.com", "livemint.com", "business-standard.com",
    "yourstory.com", "inc42.com", "entrackr.com", "moneycontrol.com", "thehindu.com",
    "tech.eu", "sifted.eu", "eu-startups.com", "siliconcanals.com", "techcabal.com",
    "disrupt-africa.com", "wamda.com", "magnitt.com", "techinasia.com", "e27.co",
    "dealstreetasia.com", "startupdaily.net", "betakit.com", "maddyness.com",
    "gruenderszene.de", "frenchweb.fr", "startupticker.ch", "silicon.co.uk", "uktech.news",
    "arabnews.com", "zawya.com", "thenationalnews.com", "prnewswire.com", "businesswire.com",
    "globenewswire.com", "einpresswire.com", "medium.com", "substack.com",
    # databases / social / infra
    "crunchbase.com", "pitchbook.com", "tracxn.com", "dealroom.co", "cbinsights.com",
    "owler.com", "zoominfo.com", "apollo.io", "rocketreach.co", "signalhire.com",
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com", "youtube.com",
    "github.com", "wikipedia.org", "reddit.com", "quora.com", "glassdoor.com", "indeed.com",
    "producthunt.com", "g2.com", "capterra.com", "trustpilot.com", "angel.co", "wellfound.com",
    "google.com", "bing.com", "duckduckgo.com", "yahoo.com", "amazon.com", "apple.com",
    "microsoft.com", "notion.so", "wordpress.com", "blogspot.com", "wixsite.com",
    "eventbrite.com", "meetup.com", "slideshare.net", "scribd.com", "issuu.com",
    # --- added after a real run wasted ~70% of its research budget on these ---
    # contact-scraping and people-data sites
    "contactout.com", "datanyze.com", "ceoemail.com", "bouncewatch.com",
    "muckrack.com", "ambitionbox.com", "craft.co", "ziprecruiter.com", "whitepages.com",
    "lusha.com", "clearbit.com", "hunter.io", "snov.io", "findthatlead.com",
    # startup/VC directories and tooling
    "parsers.vc", "clutch.co", "fi.co", "vestbee.com", "shizune.co", "investorhunt.co",
    "vcbacked.co", "openvc.app", "failory.com", "startupgenome.com", "growthlist.co",
    "coresight.com", "dxbstart.com", "demium.com", "seedtable.com", "startupblink.com",
    "theventurecodex.com", "startuplanes.com", "smbpodcast.com", "app.dealroom.co", "impactalpha.com", "retail-insider.com",
    # general and business media
    "theglobeandmail.com", "arabnews.pk", "securitysystemsnews.com", "pulse2.com", "dokumen.pub", "lexology.com",
}

# Second-level labels that mark an institution rather than a business, under
# any country TLD: ebooks.tau.edu.ng slipped through a fixed suffix list.
_INSTITUTION_LABELS = {"gov", "edu", "ac", "mil", "int", "sch", "k12", "police", "nhs"}


def _is_institution_host(host: str) -> bool:
    labels = (host or "").lower().split(".")
    return any(label in _INSTITUTION_LABELS for label in labels[1:])

_BAD_NAME_TOKENS = {
    "the", "a", "an", "this", "that", "these", "those", "it", "its", "we", "our", "us",
    "startup", "startups", "company", "companies", "firm", "platform", "founder", "founders",
    "ceo", "news", "report", "top", "best", "list", "why", "how", "what", "new", "more",
    "million", "funding", "round", "seed", "series", "investors", "investment", "raises",
    "raised", "secures", "closes", "announces", "launches", "today", "year", "month",
}

# Real funding headlines almost never start with the company name. They read
# "Indian fintech Jar raises...", "Lagos-based Moniepoint raises...",
# "Berlin's Pliant raises...". So the verb is located first, and the company is
# recovered by walking backwards from it through the qualifiers.
_HEADLINE_VERBS = (
    r"raises?|raised|raising|secures?|secured|closes?|closed|bags?|bagged|nets?|netted|"
    r"lands?|landed|announces?|announced|picks up|scores?|scored|gets?|receives?|received|"
    r"obtains?|attracts?|attracted|snags?|snagged|pulls in|banks?|rakes in|clinches?|"
    r"completes?|completed|wraps up|adds?|draws?"
)
_HEADLINE_VERB_RE = re.compile(rf"\b(?:{_HEADLINE_VERBS})\b", re.IGNORECASE)

# Words that describe a company but are not part of its name. Nationalities are
# capitalised in headlines, so they have to be listed explicitly.
_DESCRIPTORS = {
    # sector / entity
    "fintech", "healthtech", "healthcare", "edtech", "insurtech", "proptech", "agritech",
    "foodtech", "cleantech", "climatetech", "deeptech", "medtech", "regtech", "legaltech",
    "traveltech", "adtech", "hrtech", "martech", "retailtech", "logistics", "mobility",
    "cybersecurity", "security", "biotech", "spacetech", "startup", "startups", "scaleup",
    "scale-up", "company", "firm", "business", "platform", "venture", "unicorn", "soonicorn",
    "provider", "maker", "player", "giant", "leader", "innovator", "operator", "developer",
    "app", "software", "saas", "ai", "b2b", "b2c", "tech", "technology", "digital", "online",
    "mobile", "cloud", "data", "crypto", "web3", "gaming", "ecommerce", "e-commerce",
    # nationalities
    "indian", "pakistani", "bangladeshi", "sri", "lankan", "nepali", "chinese", "japanese",
    "korean", "taiwanese", "singaporean", "malaysian", "indonesian", "vietnamese", "thai",
    "filipino", "australian", "kiwi", "canadian", "american", "british", "english", "scottish",
    "welsh", "irish", "french", "german", "austrian", "swiss", "dutch", "belgian", "spanish",
    "portuguese", "italian", "greek", "swedish", "norwegian", "danish", "finnish", "icelandic",
    "polish", "czech", "slovak", "hungarian", "romanian", "bulgarian", "croatian", "serbian",
    "estonian", "latvian", "lithuanian", "ukrainian", "russian", "turkish", "israeli",
    "emirati", "saudi", "qatari", "kuwaiti", "egyptian", "moroccan", "tunisian", "nigerian",
    "kenyan", "ghanaian", "ethiopian", "rwandan", "ugandan", "tanzanian", "senegalese",
    "brazilian", "mexican", "colombian", "chilean", "argentine", "argentinian", "peruvian",
    "uruguayan", "african", "european", "asian", "latam", "mena", "nordic", "baltic",
    # editorial furniture
    "exclusive", "breaking", "update", "report", "opinion", "analysis", "profile", "interview",
    "just", "now", "again", "also", "meet", "watch", "why", "how", "what", "new", "the", "a",
    "an", "this", "that", "its", "it", "and", "or", "with", "for", "from", "after", "before",
}

_POSSESSIVE_RE = re.compile(r"[\u2019']s$")
_BASED_SUFFIX_RE = re.compile(r"-(?:based|headquartered|founded|born|bred)$", re.IGNORECASE)


def _is_name_token(token: str) -> bool:
    """Could this token be part of a company name?

    Accepts lowercase-initial brands like iZettle and eToro (they still carry an
    uppercase letter), rejects ordinary descriptive words.
    """
    t = token.strip(",:;\u2013-")
    if not t or len(t) > 30:
        return False
    if _POSSESSIVE_RE.search(t) or _BASED_SUFFIX_RE.search(t):
        return False
    if t.lower().strip(".") in _DESCRIPTORS:
        return False
    if not re.match(r"^[A-Za-z][A-Za-z0-9&.\u2019'\-]*$", t):
        return False
    return any(c.isupper() for c in t)


def company_from_headline(title: str) -> str | None:
    """Recover the subject of a funding headline.

    Walks backwards from the funding verb collecting name-shaped tokens and
    stops at the first qualifier ("Indian", "fintech", "Lagos-based", "Berlin's"),
    which is where the name begins.
    """
    if not title:
        return None
    head = title.split(" - ")[0]
    m = _HEADLINE_VERB_RE.search(head)
    if not m:
        return None
    prefix = head[: m.start()].strip()
    if ":" in prefix:
        prefix = prefix.rsplit(":", 1)[-1].strip()

    tokens = prefix.split()
    collected: list[str] = []
    for token in reversed(tokens):
        if _is_name_token(token):
            collected.insert(0, token.strip(",:;"))
            if len(collected) >= 5:
                break
        else:
            break
    if not collected:
        return None
    return " ".join(collected)


# "Bengaluru-based Acme Technologies, a SaaS platform, ..."
_BASED_RE = re.compile(
    r"(?:[A-Z][\w\-]+(?:[- ]based)|based in [A-Z][\w\- ]+)\s+(?P<name>[A-Z0-9][\w&.\-']*(?:\s+[A-Z0-9][\w&.\-']*){0,3})",
)

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:Inc|LLC|Ltd|Limited|Pvt|Private|PLC|GmbH|BV|NV|SA|SAS|SARL|AB|AS|Oy|ApS|SRL|SpA|Pte|Sdn|Bhd)\.?\b",
    re.IGNORECASE,
)


def is_probable_company_host(url: str) -> bool:
    h = host_of(url)
    if not h or h in NON_COMPANY_HOSTS:
        return False
    if _is_institution_host(h):
        return False
    if any(h.endswith("." + d) or h == d for d in NON_COMPANY_HOSTS):
        return False
    # Subdomains of blog platforms are not companies either.
    return h.count(".") <= 3


def clean_company_name(raw: str) -> str | None:
    """Normalise an extracted name, or reject it as not-a-name."""
    if not raw:
        return None
    name = re.sub(r"\s+", " ", raw).strip(" \t\n\r-–—,:;|\"'“”’()[]")
    name = _LEGAL_SUFFIX_RE.sub("", name).strip(" ,.")
    if not name or len(name) < 2 or len(name) > 60:
        return None
    toks = name.split()
    if len(toks) > 5:
        return None
    if all(t.lower() in _BAD_NAME_TOKENS for t in toks):
        return None
    # A weak leading word only condemns the name when it is the whole name:
    # "New Relic", "Next Insurance" and "Top Hat" are real companies.
    if len(toks) == 1 and toks[0].lower() in _BAD_NAME_TOKENS:
        return None
    if not re.search(r"[A-Za-z]", name):
        return None
    if is_generic_name(name):
        return None
    # Anchor text is often the bare URL. "greyhounders.com" and
    # "http://www.demium.com" both arrived as company names in a real run.
    if re.match(r"^(?:https?://)?(?:www\.)?[a-z0-9-]+\.[a-z.]{2,}/?$", name.strip(), re.I):
        return None
    # A two-or-three letter all-caps fragment is an acronym or a country code far
    # more often than a company ("EIF", "EU", "VC").
    if len(name) <= 3 and name.isupper():
        return None
    # Reject headline fragments that are really sentences.
    if re.search(r"\b(?:and|or|with|from|after|before|during|into|about)\b", name, re.I) and len(toks) > 2:
        return None
    return name


# Names that are a category, a publication or an institution rather than a
# company. Taken verbatim from what a real run actually produced: "Fintech",
# "Openvc", "Eif", "Mexicobusiness", "Failory".
_GENERIC_NAMES = {
    # sector words that arrive as a whole "name"
    "fintech", "healthtech", "edtech", "insurtech", "proptech", "agritech", "foodtech",
    "cleantech", "climatetech", "deeptech", "medtech", "regtech", "legaltech", "traveltech",
    "adtech", "hrtech", "martech", "retailtech", "biotech", "spacetech", "cybersecurity",
    "saas", "software", "technology", "tech", "startup", "startups", "venture", "ventures",
    "capital", "partners", "holdings", "group", "labs", "digital", "innovation", "ecosystem",
    "accelerator", "incubator", "portfolio", "investors", "investor", "funding", "fund",
    "funds", "seed", "angel", "angels", "equity", "advisors", "advisory", "consulting",
    # directory / media / institution names seen in real discovery output
    "openvc", "failory", "shizune", "investorhunt", "vcbacked", "crunchbase", "dealroom",
    "tracxn", "pitchbook", "eif", "ebrd", "eib", "mexicobusiness", "businessnews",
    "techcrunch", "sifted", "eustartups", "siliconcanals", "wamda", "magnitt", "f6s",
    "linkedin", "medium", "substack", "wikipedia", "producthunt", "rocketreach",
    "signalhire", "zoominfo", "apollo", "lusha", "clearbit", "owler", "growjo",
    # navigation and call-to-action labels: a link reading "Join" is a sign-up
    # button, not a company, however capitalised it happens to be
    "join", "apply", "login", "log in", "signin", "sign in", "signup", "sign up",
    "register", "subscribe", "newsletter", "download", "demo", "pricing", "docs",
    "documentation", "support", "help", "faq", "contact", "about", "home", "blog",
    "news", "press", "careers", "jobs", "privacy", "terms", "cookies", "imprint",
    "impressum", "legal", "back", "next", "previous", "more", "view", "see", "read",
    "learn", "explore", "discover", "get", "start", "menu", "search", "share",
    "follow", "connect", "team", "people", "events", "resources", "insights",
    "community", "members", "apply now", "read more", "learn more", "view all",
    "see all", "get started", "find out more", "our team", "our story", "who we are",
    # section and sponsorship labels harvested off link hubs in a real run
    "sponsor", "sponsors", "sponsor us", "sponsorship", "advertise", "advertising",
    "donate", "shop", "store", "merch", "gallery", "photos", "videos", "podcasts",
    "webinars", "reports", "report", "directory", "database", "awards", "summit",
    "conference", "conferences", "programs", "programmes", "program", "programme",
    "services", "solutions", "companies", "archive", "sitemap", "rss", "feedback",
    "volunteer", "membership", "partnerships", "consultancy", "consultancies",
    "newsletters", "magazines", "interviews", "opinion", "editorial", "guides",
}


def name_from_domain(domain: str) -> str:
    base = (domain or "").split(".")[0]
    base = re.sub(r"[-_]+", " ", base)
    return base.title() if base else domain


# Filler words that never distinguish a company, so "Apply Now" is as generic
# as "Apply".
_FILLER_TOKENS = {"now", "here", "today", "free", "more", "all", "us", "our", "the",
                  "a", "an", "your", "my", "new", "out", "up", "in", "to", "for",
                  "best", "top", "leading", "list", "guide", "of", "and"}

_GENERIC_COMPACT = {re.sub(r"[^a-z]", "", g) for g in _GENERIC_NAMES}


def _is_place_only(tokens: list[str]) -> bool:
    """"Tel Aviv Israel" and "Bengaluru" are datelines, not companies.

    Run 7 researched "Tel Aviv Israel" as though it were a startup; a name made
    of nothing but place words never is one.
    """
    from ..validation.geo import CITY_COUNTRY, COUNTRY_NAMES, US_CITIES, US_STATES

    places = {w for city in CITY_COUNTRY for w in city.split()}
    places |= {w for c in COUNTRY_NAMES for w in c.lower().split()}
    places |= {w for c in US_CITIES for w in c.split()}
    places |= {s.lower() for s in US_STATES}
    places |= {"city", "region", "province", "state", "county", "district"}
    return bool(tokens) and all(t in places for t in tokens)


def is_generic_name(name: str) -> bool:
    """Is this a category, a publisher or a button rather than a company?"""
    low = (name or "").lower().strip()
    if not low:
        return True
    if low in _GENERIC_NAMES:
        return True
    compact = re.sub(r"[^a-z]", "", low)
    if compact in _GENERIC_COMPACT:
        return True
    tokens = [t for t in re.split(r"[^a-z0-9]+", low) if t]
    if not tokens:
        return True
    # "Fintech Ventures", "Apply Now" and "Top 10 startups" are all unusable.
    meaningful = [t for t in tokens if t not in _FILLER_TOKENS and not t.isdigit()]
    if not meaningful:
        return True
    if all(t in _GENERIC_NAMES for t in meaningful):
        return True
    return _is_place_only(meaningful)


@dataclass
class Candidate:
    name: str
    domain: str | None = None
    discovered_via: list[str] = field(default_factory=list)
    hint_snippet: str = ""
    cell_key: str | None = None
    # How this candidate was found. A company named in a funding headline is a
    # far better use of a research slot than a link scraped off a directory:
    # in a real run the headline channel produced the genuine startups and the
    # link channel produced funds, blogs and parked domains.
    source_kind: str = "unknown"

    @property
    def dedup_key(self) -> str:
        from ..storage import normalise_domain, normalise_name

        return normalise_domain(self.domain) or normalise_name(self.name)


def _title_and_snippet(result: SearchResult) -> str:
    """Both halves of a search result, as one quotable passage."""
    title = (result.title or "").strip()
    snippet = (result.snippet or "").strip()
    if title and snippet and not snippet.startswith(title):
        return f"{title}. {snippet}"
    return snippet or title


def candidates_from_search(results: Iterable[SearchResult], cell_key: str | None = None) -> list[Candidate]:
    """Pull candidates out of SERP titles, snippets and result hosts."""
    out: dict[str, Candidate] = {}

    def add(name: str | None, domain: str | None, url: str, snippet: str,
            kind: str = "unknown") -> None:
        nm = clean_company_name(name) if name else None
        if not nm and domain:
            nm = clean_company_name(name_from_domain(domain))
        if not nm:
            return
        cand = Candidate(name=nm, domain=domain, discovered_via=[url],
                         hint_snippet=snippet[:400], cell_key=cell_key, source_kind=kind)
        key = cand.dedup_key
        if key in out:
            if url not in out[key].discovered_via:
                out[key].discovered_via.append(url)
            if not out[key].domain and domain:
                out[key].domain = domain
        else:
            out[key] = cand

    for r in results:
        host = host_of(r.url)
        company_host = host if is_probable_company_host(r.url) else None

        name = company_from_headline(r.title or "")
        if name:
            # A page titled "X raises $Y" is an article ABOUT X, published by
            # someone else. Adopting the result host as X's website attributes
            # the publication's staff and contact details to the company - a real
            # run produced a "qualified lead" whose founder was a trade
            # journalist and whose website was the magazine that covered it.
            # Title *and* snippet. The title is where the company name and the
            # amount live ("Foo raises $2M seed"); the snippet often never
            # repeats the name, so keeping only the snippet meant the excerpt
            # failed its own attribution check and the company was researched
            # with nothing to read. Run 9 lost 293 candidates that way.
            add(name, None, r.url, _title_and_snippet(r), kind="headline")
            continue

        for text in (r.title or "", r.snippet or ""):
            b = _BASED_RE.search(text)
            if b:
                add(b.group("name"), company_host, r.url, _title_and_snippet(r), kind="article")
                break
        else:
            # A result that *is* a company site still counts as a candidate.
            if company_host:
                add(name_from_domain(company_host), company_host, r.url,
                    _title_and_snippet(r), kind="company_site")

    return list(out.values())


def candidates_from_article(text: str, source_url: str, cell_key: str | None = None,
                            max_candidates: int = 12) -> list[Candidate]:
    """Mine an article body for companies other than the one it is about."""
    out: dict[str, Candidate] = {}
    for chunk in (text or "")[:30_000].split("\n"):
        for raw in (company_from_headline(chunk),
                    (_BASED_RE.search(chunk).group("name") if _BASED_RE.search(chunk) else None)):
            if not raw:
                continue
            nm = clean_company_name(raw)
            if not nm:
                continue
            c = Candidate(name=nm, domain=None, discovered_via=[source_url],
                          hint_snippet=chunk.strip()[:400], cell_key=cell_key,
                          source_kind="article")
            out.setdefault(c.dedup_key, c)
            if len(out) >= max_candidates:
                return list(out.values())
    return list(out.values())


# Second-level TLDs, so "chickin.co.id" reduces to "chickin" and not "co".
_TWO_PART_TLDS = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go"}


def _registrable_base(host: str) -> str:
    """The label that actually names the site: assets.example.com -> "example"."""
    parts = [p for p in (host or "").lower().split(".") if p]
    if len(parts) < 2:
        return parts[0] if parts else ""
    if len(parts) >= 3 and parts[-2] in _TWO_PART_TLDS:
        return parts[-3]
    return parts[-2]


def _anchor_matches_domain(name: str, host: str) -> bool:
    """Does a one-word link label actually name the site it points at?

    A portfolio entry reading "Chickin" points at chickin.id.  A sidebar label
    reading "Assets", "Amount", "Blocks" or "Conduct" points anywhere else - and
    run 7 spent research slots on all four.  Single common words are only
    believable as companies when the destination domain agrees with them;
    multi-word names have already survived the generic filter, so they pass.
    """
    tokens = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]
    if len(tokens) != 1:
        return True
    word = tokens[0]
    base = re.sub(r"[^a-z0-9]", "", _registrable_base(host))
    if not base:
        return False
    return word in base or base in word


def candidates_from_link_page(links: list[tuple[str, str]], source_url: str,
                              cell_key: str | None = None, max_candidates: int = 40) -> list[Candidate]:
    """Portfolio / cohort / listicle pages: outbound links *are* the companies."""
    src_host = host_of(source_url)
    out: dict[str, Candidate] = {}
    for url, anchor in links:
        h = host_of(url)
        if not h or h == src_host or not is_probable_company_host(url):
            continue
        # Deep links inside a site are usually navigation, not a portfolio entry.
        path = urlparse(url).path.strip("/")
        if path.count("/") > 1:
            continue
        # A portfolio listing names the company in the link text or the logo's
        # alt attribute. Falling back to the domain is what produced "Estvca",
        # "Seedblink", "Join" and "Ebooks" as companies in a real run: those
        # were navigation and partner links, not portfolio entries.
        nm = clean_company_name(anchor)
        if not nm:
            continue
        if not _anchor_matches_domain(nm, h):
            continue
        c = Candidate(name=nm, domain=h, discovered_via=[source_url], hint_snippet=anchor[:200],
                      cell_key=cell_key, source_kind="link_hub")
        out.setdefault(c.dedup_key, c)
        if len(out) >= max_candidates:
            break
    return list(out.values())


# A page listing *investors* looks exactly like a page listing *startups* to a
# keyword test, and mining one fills the pipeline with VC firms. Real run
# evidence: shizune.co and investorhunt.co between them contributed ~60 "companies",
# every one of them a fund.
_INVESTOR_HUB_HINTS = (
    "list of investors", "investor directory", "vc firms", "venture capital firms",
    "top investors", "angel investors", "find investors", "investor list",
    "fundraising tools", "investor database", "lead investors", "family offices",
    "funds investing", "active investors", "investor crm", "raise capital",
    "pitch investors", "investors in", "vcs investing", "seed funds in",
)


def looks_like_investor_directory(url: str, title: str, text: str) -> bool:
    """Is this page a list of funds rather than a list of operating companies?"""
    blob = f"{url} {title} {text[:3000]}".lower()
    return any(h in blob for h in _INVESTOR_HUB_HINTS)


def looks_like_link_hub(url: str, title: str, text: str) -> bool:
    """Cheap test for 'this page is a list of operating companies'."""
    blob = f"{url} {title} {text[:2000]}".lower()
    hints = ("portfolio", "our companies", "cohort", "batch", "alumni", "startups to watch",
             "top 10", "top 20", "top 50", "member companies", "finalists",
             "showcase", "accelerator", "incubator", "our investments", "companies we")
    if not any(h in blob for h in hints):
        return False
    return not looks_like_investor_directory(url, title, text)


# --------------------------------------------------------------------------- #
# How promising is this candidate, before a single credit is spent on it?
# --------------------------------------------------------------------------- #
@dataclass
class Promise:
    """What the discovery excerpt already proves about a company.

    Run 12 researched 80 companies and only 5 cleared the four non-email gates.
    The research budget was being spread evenly over candidates that had already
    told us, in the headline that found them, whether they could ever qualify.
    Reading that first costs nothing and decides where the budget goes.
    """

    score: float = 0.0
    hopeless: str | None = None       # a reason, when the excerpt rules it out
    signals: list[str] = field(default_factory=list)


_FOUNDER_HINT = re.compile(
    r"(?i:\b(?:co-?founder|founder|chief executive|CEO|managing director)\b)")
_NAME_NEAR_ROLE = re.compile(
    r"[A-Z][a-z]+ [A-Z][a-z]+[^.]{0,40}?(?i:co-?founder|founder|CEO)"
    r"|(?i:co-?founder|founder|CEO)[^.]{0,40}?[A-Z][a-z]+ [A-Z][a-z]+")
_PLATFORM_HINT = re.compile(
    r"(?i:\b(?:platform|software|app|SaaS|marketplace|API|technology|tech|"
    r"startup|solution|tool|system)\b)")


def assess_candidate(candidate: Candidate, min_usd: float, max_usd: float) -> Promise:
    """Score a candidate on what its own discovery excerpt already establishes."""
    from ..validation.geo import country_from_demonym, is_us_country, normalise_country
    from ..validation.money import parse_amounts

    text = candidate.hint_snippet or ""
    promise = Promise()
    if not text:
        # A bare link label off a portfolio grid proves nothing either way. It
        # scores zero and is ordered by how it was found, which is what the
        # earlier evidence about discovery channels already established.
        return promise

    amounts = [a for a in parse_amounts(text) if a.qualifies_as_criterion_input]
    if amounts:
        in_band = [a for a in amounts if min_usd <= float(a.amount_usd) <= max_usd]
        if in_band:
            promise.score += 3.0
            promise.signals.append(f"in-band {in_band[0].human()} in the headline")
        else:
            # The headline already says this company is outside TVB's band. No
            # amount of research changes that, so it never becomes research.
            biggest = max(amounts, key=lambda a: float(a.amount_usd))
            promise.hopeless = f"{biggest.human()} is outside the ${min_usd/1e6:.0f}M-${max_usd/1e6:.0f}M band"
            return promise

    if _NAME_NEAR_ROLE.search(text):
        promise.score += 1.5
        promise.signals.append("a named founder or CEO")
    elif _FOUNDER_HINT.search(text):
        promise.score += 0.4

    country = country_from_demonym(text) or _country_in(text, normalise_country)
    if country:
        if is_us_country(country):
            promise.hopeless = "the headline describes a US company"
            return promise
        promise.score += 1.0
        promise.signals.append(f"a non-US country ({country})")

    if _PLATFORM_HINT.search(text):
        promise.score += 0.5

    if candidate.domain:
        promise.score += 0.5
    if candidate.source_kind == "headline":
        promise.score += 0.5
    return promise


def _country_in(text: str, normalise) -> str | None:
    """A country named outright, or the "<City>-based" form press prefers."""
    # Two words, because "San Francisco-based" and "Tel Aviv-based" are how the
    # form is usually written and a one-word pattern reads only "Francisco".
    m = re.search(r"\b((?:[A-Z][A-Za-z.'\u2019-]{1,20}[ ])?[A-Z][A-Za-z.'\u2019-]{2,24})-based\b", text)
    if m:
        from ..validation.geo import country_from_city

        return normalise(m.group(1)) or country_from_city(m.group(1))[0]
    from ..validation.geo import COUNTRY_NAMES

    for name in COUNTRY_NAMES:
        if re.search(rf"\b{re.escape(name)}\b", text, re.I):
            return name
    return None
