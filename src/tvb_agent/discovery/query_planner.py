"""Search-strategy generation.

The agent does not hold a list of companies; it holds a *space of places to
look*.  A cell is one combination of

    (sector x geography x funding-phrasing x time-window x source-shape)

and the planner picks cells by novelty and by how productive similar cells have
been in past runs.  Because the exploration state lives in SQLite, a second run
starts from different cells than the first, which is what makes "find new
companies again" a structural property rather than a promise.

Sectors mirror TVB's Orbits and geographies mirror TVB's Hubs plus the emerging
markets named in the overview, so the search space is aimed at the companies TVB
actually wants rather than at startups in general.
"""

from __future__ import annotations

import datetime
import hashlib
import random
from collections.abc import Iterable
from dataclasses import dataclass

# --- TVB Orbits -------------------------------------------------------------
SECTORS: list[tuple[str, list[str]]] = [
    ("healthtech", ["digital health", "health tech", "care coordination platform", "telemedicine platform"]),
    ("edtech", ["edtech", "workforce development platform", "skills training platform", "learning platform"]),
    ("ai", ["AI startup", "applied AI platform", "AI agents platform", "machine learning platform"]),
    ("cybersecurity", ["cybersecurity startup", "security platform", "compliance automation platform"]),
    ("digital_twin", ["digital twin platform", "simulation platform", "industrial IoT platform"]),
    ("fintech", ["fintech startup", "payments platform", "embedded finance platform", "lending platform"]),
    ("traveltech", ["travel tech startup", "travel booking platform", "travel payments platform"]),
    ("b2b_saas", ["B2B SaaS startup", "SaaS platform", "enterprise software startup"]),
    ("b2b2c", ["B2B2C platform", "marketplace platform", "white-label platform"]),
    ("proptech", ["proptech platform", "construction tech platform"]),
    ("logistics", ["logistics tech platform", "supply chain software"]),
    ("agritech", ["agritech platform", "agriculture technology startup"]),
    ("hrtech", ["HR tech platform", "recruitment platform", "talent platform"]),
    ("insurtech", ["insurtech platform", "insurance technology startup"]),
    ("climatetech", ["climate tech platform", "energy management software"]),
    ("legaltech", ["legaltech platform", "contract automation platform"]),
    ("retailtech", ["retail tech platform", "commerce enablement platform"]),
]

# --- TVB Hubs and adjacent non-US markets ----------------------------------
GEOGRAPHIES: list[tuple[str, list[str]]] = [
    ("india", ["India", "Bengaluru", "Mumbai", "Delhi NCR", "Hyderabad", "Pune", "Chennai"]),
    ("uk", ["UK", "London", "Manchester", "Edinburgh", "Bristol"]),
    ("france", ["France", "Paris", "Lyon", "Toulouse"]),
    ("uae", ["UAE", "Dubai", "Abu Dhabi"]),
    ("saudi", ["Saudi Arabia", "Riyadh"]),
    ("singapore", ["Singapore"]),
    ("germany", ["Germany", "Berlin", "Munich", "Hamburg"]),
    ("netherlands", ["Netherlands", "Amsterdam", "Rotterdam"]),
    ("spain", ["Spain", "Madrid", "Barcelona"]),
    ("italy", ["Italy", "Milan"]),
    ("nordics", ["Sweden", "Stockholm", "Denmark", "Copenhagen", "Norway", "Oslo", "Finland", "Helsinki"]),
    ("cee", ["Poland", "Warsaw", "Czech Republic", "Prague", "Romania", "Bucharest", "Estonia", "Tallinn"]),
    ("iberia_pt", ["Portugal", "Lisbon", "Porto"]),
    ("ireland", ["Ireland", "Dublin"]),
    ("switzerland", ["Switzerland", "Zurich", "Geneva"]),
    ("brazil", ["Brazil", "Sao Paulo"]),
    ("mexico", ["Mexico", "Mexico City"]),
    ("latam_other", ["Colombia", "Bogota", "Chile", "Santiago", "Argentina", "Buenos Aires", "Peru", "Lima"]),
    ("nigeria", ["Nigeria", "Lagos"]),
    ("kenya", ["Kenya", "Nairobi"]),
    ("south_africa", ["South Africa", "Cape Town", "Johannesburg"]),
    ("egypt", ["Egypt", "Cairo"]),
    ("indonesia", ["Indonesia", "Jakarta"]),
    ("vietnam", ["Vietnam", "Ho Chi Minh City", "Hanoi"]),
    ("malaysia", ["Malaysia", "Kuala Lumpur"]),
    ("thailand", ["Thailand", "Bangkok"]),
    ("philippines", ["Philippines", "Manila"]),
    ("pakistan", ["Pakistan", "Karachi", "Lahore"]),
    ("bangladesh", ["Bangladesh", "Dhaka"]),
    ("turkey", ["Turkey", "Istanbul"]),
    ("israel", ["Israel", "Tel Aviv"]),
    ("australia", ["Australia", "Sydney", "Melbourne"]),
    ("newzealand", ["New Zealand", "Auckland"]),
    ("canada", ["Canada", "Toronto", "Vancouver", "Montreal"]),
    ("japan", ["Japan", "Tokyo"]),
    ("korea", ["South Korea", "Seoul"]),
]

# --- how a funding fact tends to be worded, in several languages ------------
PHRASINGS: list[tuple[str, list[str]]] = [
    ("seed_usd", ['"raises $2 million" seed', '"raises $3 million" seed', '"secures $1.5 million" seed',
                  '"$2.5 million seed round"', '"raised $4 million" seed round']),
    ("preseries_a", ['"pre-Series A" "raises"', '"pre-Series A funding" million',
                     '"Series A" "$3 million"', '"bridge round" "$2 million" startup']),
    ("generic_round", ['"closes" "million" "funding round" startup platform',
                       '"secures" "million" "in funding" technology startup',
                       '"oversubscribed" seed round startup platform']),
    ("inr", ['"raises Rs" crore startup funding', '"crore" "pre-Series A" startup',
             '"₹" crore seed funding startup platform']),
    ("eur", ['"lève" "millions d\'euros" startup', '"Millionen Euro" Finanzierungsrunde Startup',
             '"ronda de financiación" "millones" startup', '"levée de fonds" "millions" startup']),
    ("other_ccy", ['"AED" million funding startup', '"S$" million seed startup',
                   '"R$" milhões startup rodada', '"naira" million funding startup']),
    ("revenue", ['startup "ARR" "$2 million" platform', '"annual recurring revenue" "$3 million" startup',
                 'startup "revenue of" "$2 million" platform']),
    ("investor_led", ['seed round "led by" startup platform million',
                      '"participation from" seed round startup million']),
]

# --- rolling windows; the agent prefers recent news ------------------------
# Recency first. A round announced in the last few days is the best possible
# candidate: the coverage is indexed, the amount and the founder are in the
# headline, the company's site is live, and nobody has researched it yet. Broad
# year windows compete with everything already written about that year.
def _recent_windows() -> list[str]:
    today = datetime.date.today()
    month = today.strftime("%B %Y")
    last_month = (today.replace(day=1) - datetime.timedelta(days=1)).strftime("%B %Y")
    return ["this week", "past 7 days", month, last_month]


WINDOWS: list[str] = [
    *_recent_windows(),
    "2026", "2026 Q2", "2026 Q1", "2025 H2", "2025", "last 12 months",
]

# --- the shape of the page we hope to land on -----------------------------
# Verified against the live web before being weighted. Funding roundups and
# funding-news trackers list many companies per page WITH the amount, the round
# and often the founders' names - far and away the richest channel. VC portfolio
# pages, by contrast, mostly surfaced the funds themselves: a real run spent
# most of its budget researching venture firms, media outlets and directories.
SOURCE_SHAPES: list[str] = [
    "in_band_amount",
    "funding_roundup",
    "funding_news",
    "funding_tracker",
    "accelerator_cohort",
    "vc_portfolio",
    "startup_directory",
    "company_site",
    "award_list",
]

# Sites that publish structured funding coverage, one company per article or a
# daily/weekly digest of many. Confirmed to be fetchable and to carry amounts.
FUNDING_NEWS_SITES: list[str] = [
    "thesaasnews.com", "startuptalky.com", "beststartup.in", "entrackr.com",
    "inc42.com", "yourstory.com", "eu-startups.com", "tech.eu", "sifted.eu",
    "siliconcanals.com", "techcabal.com", "disrupt-africa.com", "wamda.com",
    "techinasia.com", "e27.co", "startuprise.co.uk", "maddyness.com",
    "vccircle.com", "contxto.com", "labsnews.com", "arabianbusiness.com",
    # Added after checking live results: each returned single-company funding
    # headlines carrying the amount, the round and the country.
    "technode.global", "disruptafrica.com", "vir.com.vn", "digitalnewsasia.com",
    "launchbaseafrica.com", "techinafrica.com", "ffnews.com", "fintech.global",
    "menabytes.com", "khaleejtimes.com", "startupdaily.net", "itnews.asia",
]

# Exact-amount phrases inside TVB's $1M-$5M band. Searching for the *number*
# rather than the word "seed" is the one shape whose results are in-band by
# construction: the company is discovered by the very figure that has to clear
# the funding gate, and the headline carrying it becomes the evidence. Checked
# live - "logistics startup Vietnam \"$1.5 million\" seed round raises 2026"
# returned a Vietnamese company with an in-band round in the headline.
IN_BAND_PHRASES: list[str] = [
    '"raises $1.2 million"', '"raises $1.5 million"', '"raises $2 million"',
    '"raises $2.5 million"', '"raises $3 million"', '"raises $3.5 million"',
    '"raises $4 million"', '"raises $4.5 million"', '"secures $1.5 million"',
    '"secures $2 million"', '"secures $3 million"', '"closes $2 million"',
    '"closes $2.5 million"', '"raises $1.8M"', '"raises $2.2M"', '"raises $3.2M"',
    '"raised $2 million"', '"raised $3 million"', '"$2 million seed round"',
    '"$1.5 million seed round"', '"$3 million seed round"', '"$4 million seed"',
    '"raises $2.4 million"', '"million pre-seed round"',
]

# Euro and sterling phrasings, used only where the round would be reported in
# that currency: '"raises £2 million" South Korea' is a query that can only
# return nothing.
EURO_PHRASES: list[str] = [
    '"raises €2 million"', '"raises €3 million"', '"€2 million seed"',
    '"secures €1.5 million"', '"€3 million seed round"', '"raises €2.5 million"',
]
STERLING_PHRASES: list[str] = [
    '"raises £2 million"', '"raises £3 million"', '"£2 million seed round"',
    '"secures £1.5 million"',
]
EURO_GEOGRAPHIES = {"france", "germany", "netherlands", "spain", "italy", "nordics",
                    "cee", "iberia_pt", "ireland"}


def amount_phrases_for(geography: str) -> list[str]:
    """USD everywhere, plus the local currency where rounds are reported in it."""
    if geography == "uk":
        return IN_BAND_PHRASES + STERLING_PHRASES
    if geography in EURO_GEOGRAPHIES:
        return IN_BAND_PHRASES + EURO_PHRASES
    return IN_BAND_PHRASES

SOURCE_SHAPE_TEMPLATES: dict[str, list[str]] = {
    # The number first: in-band by construction.
    "in_band_amount": [
        "{amount} {sector} startup {geo} {window}",
        "{amount} {geo} startup {window} founder CEO",
        "{sector} startup {geo} {amount} {window}",
    ],
    # Digests: one page, many funded companies, amounts and founders included.
    "funding_roundup": [
        "startup funding roundup {geo} {window} seed round raised",
        "{geo} startups raised seed funding {window} roundup",
        "{geo} startup funding news {window} raised million seed",
    ],
    # Single-company funding announcements on trackers that state HQ and CEO.
    "funding_tracker": [
        '{sector} startup {geo} "raises" "seed round" {window} founder CEO',
        '{sector} startup {geo} "raises" million seed funding {window}',
        '{geo} {sector} startup "raises" "pre-Series A" {window}',
    ],
    "funding_news": ["{phrase} {sector} {geo} {window}", "{sector} startup {geo} funding news {window}"],
    "vc_portfolio": ["{geo} venture capital portfolio {sector} companies seed",
                     "{geo} seed fund portfolio companies {sector}"],
    "accelerator_cohort": ["{geo} accelerator cohort {sector} startups {window}",
                           "{geo} startup incubator batch {sector} {window}"],
    "startup_directory": ["list of {sector} startups in {geo} {window}",
                          "top {sector} startups {geo} seed funded {window}"],
    "company_site": ['{sector} {geo} startup "our platform" "founded in" team',
                     '{sector} company {geo} "about us" founder CEO platform'],
    "award_list": ["{geo} {sector} startup awards finalists {window}",
                   "{geo} emerging {sector} companies to watch {window}"],
}


# Markets with mandatory published-imprint rules (Impressum, mentions legales,
# colofon). Companies there must publish a named, contactable representative, so
# the scarcest requirement in this whole pipeline - a verified founder email -
# is materially easier to satisfy. This is a yield bias, not a relaxation:
# every company from these markets still clears exactly the same five gates.
IMPRINT_LAW_GEOGRAPHIES: frozenset[str] = frozenset({
    "germany", "switzerland", "france", "netherlands", "cee", "nordics", "spain", "italy",
})


@dataclass(frozen=True)
class Cell:
    sector: str
    geography: str
    phrasing: str
    window: str
    source_shape: str

    @property
    def key(self) -> str:
        raw = f"{self.sector}|{self.geography}|{self.phrasing}|{self.window}|{self.source_shape}"
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    def parts(self) -> dict[str, str]:
        return {
            "sector": self.sector, "geography": self.geography, "phrasing": self.phrasing,
            "window": self.window, "source_shape": self.source_shape,
        }

    def label(self) -> str:
        return f"{self.sector}/{self.geography}/{self.source_shape}/{self.window}"


def _pick(options: list[str], rng: random.Random) -> str:
    return rng.choice(options)


def _dedupe_words(query: str) -> str:
    """Collapse accidental repeats like "cybersecurity startup startup"."""
    out: list[str] = []
    for tok in query.split():
        if out and tok.lower() == out[-1].lower():
            continue
        out.append(tok)
    return " ".join(out)


class QueryPlanner:
    """Chooses where to look next and renders concrete search queries."""

    def __init__(self, cell_stats: dict[str, dict] | None = None, seen_queries: set[str] | None = None,
                 seed: int | None = None,
                 sectors: Iterable[str] | None = None, geographies: Iterable[str] | None = None):
        self.cell_stats = cell_stats or {}
        self.seen_queries = seen_queries or set()
        self.rng = random.Random(seed)
        self.sector_filter = set(sectors) if sectors else None
        self.geo_filter = set(geographies) if geographies else None

    # ------------------------------------------------------------------ #
    def _sectors(self) -> list[tuple[str, list[str]]]:
        return [s for s in SECTORS if not self.sector_filter or s[0] in self.sector_filter] or SECTORS

    def _geographies(self) -> list[tuple[str, list[str]]]:
        return [g for g in GEOGRAPHIES if not self.geo_filter or g[0] in self.geo_filter] or GEOGRAPHIES

    def score_cell(self, cell: Cell) -> float:
        """Higher is more attractive.

        Novelty dominates so that each run moves into fresh ground, but a cell
        family that has produced qualified leads before keeps a standing bonus.
        """
        stat = self.cell_stats.get(cell.key)
        times_used = stat["times_used"] if stat else 0
        novelty = 1.0 / (1.0 + times_used)

        sibling_cands = sibling_quals = sibling_uses = 0
        for s in self.cell_stats.values():
            if s.get("sector") == cell.sector or s.get("geography") == cell.geography:
                sibling_cands += s.get("candidates_found", 0)
                sibling_quals += s.get("qualified_found", 0)
                sibling_uses += s.get("times_used", 0)
        yield_rate = (sibling_cands + 3.0 * sibling_quals) / (1.0 + sibling_uses)

        if cell.window in _recent_windows():
            recency_bonus = 0.45          # days old, and nobody has looked yet
        elif cell.window in ("2026", "2026 Q1", "2026 Q2"):
            recency_bonus = 0.25
        else:
            recency_bonus = 0.0
        # Weighted by what each shape actually returned on the live web.
        shape_bonus = {
            # Searching the figure itself is the only shape that is in-band by
            # construction, so it outranks everything else.
            "in_band_amount": 0.50,
            # Measured on the live web: the tracker phrasing returns company
            # names in headlines with in-band amounts; roundups return digests
            # listing several funded companies at once.
            "funding_tracker": 0.45,
            "funding_news": 0.35,
            "funding_roundup": 0.30,
            "accelerator_cohort": 0.10,
            "award_list": 0.05,
            "startup_directory": -0.10,
            "company_site": 0.0,
            # Portfolio pages mostly surface the fund, not its companies.
            "vc_portfolio": -0.20,
        }.get(cell.source_shape, 0.0)
        # The binding constraint on this whole pipeline is a *verified founder
        # email*, and imprint-law markets are the only places where one is
        # reliably published: a named, contactable representative is a legal
        # requirement there. Weighted accordingly - this is a yield bias, not a
        # relaxation of any requirement.
        imprint_bonus = 0.28 if cell.geography in IMPRINT_LAW_GEOGRAPHIES else 0.0
        return (0.60 * novelty + 0.30 * min(yield_rate, 3.0) / 3.0
                + recency_bonus + shape_bonus + imprint_bonus + self.rng.uniform(0, 0.05))

    def propose_cells(self, n: int) -> list[Cell]:
        """Sample a candidate pool, then keep the best-scoring distinct cells."""
        pool: list[Cell] = []
        sectors, geos = self._sectors(), self._geographies()
        for _ in range(max(n * 12, 60)):
            pool.append(
                Cell(
                    sector=self.rng.choice(sectors)[0],
                    geography=self.rng.choice(geos)[0],
                    phrasing=self.rng.choice(PHRASINGS)[0],
                    window=self.rng.choice(WINDOWS),
                    source_shape=self.rng.choice(SOURCE_SHAPES),
                )
            )
        uniq: dict[str, Cell] = {c.key: c for c in pool}
        ranked = sorted(uniq.values(), key=self.score_cell, reverse=True)
        return self._diversify(ranked, n)

    @staticmethod
    def _diversify(ranked: list[Cell], n: int) -> list[Cell]:
        """Let the best shape lead without letting it take the whole run.

        Scoring alone hands every slot to one shape, and a run that only ever
        searches one way stops discovering new kinds of source - which is the
        one thing the brief asks the agent to keep doing. So no shape may take
        more than half the slots while other shapes are still waiting.
        """
        cap = max(1, (n + 1) // 2)
        chosen: list[Cell] = []
        deferred: list[Cell] = []
        used: dict[str, int] = {}
        for cell in ranked:
            if len(chosen) >= n:
                break
            if used.get(cell.source_shape, 0) >= cap:
                deferred.append(cell)
                continue
            chosen.append(cell)
            used[cell.source_shape] = used.get(cell.source_shape, 0) + 1
        for cell in deferred:
            if len(chosen) >= n:
                break
            chosen.append(cell)
        return chosen[:n]

    def render_queries(self, cell: Cell, per_cell: int = 2) -> list[str]:
        """Turn a cell into concrete, de-duplicated search strings."""
        sector_terms = dict(SECTORS)[cell.sector]
        geo_terms = dict(GEOGRAPHIES)[cell.geography]
        phrases = dict(PHRASINGS)[cell.phrasing]
        templates = SOURCE_SHAPE_TEMPLATES[cell.source_shape]

        out: list[str] = []
        attempts = 0
        while len(out) < per_cell and attempts < per_cell * 8:
            attempts += 1
            q = _pick(templates, self.rng).format(
                sector=_pick(sector_terms, self.rng),
                geo=_pick(geo_terms, self.rng),
                phrase=_pick(phrases, self.rng),
                amount=_pick(amount_phrases_for(cell.geography), self.rng),
                window=cell.window,
            )
            q = _dedupe_words(q)
            if q not in self.seen_queries and q not in out:
                out.append(q)
        # Everything already tried? Vary it rather than repeating verbatim.
        if not out:
            base = _pick(templates, self.rng).format(
                sector=_pick(sector_terms, self.rng), geo=_pick(geo_terms, self.rng),
                phrase=_pick(phrases, self.rng), window=cell.window)
            out = [f"{base} {self.rng.choice(['startup', 'company', 'platform', 'founders'])}"]
        return out

    def targeted_queries(self, company: str, domain: str | None = None) -> dict[str, list[str]]:
        """Per-company probes used during the research stage."""
        d = f" {domain}" if domain else ""
        return {
            "funding": [f'"{company}" funding raised million seed round',
                        f'"{company}" "Series A" OR "seed" funding announcement'],
            "founder": [f'"{company}" founder CEO co-founder',
                        f'"{company}" CEO interview founder name'],
            "email": [f'"{company}" founder email contact{d}',
                      f'"{company}" CEO email address press contact'],
            "location": [f'"{company}" headquarters office location',
                         f'"{company}" "based in" headquarters country'],
            "us_presence": [f'"{company}" "United States" office OR subsidiary OR "New York" OR "San Francisco"',
                            f'"{company}" careers jobs "United States"'],
        }
