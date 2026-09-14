"""Grounded fact extraction.

The model's only job is to *locate* facts in text that has already been fetched
and to quote the sentence it found each one in.  Every returned claim is then
checked by :mod:`grounding` against the exact source text; unverifiable claims
are dropped and counted.  When no LLM is configured at all, a deterministic
rule-based path produces a smaller but equally grounded set of claims, so the
system never depends on a model being available in order to be honest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..models import Evidence, SourceAuthority
from ..providers.email_verify import find_emails_in_text, is_role_account
from ..providers.llm import LLMService
from .grounding import snippet_around, verify_quote

MAX_SOURCE_CHARS = 9_000
MAX_TOTAL_CHARS = 40_000


@dataclass
class Source:
    url: str
    text: str
    authority: SourceAuthority = SourceAuthority.UNKNOWN
    title: str = ""
    note: str = ""   # e.g. "search-engine excerpt; full page was not retrievable"


@dataclass
class Claim:
    field: str
    value: Any
    evidence: Evidence
    grounding_mode: str = "exact"
    extra: dict = field(default_factory=dict)


@dataclass
class ExtractionResult:
    claims: list[Claim] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)   # ungrounded claims, kept for the audit trail
    used_llm: bool = False

    def by_field(self, name: str) -> list[Claim]:
        return [c for c in self.claims if c.field == name]

    def first(self, name: str) -> Claim | None:
        got = self.by_field(name)
        return got[0] if got else None


SYSTEM_PROMPT = """You extract facts about companies from web pages for an investment-research pipeline.

ABSOLUTE RULES:
1. Only report a fact if it is stated in the provided sources. Never use outside knowledge.
2. For every fact, copy the EXACT sentence from the source that states it into "quote".
   Copy it verbatim, character for character. Do not paraphrase, summarise or translate it.
3. If a fact is not stated in the sources, omit that field entirely. Omitting is always correct
   when you are unsure. A wrong answer is far worse than a missing one.
4. "source" must be the integer index of the source the quote came from.

Return ONLY a JSON object with this shape (every key optional):
{
  "description": {"value": "<one sentence on what the company does>", "quote": "...", "source": 0},
  "sector": {"value": "<e.g. fintech, healthtech, B2B SaaS>", "quote": "...", "source": 0},
  "country": {"value": "<country of headquarters>", "quote": "...", "source": 0},
  "hq_city": {"value": "<city>", "quote": "...", "source": 0},
  "is_tech_platform": {"value": true, "quote": "<sentence describing the product/platform>", "source": 0},
  "funding_statements": [{"text": "<sentence stating money raised or revenue>", "quote": "...", "source": 0}],
  "founders": [{"name": "<person>", "title": "<their role>", "quote": "...", "source": 0}],
  "emails": [{"address": "<email>", "owner": "<whose it is, if stated>", "quote": "...", "source": 0}],
  "us_signals": [{"kind": "<us_office|us_subsidiary|us_incorporation|us_job_posting|us_address_mention>",
                  "quote": "...", "source": 0}]
}"""


def _fmt_sources(sources: list[Source]) -> str:
    parts = []
    budget = MAX_TOTAL_CHARS
    for i, s in enumerate(sources):
        if budget <= 0:
            break
        chunk = s.text[: min(MAX_SOURCE_CHARS, budget)]
        budget -= len(chunk)
        parts.append(f"--- SOURCE {i} | {s.url} ---\n{chunk}")
    return "\n\n".join(parts)


class GroundedExtractor:
    def __init__(self, llm: LLMService | None = None, on_event=None):
        self.llm = llm
        self.on_event = on_event or (lambda *a, **k: None)
        self.grounded_ok = 0
        self.grounded_dropped = 0

    # ------------------------------------------------------------------ #
    # Fields worth spending a model call on. If the deterministic pass already
    # established these, the model has nothing to add that is worth waiting for.
    _KEY_FIELDS = ("description", "country", "founder", "email")

    async def extract(self, company_name: str, sources: list[Source]) -> ExtractionResult:
        """Rules first, model only for what the rules could not settle.

        The rule-based pass is free and instant; the model is rate-limited and,
        on a free tier, can be slower than every other stage combined. Running
        the model on every company made it the bottleneck for the whole
        pipeline - a real run spent 17 minutes researching 16 companies, almost
        all of it waiting on model calls that added nothing the regexes had not
        already found.
        """
        sources = [s for s in sources if s.text and s.text.strip()]
        result = ExtractionResult()
        if not sources:
            return result

        # 1. Deterministic pass. Always runs, costs nothing, and catches things
        #    models routinely miss (obfuscated emails especially).
        self._ingest_rules(company_name, sources, result)

        if not result.by_field("sector"):
            found = self._rule_sector(sources)
            if found:
                label, quote, src = found
                if verify_quote(quote, src.text).ok:
                    result.claims.append(
                        Claim(field="sector", value=label,
                              evidence=self._make_evidence(quote, src, "rule-based classification"))
                    )

        # 2. Model pass, only for the gaps that actually matter.
        missing = [f for f in self._KEY_FIELDS if not result.by_field(f)]
        if not missing or self.llm is None or not self.llm.has_model:
            return result

        user = (
            f"Company under investigation: {company_name}\n\n"
            f"{_fmt_sources(sources)}\n\n"
            f"Extract only what these sources state about {company_name}. "
            f"Ignore facts about other companies mentioned in passing. "
            f"The following are still unknown and matter most: {', '.join(missing)}."
        )
        try:
            data = await self.llm.complete_json(SYSTEM_PROMPT, user)
        except Exception as e:  # pragma: no cover - defensive
            self.on_event(f"extraction LLM error for {company_name}: {type(e).__name__}", "debug")
            data = None

        if isinstance(data, dict):
            result.used_llm = True
            self._ingest_llm(data, sources, result)
        return result

    # ------------------------------------------------------------------ #
    def _make_evidence(self, quote: str, src: Source, note: str = "") -> Evidence:
        combined = "; ".join(n for n in (src.note, note) if n)
        return Evidence(
            url=src.url,
            quote=snippet_around(quote, src.text) or quote,
            authority=src.authority,
            title=src.title or None,
            note=combined or None,
        )

    def _ground(self, quote: str, src_index: Any, sources: list[Source], field_name: str,
                result: ExtractionResult) -> tuple[Source, str] | None:
        """Validate a model-supplied quote; returns ``(source, mode)`` or None."""
        try:
            idx = int(src_index)
        except (TypeError, ValueError):
            idx = -1

        order = ([sources[idx]] if 0 <= idx < len(sources) else []) + [
            s for j, s in enumerate(sources) if j != idx
        ]
        for src in order:
            res = verify_quote(quote, src.text)
            if res.ok:
                self.grounded_ok += 1
                return src, res.mode

        self.grounded_dropped += 1
        result.dropped.append({"field": field_name, "quote": (quote or "")[:200],
                               "reason": "quote not found in any fetched source"})
        return None

    def _add(self, result: ExtractionResult, field_name: str, value: Any, quote: str,
             src_index: Any, sources: list[Source], extra: dict | None = None) -> None:
        if value in (None, "", []):
            return
        grounded = self._ground(quote, src_index, sources, field_name, result)
        if not grounded:
            return
        src, mode = grounded
        note = "quote matched approximately" if mode == "approximate" else ""
        result.claims.append(
            Claim(field=field_name, value=value, evidence=self._make_evidence(quote, src, note),
                  grounding_mode=mode, extra=extra or {})
        )

    # ------------------------------------------------------ LLM ingestion --
    def _ingest_llm(self, data: dict, sources: list[Source], result: ExtractionResult) -> None:
        for key in ("description", "sector", "country", "hq_city", "is_tech_platform"):
            item = data.get(key)
            if isinstance(item, dict):
                self._add(result, key, item.get("value"), item.get("quote", ""), item.get("source"), sources)

        for item in data.get("funding_statements") or []:
            if isinstance(item, dict):
                self._add(result, "funding_statement", item.get("text") or item.get("quote"),
                          item.get("quote", ""), item.get("source"), sources)

        for item in data.get("founders") or []:
            if isinstance(item, dict) and item.get("name"):
                self._add(result, "founder", str(item["name"]).strip(), item.get("quote", ""),
                          item.get("source"), sources, extra={"title": (item.get("title") or "").strip()})

        for item in data.get("emails") or []:
            if isinstance(item, dict) and item.get("address"):
                addr = str(item["address"]).strip().lower()
                self._add(result, "email", addr, item.get("quote", ""), item.get("source"), sources,
                          extra={"owner": (item.get("owner") or "").strip()})

        for item in data.get("us_signals") or []:
            if isinstance(item, dict) and item.get("kind"):
                self._add(result, "us_signal", str(item["kind"]).strip().lower(),
                          item.get("quote", ""), item.get("source"), sources)

    # ----------------------------------------------------- rule ingestion --
    # Name patterns are deliberately CASE-SENSITIVE: under re.IGNORECASE the
    # class [A-Z] matches lowercase too, which turns "CEO, said the funding will"
    # into a person called "said the funding will".  Only the job titles are
    # matched case-insensitively, via scoped inline flags.
    _TITLE = (
        r"(?i:co[-\s]?founder(?:\s*(?:&|and)\s*(?:CEO|CTO|COO|MD))?|"
        r"founder(?:\s*(?:&|and)\s*(?:CEO|CTO|COO|MD))?|"
        r"chief\s+executive\s+officer|CEO(?:\s*(?:&|and)\s*(?:co[-\s]?founder|founder))?|"
        r"managing\s+director)"
    )
    # Spaces and tabs only - never newlines. With \s+ here, a heading on one line
    # fuses with the name on the next ("Leadership\nMaria Silva Santos" parses as
    # a four-word person), which is exactly how team pages are laid out.
    # Surnames carry lowercase particles - "Jeroen van Dijk", "Ana de Souza" -
    # and a pattern insisting on initial capitals loses the person entirely.
    _PARTICLE = r"(?:van|von|de|del|della|der|den|di|da|dos|das|du|la|le|el|al|bin|binti|ibn|ter|op)"
    _WORD = r"[A-Z][a-zA-Z'\u2019\-]+"
    _NAME = rf"{_WORD}(?:[ \t]+(?:{_PARTICLE}[ \t]+)?(?:{_WORD}|[A-Z]\.)){{1,3}}"

    _FOUNDER_RE = re.compile(
        rf"(?P<name>{_NAME})\s*"
        rf"(?:,|\s+[-\u2013\u2014]\s+|\s+is\s+(?:the\s+|our\s+)?|"
        rf"\s+serves\s+as\s+(?:the\s+)?|\s*\|\s*)\s*"
        rf"(?P<title>{_TITLE})\b"
    )
    _FOUNDER_RE_REV = re.compile(
        rf"(?P<title>{_TITLE})\s*(?:,|:|\s+is|\s+of\s+\w+)?\s+(?P<name>{_NAME})\b"
    )
    # Real team pages overwhelmingly stack the name and the role on separate
    # lines rather than joining them with a comma.
    _FOUNDER_RE_STACKED = re.compile(
        rf"^[ \t]*(?P<name>{_NAME})[ \t]*$\n[ \t]*(?P<title>{_TITLE})\b",
        re.MULTILINE,
    )
    # "founded in 2021 by Lena Brandt" - the role is stated a little further on.
    _FOUNDER_RE_FOUNDED_BY = re.compile(
        rf"(?i:founded)(?:\s+in\s+\d{{4}})?\s+by\s+(?P<name>{_NAME})"
    )
    # Imprint pages. These markets are weighted heavily in discovery precisely
    # because the law requires a named, contactable representative - and the
    # agent was reading the address off the imprint while failing to read the
    # name printed directly above it.
    _IMPRINT_LEAD = (
        r"(?i:vertreten\s+durch|gesch(?:ä|ae)ftsf(?:ü|ue)hrer(?:in)?|inhaber(?:in)?|"
        r"vorstand|directeur\s+de\s+la\s+publication|repr(?:é|e)sentant\s+l(?:é|e)gal|"
        r"g(?:é|e)rant(?:e)?|pr(?:é|e)sident(?:e)?|bestuurder|directeur|"
        r"amministratore\s+(?:unico|delegato)|legale\s+rappresentante|representante\s+legal|"
        r"administrador(?:a)?|prezes\s+zarz(?:ą|a)du|verantwortlich(?:er)?)"
    )
    _IMPRINT_ROLE = re.compile(
        "(?P<lead>" + _IMPRINT_LEAD + ")"
        + r"[ \t]*[:：–-]?[ \t]*"
        + r"(?:(?i:dr|prof|mr|mrs|ms|ing|mag)\.?[ \t]+)?"
        + rf"(?P<name>{_NAME})"
    )

    # What the imprint actually called them, in English. An imprint states an
    # office, not "founder", and recording the office keeps the claim honest.
    _IMPRINT_TITLES = {
        # Offices that are the statutory chief executive of the company. These
        # satisfy TVB's "CEO or Co-founder" in substance; the ones below them
        # (publication director, legal representative, board member) do not, and
        # are deliberately left as titles the founder gate will not accept.
        "vertreten durch": "Authorised representative",
        "geschäftsführer": "Managing Director", "geschäftsführerin": "Managing Director",
        "geschaeftsfuehrer": "Managing Director", "geschaeftsfuehrerin": "Managing Director",
        "inhaber": "Owner", "inhaberin": "Owner", "vorstand": "Board member",
        "verantwortlich": "Responsible person", "verantwortlicher": "Responsible person",
        "directeur de la publication": "Publication director",
        "représentant légal": "Legal representative", "representant legal": "Legal representative",
        "gérant": "Managing Director", "gerant": "Managing Director",
        "gérante": "Managing Director", "gerante": "Managing Director",
        "président": "President", "presidente": "President", "présidente": "President",
        "president": "President", "bestuurder": "Managing Director", "directeur": "Director",
        "amministratore unico": "Managing Director",
        "amministratore delegato": "Managing Director",
        "legale rappresentante": "Legal representative",
        "representante legal": "Legal representative",
        "administrador": "Managing Director", "administradora": "Managing Director",
        "prezes zarządu": "Managing Director", "prezes zarzadu": "Managing Director",
    }

    # Tokens that betray a sentence fragment masquerading as a name.
    _NAME_STOPWORDS = frozenset({
        "said", "says", "the", "a", "an", "and", "or", "will", "was", "were", "is", "are",
        "has", "have", "had", "this", "that", "our", "their", "its", "we", "they", "he", "she",
        "funding", "round", "company", "startup", "platform", "million", "today", "also",
        "new", "more", "about", "with", "from", "for", "after", "before", "who", "which",
        # section headings that sit directly above a name on a team page
        "leadership", "team", "management", "board", "people", "founders", "staff",
        "meet", "executives", "directors", "advisors", "contact",
    })

    # A role is not a person. Run 10 offered TVB a "founder" called
    # "Ex-Pipedrive Founder" - marketing copy read as a name, the exact class of
    # error the whole evidence pipeline exists to prevent. Any name carrying one
    # of these words is a description of a job, not somebody TVB can write to.
    _ROLE_WORDS = frozenset({
        "founder", "founders", "cofounder", "co-founder", "founding",
        "ceo", "cto", "coo", "cfo", "cmo", "cpo", "cio", "cro", "chief",
        "president", "vice", "vp", "director", "directors", "head", "officer",
        "partner", "partners", "investor", "investors", "chairman", "chairwoman",
        "chair", "owner", "executive", "manager", "lead", "principal",
        "ex", "former", "serial", "angel", "mentor", "advisor", "adviser",
        "alum", "alumni", "entrepreneur", "entrepreneurs", "expert", "veteran",
        "employee", "employees", "member", "members", "guest", "speaker",
    })

    # Field labels off a contact or imprint page. Run 14 offered TVB a founder
    # called "Personal E-Mail" - the caption above the address, read as the
    # person it belonged to. These sit directly beside the very fields the
    # extractor is reading, so they are the likeliest words to be mistaken for
    # a name anywhere in this pipeline.
    _LABEL_WORDS = frozenset({
        "personal", "email", "e", "mail", "e-mail", "emails", "phone", "telephone",
        "telefon", "tel", "fax", "mobile", "address", "adresse", "anschrift",
        "contact", "kontakt", "website", "web", "site", "registered", "register",
        "registration", "registergericht", "court", "tax", "vat", "ust", "steuernummer",
        "handelsregister", "company", "firm", "office", "headquarters", "hq",
        "postal", "post", "street", "strasse", "city", "country", "zip", "postcode",
        "details", "information", "info", "imprint", "impressum", "colofon",
        "responsible", "verantwortlich", "disclaimer", "privacy", "cookies",
        "general", "enquiries", "support", "sales", "press", "media", "careers",
        "number", "name", "title", "role", "position", "department",
    })

    @classmethod
    def _is_a_form_label(cls, name: str) -> bool:
        """"Personal E-Mail", "Registered Office", "Contact Details" - captions."""
        tokens = [t for t in re.split(r"[^A-Za-z]+", name or "") if t]
        if not tokens:
            return True
        return all(t.lower() in cls._LABEL_WORDS for t in tokens)

    @classmethod
    def _carries_a_role_word(cls, name: str) -> bool:
        """"Ex-Pipedrive Founder" and "Serial Entrepreneur" are not names."""
        for token in re.split(r"[^A-Za-z]+", name or ""):
            if token and token.lower() in cls._ROLE_WORDS:
                return True
        return False

    @classmethod
    def _plausible_person_name(cls, name: str) -> bool:
        toks = name.split()
        if not (2 <= len(toks) <= 4):
            return False
        if any(t.lower().strip(".") in cls._NAME_STOPWORDS for t in toks):
            return False
        if cls._carries_a_role_word(name):
            return False
        if cls._is_a_form_label(name):
            return False
        # Every token must start uppercase (initials like "J." are fine), except
        # the lowercase particles real surnames carry: "Jeroen van Dijk".
        particles = {"van", "von", "de", "del", "della", "der", "den", "di", "da",
                     "dos", "das", "du", "la", "le", "el", "al", "bin", "binti",
                     "ibn", "ter", "op"}
        if toks[0].lower() in particles or toks[-1].lower() in particles:
            return False    # a name cannot begin or end with a particle
        return all(re.match(r"^[A-Z]", t) or t.lower() in particles for t in toks)

    _US_PATTERNS: list[tuple[str, re.Pattern]] = [
        ("us_office", re.compile(
            # "New York office"
            r"\b(?:our|the)?\s*(?:US|U\.S\.|United States|American|New York|San Francisco|Boston|"
            r"Chicago|Austin|Seattle|Los Angeles|Miami|Atlanta|Denver|Delaware)\s+"
            r"(?:office|offices|headquarters|HQ|hub|branch|presence|team)\b"
            r"|"
            # ...and the commoner phrasing: "offices in London and New York",
            # "presence spans London and San Francisco", "locations include NYC".
            r"\b(?:office|offices|headquarters|hq|hubs?|locations?|presence|teams?)\s+"
            r"(?:in|across|spanning|spans|span|include|includes|including|from|between)\s+"
            r"[^\n.]{0,80}?"
            r"\b(?:New York|NYC|San Francisco|Boston|Chicago|Austin|Seattle|Los Angeles|"
            r"Miami|Atlanta|Denver|Palo Alto|Silicon Valley|United States|USA|U\.S\.)\b",
            re.I)),
        ("us_subsidiary", re.compile(r"\b(?:US|U\.S\.|United States|American)\s+(?:subsidiary|entity|arm|affiliate)\b|\bInc\.?\s*\(\s*(?:USA|United States)\s*\)", re.I)),
        ("us_incorporation", re.compile(r"\b(?:incorporated|registered|domiciled)\s+in\s+(?:Delaware|the United States|the US|USA)\b|\bDelaware\s+(?:C-?Corp|corporation|LLC)\b", re.I)),
        ("us_job_posting", re.compile(r"\b(?:remote\s*[-–(]\s*)?(?:US|USA|United States)\b[^\n]{0,40}\b(?:based|only|hiring|position|role|opening)\b|\b(?:New York|San Francisco|Austin|Boston|Seattle|Chicago),?\s*(?:NY|CA|TX|MA|WA|IL|US|USA)\b", re.I)),
        ("us_address_mention", re.compile(r"\b\d{1,5}\s+[A-Z][\w .]{2,30}(?:Street|St\.|Avenue|Ave\.|Road|Rd\.|Boulevard|Blvd\.|Suite|Ste\.)[^\n]{0,40}\b(?:NY|CA|TX|MA|WA|IL|FL|GA|CO|NJ|VA)\b\s*\d{5}", re.I)),
        ("us_phone", re.compile(r"(?:\+1[\s.\-]?)\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")),
    ]

    _COUNTRY_RE = re.compile(
        r"\b(?:headquartered|based|located|head\s*office)\s+in\s+(?:[A-Z][\w.\-]+(?:\s+[A-Z][\w.\-]+){0,2},\s*)?"
        r"(?P<country>[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,2})",
    )

    def _sentence_for(self, text: str, match_start: int, match_end: int) -> str:
        lo = max(0, text.rfind(".", 0, match_start) + 1)
        hi = text.find(".", match_end)
        hi = len(text) if hi == -1 else hi + 1
        return re.sub(r"\s+", " ", text[lo:hi]).strip()[:400]

    # Sector vocabulary, checked against page text when no model is available.
    _SECTOR_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
        ("Healthtech / Digital health", ("digital health", "healthcare", "health tech", "patient",
                                         "clinic", "telemedicine", "care coordination", "medical",
                                         "ehr", "hospital")),
        ("Edtech", ("edtech", "learning platform", "students", "curriculum", "upskilling",
                    "workforce development", "courses", "training platform")),
        ("Cybersecurity", ("cybersecurity", "threat detection", "vulnerability", "security posture",
                           "penetration testing", "soc ", "zero trust", "endpoint security")),
        ("Fintech / Payments", ("fintech", "payments", "wallet", "lending", "credit", "banking",
                                "embedded finance", "payouts", "remittance", "settlement")),
        ("Insurtech", ("insurtech", "insurance", "underwriting", "claims processing")),
        ("Digital twin / Industrial", ("digital twin", "simulation", "industrial iot", "factory",
                                       "manufacturing", "predictive maintenance")),
        ("Traveltech", ("travel", "booking platform", "itinerary", "airline", "hotel", "tourism")),
        ("Logistics / Supply chain", ("logistics", "supply chain", "freight", "shipment",
                                      "warehouse", "last mile", "fleet")),
        ("Agritech", ("agritech", "agriculture", "farmers", "crop", "farm management")),
        ("HR tech", ("recruitment", "hiring platform", "hr platform", "payroll", "talent platform",
                     "applicant tracking")),
        ("Proptech / Construction", ("proptech", "real estate", "construction", "property management")),
        ("Climate / Energy", ("climate", "carbon", "renewable", "energy management", "sustainability",
                              "emissions")),
        ("Legaltech", ("legaltech", "contract management", "compliance automation", "legal teams")),
        ("Retail / Commerce", ("ecommerce", "e-commerce", "retail", "merchants", "storefront",
                               "point of sale", "commerce platform")),
        ("AI / Machine learning", ("artificial intelligence", "machine learning", "ai platform",
                                   "ai agents", "llm", "computer vision", "nlp")),
        ("B2B SaaS", ("b2b saas", "saas platform", "enterprise software", "business software",
                      "workflow automation", "api platform")),
    ]

    _DESC_BAD_STARTS = (
        "cookie", "we use cookies", "privacy", "terms", "copyright", "all rights reserved",
        "skip to", "menu", "sign in", "log in", "subscribe", "newsletter", "loading",
    )

    def _rule_description(self, company_name: str, src: Source) -> tuple[str, str] | None:
        """A clean one-liner about the company, taken verbatim from the page."""
        first_token = (company_name.split() or [company_name])[0].lower()
        for block in (src.text or "")[:6000].split("\n"):
            line = re.sub(r"\s+", " ", block).strip()
            if not (45 <= len(line) <= 400):
                continue
            low = line.lower()
            if any(low.startswith(b) for b in self._DESC_BAD_STARTS):
                continue
            if low.count("|") > 2 or low.count("•") > 2:
                continue
            mentions = first_token in low or " we " in f" {low} " or low.startswith("we ")
            descriptive = any(t in low for t in (
                "platform", "software", "app", "solution", "product", "technology", "builds",
                "provides", "helps", "enables", "offers", "is a", "we are", "powers", "delivers"))
            if mentions and descriptive:
                sentence = re.split(r"(?<=[.!?])\s+", line)[0]
                if len(sentence) < 40:
                    sentence = line[:300]
                return sentence[:400], line[:400]
        return None

    def _rule_sector(self, sources: list[Source]) -> tuple[str, str, Source] | None:
        blob_parts = [(s, (s.text or "")[:8000].lower()) for s in sources[:4]]
        best: tuple[int, str, str, Source] | None = None
        for src, low in blob_parts:
            for label, keys in self._SECTOR_KEYWORDS:
                hits = [k for k in keys if k in low]
                if not hits:
                    continue
                if best is None or len(hits) > best[0]:
                    idx = low.find(hits[0])
                    quote = self._sentence_for(src.text, idx, idx + len(hits[0]))
                    best = (len(hits), label, quote, src)
        if best and best[2]:
            return best[1], best[2], best[3]
        return None

    def _ingest_rules(self, company_name: str, sources: list[Source], result: ExtractionResult) -> None:
        have_founder = {c.value.lower() for c in result.by_field("founder")}
        have_email = {c.value for c in result.by_field("email")}
        have_signal = {(c.value, c.evidence.url) for c in result.by_field("us_signal")}

        for src in sources:
            text = src.text

            # --- founders ---
            for rx in (self._FOUNDER_RE, self._FOUNDER_RE_STACKED,
                       self._FOUNDER_RE_REV, self._FOUNDER_RE_FOUNDED_BY,
                       self._IMPRINT_ROLE):
                for m in rx.finditer(text[:30_000]):
                    name = re.sub(r"\s+", " ", m.group("name")).strip()
                    raw_title = m.groupdict().get("title")
                    lead = m.groupdict().get("lead")
                    if raw_title:
                        title = re.sub(r"\s+", " ", raw_title).strip()
                    elif lead:
                        # An imprint states an office, not "founder". Recording
                        # what it actually said keeps the claim honest.
                        key = re.sub(r"\s+", " ", lead).strip().lower()
                        title = self._IMPRINT_TITLES.get(key, "Company representative")
                        if key == "vertreten durch":
                            # German imprints name the office right after the
                            # person: "Vertreten durch: Dr. Lena Brandt
                            # (Geschäftsführerin)".
                            window = text[m.end(): m.end() + 90].lower()
                            for office, mapped in self._IMPRINT_TITLES.items():
                                if office != key and office in window:
                                    title = mapped
                                    break
                    else:
                        # "founded by X" names the person; the role follows nearby.
                        window = text[m.start(): m.end() + 220]
                        tm = re.search(
                            r"(?i:co[-\s]?founder|founder|chief\s+executive\s+officer|CEO|"
                            r"managing\s+director)", window)
                        title = tm.group(0) if tm else "Founder"
                    if name.lower() in have_founder or not self._plausible_person_name(name):
                        continue
                    if name.lower().startswith(company_name.lower()[:10]):
                        continue
                    quote = self._sentence_for(text, m.start(), m.end())
                    if verify_quote(quote, text).ok:
                        have_founder.add(name.lower())
                        result.claims.append(
                            Claim(field="founder", value=name,
                                  evidence=self._make_evidence(quote, src, "rule-based extraction"),
                                  extra={"title": title})
                        )

            # --- emails (rule-based always runs: models miss obfuscated forms) ---
            for addr in find_emails_in_text(text[:40_000]):
                if addr in have_email:
                    continue
                pos = text.lower().find(addr.split("@")[0].lower())
                quote = self._sentence_for(text, max(pos, 0), max(pos, 0) + len(addr)) if pos >= 0 else addr
                if len(quote) < 12:
                    quote = f"Contact address published on this page: {addr}"
                have_email.add(addr)
                result.claims.append(
                    Claim(field="email", value=addr,
                          evidence=self._make_evidence(quote if verify_quote(quote, text).ok else addr, src,
                                                       "rule-based extraction"),
                          extra={"owner": "", "role": is_role_account(addr)})
                )

            # --- US presence signals ---
            for kind, rx in self._US_PATTERNS:
                m = rx.search(text[:40_000])
                if not m or (kind, src.url) in have_signal:
                    continue
                quote = self._sentence_for(text, m.start(), m.end())
                have_signal.add((kind, src.url))
                result.claims.append(
                    Claim(field="us_signal", value=kind,
                          evidence=self._make_evidence(quote, src, "rule-based extraction"))
                )

            # --- description (only if the model did not already supply one) ---
            if not result.by_field("description"):
                got = self._rule_description(company_name, src)
                if got:
                    value, quote = got
                    if verify_quote(quote, src.text).ok:
                        result.claims.append(
                            Claim(field="description", value=value,
                                  evidence=self._make_evidence(quote, src, "rule-based extraction"))
                        )

            # --- country ---
            if not result.by_field("country"):
                m = self._COUNTRY_RE.search(text[:20_000])
                if m:
                    quote = self._sentence_for(text, m.start(), m.end())
                    result.claims.append(
                        Claim(field="country", value=m.group("country").strip(),
                              evidence=self._make_evidence(quote, src, "rule-based extraction"))
                    )
