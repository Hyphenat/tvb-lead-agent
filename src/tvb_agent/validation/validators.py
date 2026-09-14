"""Turning grounded claims into evidenced, gate-ready fields.

Each validator consumes the extractor's claims plus whatever pages were fetched,
and produces an ``Evidenced[...]`` value.  The rule every validator follows: when
the evidence does not establish something, the field stays ``unknown`` - which
fails its gate - rather than being filled with a plausible guess.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from ..config import Settings
from ..models import (
    Evidence,
    Evidenced,
    MoneyAmount,
    Person,
    SourceAuthority,
    USPresenceAssessment,
    USPresenceLevel,
    USSignal,
    USSignalKind,
)
from ..research.extractor import Claim, ExtractionResult
from .geo import (
    country_from_city,
    country_from_demonym,
    country_from_text,
    country_from_tld,
    is_us_country,
    normalise_country,
)
from .money import best_qualifying_amount, parse_amounts


@dataclass
class SourceText:
    """A fetched page offered to the validators for direct scanning.

    Scanning the page itself (rather than only the model's quotes) is what lets
    the deterministic validators work when no LLM is configured - and the quote
    attached to each finding is cut from this same text, so the evidence is
    grounded by construction.
    """

    url: str
    text: str
    authority: SourceAuthority = SourceAuthority.UNKNOWN
    title: str = ""
    note: str = ""

    def evidence(self, quote: str, note: str = "") -> Evidence:
        combined = "; ".join(n for n in (self.note, note) if n)
        return Evidence(url=self.url, quote=quote.strip()[:500], authority=self.authority,
                        title=self.title or None, note=combined or None)


def sentence_containing(text: str, start: int, end: int, max_len: int = 400) -> str:
    """The sentence around a match, used as the verbatim quote for evidence."""
    lo = max(0, max(text.rfind(". ", 0, start), text.rfind("\n", 0, start)) + 1)
    candidates = [i for i in (text.find(". ", end), text.find("\n", end)) if i != -1]
    hi = min(candidates) + 1 if candidates else min(len(text), end + 200)
    return re.sub(r"\s+", " ", text[lo:hi]).strip()[:max_len]

# --------------------------------------------------------------------------- #
# Is this an operating company at all?
# --------------------------------------------------------------------------- #
# Phrases by which an organisation identifies itself as something other than a
# company TVB could invest in or sell to. A real run spent most of its research
# budget on venture funds, trade publications and consultancies - each one
# clearing the crawl, the extraction and the probes before failing a gate.
# A single phrase is rarely proof. Run 7 threw away Rapyd ("our clients"), Torq
# ("newsroom") and Zocks ("family office") - three real operating companies -
# because one phrase anywhere on their site was treated as a confession. So the
# phrases are split: DECISIVE ones an operating company would not write about
# itself, and SUPPORTING ones that only condemn in company with another. And
# pages that sell a product push back, because funds do not run pricing pages.
_DECISIVE = (
    ("an investor", (
        "we are a venture capital", "we are an early-stage investor",
        "we are an early stage investor", "we are a seed fund", "we are a fund",
        "venture capital firm", "venture capital fund", "our portfolio companies",
        "we invest in", "we back founders", "we lead rounds", "we write cheques",
        "we write checks", "limited partners", "assets under management",
        "our investment thesis", "we are an investor", "apply to our program",
        "apply to our programme", "our accelerator", "our cohort",
    )),
    ("a publication", (
        "our journalists", "our reporters", "editorial team", "advertise with us",
        "press releases submitted", "submit a press release", "media kit",
        "our newsroom team", "letters to the editor", "editorial guidelines",
    )),
    ("an agency or consultancy", (
        "we are an agency", "we are a consultancy", "our consultants",
        "pitch deck design", "fundraising consultancy",
    )),
    # Run 10 put Lift99 forward as a qualifying lead. It is a startup hub and
    # members' community in Tallinn - a place, not a technology company - and
    # offering it to TVB as one was the worst outcome this pipeline can produce.
    ("a workspace or community", (
        "coworking space", "co-working space", "shared office space",
        "startup hub", "community hub", "members club", "members' club",
        "event space", "book a desk", "book a tour", "hot desk", "hot desks",
        "meeting rooms available", "our members", "membership plans",
        "join our community of founders",
    )),
)

_SUPPORTING = (
    ("an investor", (
        "portfolio companies", "seed fund", "family office", "private equity firm",
        "angel syndicate", "we back", "our investments", "early-stage investor",
        "accelerator program", "accelerator programme", "backing founders",
        "we partner with founders", "venture fund",
    )),
    ("a publication", (
        "newsroom", "breaking news", "latest news and analysis", "media outlet",
        "magazine", "podcast episode", "market research reports",
        "industry analysis reports", "research and advisory firm",
    )),
    ("an agency or consultancy", (
        "our clients", "consulting services", "we help companies build",
        "advisory services for", "client engagements", "our agency",
    )),
)

# What an operating software company puts on its own site and a fund does not.
_PRODUCT_SIGNALS = (
    "start free trial", "start your free trial", "free trial", "book a demo",
    "request a demo", "get a demo", "our platform", "the platform",
    "api documentation", "developer docs", "integrations", "pricing plans",
    "per month", "sign up free", "create an account", "our product",
    "product tour", "customer support", "uptime", "sdk", "dashboard",
)


def _phrase_present(phrase: str, text: str) -> bool:
    """Whole-phrase match.

    Substring matching reads "raised $2.2 million in seed funding" as the phrase
    "seed fund" and throws away a perfectly good company.
    """
    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None


def _hits(groups, text: str) -> list[tuple[str, str]]:
    found = []
    for label, phrases in groups:
        for phrase in phrases:
            if _phrase_present(phrase, text):
                found.append((label, phrase))
    return found


def looks_like_non_operating_company(sources: Iterable[SourceText]) -> str | None:
    """Return a reason if this organisation is not a company we can qualify.

    Judged on the organisation's *own* words, on its own site, before any search
    credits or email verification are spent on it - and only when the evidence is
    decisive, or two independent phrases agree and the site is not also selling a
    product.
    """
    own = "\n".join((s.text or "")[:12_000] for s in sources
                     if s.authority is SourceAuthority.COMPANY_OWNED).lower()
    if not own.strip():
        return None

    decisive = _hits(_DECISIVE, own)
    if decisive:
        label, phrase = decisive[0]
        return f"describes itself as {label} ('{phrase}')"

    supporting = _hits(_SUPPORTING, own)
    if len(supporting) < 2:
        return None
    # Two phrases from different families ("newsroom" + "our clients") are the
    # site being wordy, not a confession; agreement has to be within one family.
    by_label: dict[str, list[str]] = {}
    for label, phrase in supporting:
        by_label.setdefault(label, []).append(phrase)
    agreed = [(lab, ph) for lab, ph in by_label.items() if len(ph) >= 2]
    if not agreed:
        return None
    if sum(1 for p in _PRODUCT_SIGNALS if _phrase_present(p, own)) >= 2:
        return None   # it sells something; funds and magazines do not
    label, phrases = agreed[0]
    return f"describes itself as {label} ('{phrases[0]}', '{phrases[1]}')"


# --------------------------------------------------------------------------- #
# Funding / revenue
# --------------------------------------------------------------------------- #
def company_name_tokens(name: str) -> list[str]:
    """Distinctive tokens used to check that a fact is about *this* company."""
    raw = [t for t in re.split(r"[^A-Za-z0-9]+", (name or "").lower()) if t]
    generic = {"the", "group", "labs", "lab", "technologies", "technology", "solutions",
               "systems", "software", "digital", "company", "holdings", "ltd", "inc",
               "pvt", "limited", "app", "io", "ai", "co"}
    strong = [t for t in raw if len(t) >= 4 and t not in generic]
    return strong or raw


def mentions_company(text: str, name: str) -> bool:
    low = (text or "").lower()
    toks = company_name_tokens(name)
    if not toks:
        return False
    compact = "".join(t for t in re.split(r"[^A-Za-z0-9]+", name.lower()) if t)
    if compact and compact in low.replace(" ", ""):
        return True
    return any(re.search(rf"\b{re.escape(t)}\b", low) for t in toks)


def validate_funding(result: ExtractionResult, settings: Settings,
                     sources: Iterable[SourceText] = (),
                     company_name: str = "") -> Evidenced[MoneyAmount]:
    """Find the figure that genuinely represents money raised or earned.

    Amounts are parsed out of real page text and out of quoted evidence - never
    out of a model's summary - so the figure and the sentence proving it always
    agree.  Only sources with standing count, and only amounts whose *type* is
    funding, revenue or ARR are eligible.
    """
    from ..models import AUTHORITY_RANK

    candidates: list[tuple[MoneyAmount, Evidence]] = []

    for claim in result.by_field("funding_statement"):
        for amt in parse_amounts(claim.evidence.quote):
            candidates.append((amt, claim.evidence))

    for src in sources:
        text = src.text[:40_000]
        own_site = src.authority is SourceAuthority.COMPANY_OWNED
        for amt in parse_amounts(text):
            sentence = sentence_containing(text, amt.start, amt.end) or amt.context or amt.raw
            # A press article routinely covers several companies in one page.
            # Unless the page *is* the company's own site, the sentence carrying
            # the figure must name the company, or the number belongs to someone
            # else - the fastest way to hand TVB a wrong number about a real firm.
            if not own_site and company_name and not mentions_company(sentence, company_name):
                continue
            candidates.append((amt, src.evidence(sentence)))

    if not candidates:
        return Evidenced.unknown()

    floor = AUTHORITY_RANK[SourceAuthority.AGGREGATOR]
    usable = [(a, e) for a, e in candidates
              if a.qualifies_as_criterion_input and AUTHORITY_RANK[e.authority] >= floor]
    if not usable:
        return Evidenced.unknown()

    lo, hi = settings.min_amount_usd, settings.max_amount_usd
    best = best_qualifying_amount([a for a, _ in usable], lo, hi)
    if best is None:
        return Evidenced.unknown()

    support = [e for a, e in usable
               if a.currency == best.currency and a.amount_original == best.amount_original]
    seen: set[str] = set()
    unique: list[Evidence] = []
    for e in support:
        if e.url not in seen:
            seen.add(e.url)
            unique.append(e)
    return Evidenced.of(best, unique[:3])


# --------------------------------------------------------------------------- #
# Technology platform
# --------------------------------------------------------------------------- #
# Words that describe an actual product, matched on whole words only. The old
# list passed a family bakery: "app" matched inside "happens", and "data",
# "product", "solution", "system" and "technology" appear on virtually every
# commercial website ever written. This gate proved nothing; now it asks for
# language a company only uses when it really ships software.
_PLATFORM_TERMS = (
    "platform", "software", "saas", "web app", "mobile app", "api", "apis",
    "dashboard", "marketplace", "machine learning", "artificial intelligence",
    "algorithm", "algorithms", "automation", "cloud-based", "our app",
    "the app", "our product", "our platform", "integrations", "sdk",
    "open source", "self-serve", "no-code", "low-code", "data platform",
    "analytics platform", "software platform", "technology platform",
    "operating system", "infrastructure for", "built on",
)
_SERVICES_ONLY_TERMS = (
    "consultancy", "consulting firm", "staffing agency", "recruitment agency", "law firm",
    "advisory firm", "marketing agency", "design agency", "outsourcing services",
    "bespoke development services", "it services company", "system integrator",
)
_PRODUCT_PAGE_HINTS = ("/product", "/platform", "/features", "/pricing", "/solutions",
                       "/docs", "/api", "/how-it-works", "/technology")


def validate_technology(result: ExtractionResult, page_urls: Iterable[str],
                        sources: Iterable[SourceText] = ()) -> Evidenced[bool]:
    """Require evidence of an actual product, not merely a sector label."""
    positives: list[Evidence] = []

    for claim in result.by_field("is_tech_platform"):
        if claim.value in (True, "true", "True", 1):
            positives.append(claim.evidence)

    for claim in result.by_field("description"):
        blob = f"{claim.value} {claim.evidence.quote}".lower()
        if any(re.search(rf"(?<![a-z]){re.escape(t)}(?![a-z])", blob) for t in _PLATFORM_TERMS):
            positives.append(claim.evidence)

    from ..models import AUTHORITY_RANK

    floor = AUTHORITY_RANK[SourceAuthority.AGGREGATOR]
    negatives = 0
    for src in sources:
        low = (src.text or "")[:30_000].lower()
        # The same standing the funding gate demands. Without it a search snippet
        # - rank 1, a fragment nobody verified - was enough to prove that a
        # company operates a technology platform.
        if AUTHORITY_RANK[src.authority] >= floor:
            for term in _PLATFORM_TERMS:
                m = re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", low)
                if m:
                    positives.append(src.evidence(
                        sentence_containing(src.text, m.start(), m.end()),
                        note="product/platform language on source"))
                    break
        if any(t in low for t in _SERVICES_ONLY_TERMS):
            negatives += 1

    for claim in result.by_field("description"):
        blob = f"{claim.value} {claim.evidence.quote}".lower()
        if any(t in blob for t in _SERVICES_ONLY_TERMS):
            negatives += 1

    urls = list(page_urls)
    has_product_page = any(any(h in u.lower() for h in _PRODUCT_PAGE_HINTS) for u in urls)

    if not positives:
        return Evidenced.unknown()
    # A pure services business is disqualified even when it says "platform".
    if negatives and not has_product_page:
        return Evidenced.unknown()

    seen: set[str] = set()
    unique: list[Evidence] = []
    for e in positives:
        if e.url not in seen:
            seen.add(e.url)
            unique.append(e)
    confidence = min(0.95, 0.55 + 0.10 * len(unique) + (0.15 if has_product_page else 0.0))
    return Evidenced.of(True, unique[:3], confidence=confidence)


# --------------------------------------------------------------------------- #
# Country
# --------------------------------------------------------------------------- #
def validate_country(result: ExtractionResult, domain: str | None,
                     sources: Iterable[SourceText] = (), company_name: str = "") -> Evidenced[str]:
    for claim in result.by_field("country"):
        country = normalise_country(str(claim.value))
        if country:
            return Evidenced.of(country, [claim.evidence])

    for claim in result.by_field("hq_city"):
        country, _ = country_from_city(str(claim.value))
        if country:
            return Evidenced.of(country, [claim.evidence])

    # "Jakarta-based", "the Vietnamese studio", "Kenyan AI startup" - how press
    # actually states where a company is. Run 7 could not establish a country
    # for real non-US companies whose location was in the opening clause of the
    # headline that found them, and the US-presence gate then failed for want
    # of one.
    based_re = re.compile(r"\b([A-Z][A-Za-z.\u2019'-]{2,24}(?:[ ][A-Z][A-Za-z.\u2019'-]{2,24})?)-based\b")
    for src in sources:
        for m in based_re.finditer(src.text or ""):
            sentence = sentence_containing(src.text, m.start(), m.end())
            if (src.authority is not SourceAuthority.COMPANY_OWNED and company_name
                    and not mentions_company(sentence, company_name)):
                continue
            place = m.group(1)
            country = normalise_country(place) or country_from_city(place)[0]
            if country:
                return Evidenced.of(country, [src.evidence(sentence)])

    hq_re = re.compile(r"(?:headquartered|head office|based|located|registered office)\s+in\s+([^\n.;]{3,60})", re.I)
    for src in sources:
        m = hq_re.search(src.text or "")
        if m:
            sentence = sentence_containing(src.text, m.start(), m.end())
            if (src.authority is not SourceAuthority.COMPANY_OWNED and company_name
                    and not mentions_company(sentence, company_name)):
                continue
            phrase = m.group(1)
            country = (normalise_country(phrase) if len(phrase.split()) <= 3 else None) \
                or country_from_text(phrase) or country_from_city(phrase)[0]
            if country:
                return Evidenced.of(country, [src.evidence(sentence_containing(src.text, m.start(), m.end()))])

    # A demonym is the weakest of the textual signals, so it is tried last and
    # only in a sentence that names the company.
    for src in sources:
        for m in re.finditer(r"[A-Za-z][^\n.;]{10,300}", src.text or ""):
            sentence = m.group(0)
            if (src.authority is not SourceAuthority.COMPANY_OWNED and company_name
                    and not mentions_company(sentence, company_name)):
                continue
            country = country_from_demonym(sentence)
            if country:
                return Evidenced.of(country, [src.evidence(sentence.strip())], confidence=0.6)

    # A country-code TLD is weak but real evidence of where a company operates.
    tld_country = country_from_tld(domain)
    if tld_country and domain:
        # This sentence appears on no page: it is generated here. It is kept
        # because a country-code domain is real signal, but it must not borrow
        # the standing of something somebody actually published - it was rank 5,
        # the same as a companies-house filing, and a US company on a .ca domain
        # could clear the US-presence gate on it alone.
        ev = Evidence(url=f"https://{domain}",
                      quote=f"Country-code domain .{domain.split('.')[-1]} registered to {tld_country}.",
                      authority=SourceAuthority.UNKNOWN,
                      note="INFERRED by this agent from the country-code TLD; not a quotation")
        return Evidenced.of(tld_country, [ev], confidence=0.45)

    return Evidenced.unknown()


# --------------------------------------------------------------------------- #
# US presence
# --------------------------------------------------------------------------- #
_SIGNAL_MAP = {
    "us_office": USSignalKind.US_OFFICE,
    "us_subsidiary": USSignalKind.US_SUBSIDIARY,
    "us_incorporation": USSignalKind.US_INCORPORATION,
    "us_job_posting": USSignalKind.US_JOB_POSTING,
    "us_address_mention": USSignalKind.US_ADDRESS_MENTION,
    "us_phone": USSignalKind.US_PHONE,
    "us_locale_page": USSignalKind.US_LOCALE_PAGE,
    "us_press_dateline": USSignalKind.US_PRESS_DATELINE,
    "hq_in_us": USSignalKind.HQ_IN_US,
}


def validate_us_presence(result: ExtractionResult, country: Evidenced[str],
                         pages_checked: Iterable[str], settings: Settings) -> Evidenced[USPresenceAssessment]:
    """Apply the documented US-presence rule.

    PASS requires a known non-US headquarters, no hard US signal of any kind, and
    at most ``max_weak_us_signals`` weak signals.  An unknown headquarters never
    passes: "we could not tell" is not the same as "there is no US presence", and
    treating it as such is how a list fills up with companies TVB cannot use.
    """
    pages = list(pages_checked)
    signals: list[USSignal] = []

    for claim in result.by_field("us_signal"):
        kind = _SIGNAL_MAP.get(str(claim.value).strip().lower())
        if kind:
            signals.append(USSignal(kind=kind, evidence=claim.evidence))

    hq = country.value
    if hq and is_us_country(hq) and country.evidence:
        signals.append(USSignal(kind=USSignalKind.HQ_IN_US, evidence=country.evidence[0]))

    assessment = USPresenceAssessment(
        hq_country=hq, signals=signals, pages_checked=pages[:20],
    )

    if not country.known or not hq:
        assessment.level = USPresenceLevel.UNKNOWN
        assessment.rationale = ("Headquarters country could not be established from the evidence, "
                                "so US presence cannot be ruled out.")
        return Evidenced.unknown()

    if is_us_country(hq):
        assessment.level = USPresenceLevel.SIGNIFICANT
        assessment.rationale = f"Headquarters is in the United States ({hq})."
        return Evidenced.of(assessment, country.evidence)

    hard = assessment.hard_signals
    weak = assessment.weak_signal_count

    if hard:
        assessment.level = USPresenceLevel.SIGNIFICANT
        kinds = ", ".join(sorted({s.kind.value for s in hard}))
        assessment.rationale = f"Non-US headquarters ({hq}) but disqualifying US footprint found: {kinds}."
        return Evidenced.of(assessment, [s.evidence for s in hard][:3])

    if weak > settings.max_weak_us_signals:
        assessment.level = USPresenceLevel.SIGNIFICANT
        assessment.rationale = (f"Non-US headquarters ({hq}) but {weak} weak US signals exceed the "
                                f"threshold of {settings.max_weak_us_signals}.")
        return Evidenced.of(assessment, [s.evidence for s in assessment.signals][:3])

    assessment.level = USPresenceLevel.MINIMAL if weak else USPresenceLevel.NONE
    checked = len(pages)
    assessment.rationale = (
        f"Headquarters in {hq}. No US office, subsidiary, US incorporation or US job posting found "
        f"across {checked} page(s) checked; {weak} weak signal(s), within the threshold of "
        f"{settings.max_weak_us_signals}."
    )
    ev = list(country.evidence) + [s.evidence for s in assessment.signals][:2]
    return Evidenced.of(assessment, ev)


# --------------------------------------------------------------------------- #
# Founder
# --------------------------------------------------------------------------- #
_TITLE_RANK = [
    (("co-founder", "cofounder", "co founder"), 5),
    (("founder",), 5),
    (("ceo", "chief executive"), 4),
    (("managing director", "md"), 3),
]


# Domains that announce an investor before a word of the page is read. Run 13
# offered TVB "Serena" at serena.vc - a French venture firm - as a qualifying
# lead. A .vc domain is a venture fund essentially always, and no amount of
# reading the copy is needed to know it.
_INVESTOR_TLDS = (".vc", ".fund", ".capital", ".ventures")
_INVESTOR_DOMAIN_WORDS = ("ventures", "venturepartners", "capitalpartners",
                          "seedfund", "venturefund", "growthfund", "angelfund")


def domain_is_an_investor(domain: str | None) -> str | None:
    """Does the company's own address say it is a fund rather than a company?"""
    host = (domain or "").lower().strip()
    if not host:
        return None
    host = host[4:] if host.startswith("www.") else host
    for tld in _INVESTOR_TLDS:
        if host.endswith(tld):
            return f"{host} is a {tld} domain, which is a venture fund's address"
    base = host.split(".")[0].replace("-", "")
    for word in _INVESTOR_DOMAIN_WORDS:
        if word in base:
            return f"{host} names itself an investor"
    return None


def names_a_role_not_a_person(name: str) -> bool:
    """Is this a job description or a form label rather than a person?"""
    from ..research.extractor import GroundedExtractor

    return (GroundedExtractor._carries_a_role_word(name)
            or GroundedExtractor._is_a_form_label(name))


def _title_score(title: str) -> int:
    t = (title or "").lower()
    score = 0
    for keys, val in _TITLE_RANK:
        if any(k in t for k in keys):
            score = max(score, val)
    if "ceo" in t and ("founder" in t):
        score += 2
    return score


def validate_founder(result: ExtractionResult, company_name: str = "") -> Evidenced[Person]:
    """Pick the best-evidenced founder/CEO of *this* company.

    The funding and country validators both refuse a fact taken from a page that
    is not the company's own unless the sentence names the company. This one did
    not, and a funding article names the lead investor, their partners, other
    founders in the same round and the reporter - any of whom could be read as
    this company's CEO. That is exactly how a trade journalist was once recorded
    as a company's founder and shipped as a lead.
    """
    best: tuple[int, Claim] | None = None
    for claim in result.by_field("founder"):
        own_site = claim.evidence.authority is SourceAuthority.COMPANY_OWNED
        if not own_site and company_name and not mentions_company(claim.evidence.quote, company_name):
            continue
        # A role is not a person. Checked again here because a model can supply
        # a claim the rule-based extractor never saw, and a lead whose founder
        # is "Ex-Pipedrive Founder" is worse than no lead at all.
        if names_a_role_not_a_person(str(claim.value)):
            continue
        title = str(claim.extra.get("title") or "")
        score = _title_score(title)
        if score == 0:
            # The title may only appear in the quote itself.
            score = _title_score(claim.evidence.quote)
        if score == 0:
            continue
        authority_bonus = claim.evidence.rank
        total = score * 10 + authority_bonus
        if best is None or total > best[0]:
            best = (total, claim)

    if not best:
        return Evidenced.unknown()

    claim = best[1]
    title = str(claim.extra.get("title") or "").strip()
    if not title:
        m = re.search(r"(co[- ]?founder[^,.;]{0,24}|chief executive officer|CEO|managing director)",
                      claim.evidence.quote, re.I)
        title = m.group(1).strip() if m else None

    support = [claim.evidence]
    for other in result.by_field("founder"):
        if (str(other.value).lower() == str(claim.value).lower()
                and other.evidence.url != claim.evidence.url):
            support.append(other.evidence)
    return Evidenced.of(Person(name=str(claim.value).strip(), title=title), support[:3])

# --------------------------------------------------------------------------- #
# Re-checking leads that were banked under older, weaker rules
# --------------------------------------------------------------------------- #
def lead_fails_current_rules(profile) -> str | None:
    """Would this stored lead still qualify under the rules as they stand now?

    A lead banked by an earlier build is not grandfathered in. Two of them -
    a trade journalist recorded as a company's founder, and an office mailbox
    recorded as a founder's address - survived in the database after the holes
    that produced them were closed, and an export would have handed both to TVB
    as verified. Every read is re-checked, so a fix applies retroactively.
    """
    from ..providers.email_verify import is_role_account
    from ..research.authority import TIER1_PRESS_HOSTS, classify_authority

    founder = profile.founder.value.name if profile.founder.value else ""
    if not founder:
        return "no founder on record"
    if names_a_role_not_a_person(founder):
        return f"'{founder}' is a job description, not a person"

    address = (profile.email.address or "").strip()
    if not address:
        return "no email address on record"
    if is_role_account(address):
        return f"{address} is a shared mailbox, not a founder's address"

    # The publication that covered a company is not the company, and neither is
    # the fund that invested in one.
    domain = (profile.domain or "").lower()
    investor = domain_is_an_investor(domain)
    if investor:
        return investor
    if domain:
        host = domain[4:] if domain.startswith("www.") else domain
        if host in TIER1_PRESS_HOSTS or any(host.endswith("." + h) for h in TIER1_PRESS_HOSTS):
            return f"{host} is a publication, not the company's own site"
        if classify_authority(f"https://{host}/") is SourceAuthority.SOCIAL_PROFILE:
            return f"{host} is a social profile, not the company's own site"
        # Names only a publication gives itself. Deliberately narrower than the
        # press heuristic used for source standing: "tech", "startup" and
        # "digital" are in half the startup domains on earth, but nobody calls
        # their SaaS product the Gazette.
        base = host.split(".")[0]
        for marker in ("insider", "magazine", "journal", "gazette", "herald", "tribune",
                       "times", "newsroom", "reporter", "weekly", "-news", "news-",
                       "thenews", "dailynews", "presse"):
            if marker in base:
                return f"{host} names itself a publication, not a company TVB can invest in"

    # The address has to belong to the company, or to the founder by name.
    from .email import domain_matches_company, local_part_matches_name

    if not domain_matches_company(address, domain) and not local_part_matches_name(address, founder):
        return (f"{address} belongs to neither {domain or 'the company'} "
                f"nor to {founder} by name")

    # And the band is re-checked on every read. The stored verdict is restored
    # verbatim from JSON, so a lead banked under a different MIN/MAX_AMOUNT_USD -
    # or before the funding rules were tightened - would be re-exported as
    # qualified without anything re-evaluating it.
    from ..config import get_settings

    money = profile.funding.value
    if money is None:
        return "no funding or revenue figure on record"
    settings = get_settings()
    usd = float(money.amount_usd)
    if usd < settings.min_amount_usd or usd > settings.max_amount_usd:
        return (f"{money.human()} is outside the current "
                f"${settings.min_amount_usd/1e6:.0f}M-${settings.max_amount_usd/1e6:.0f}M band")
    if not profile.email.is_verified:
        return f"the email is {profile.email.status.value}, not verified"
    return None
