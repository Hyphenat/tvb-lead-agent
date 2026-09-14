"""Core data model for the TVB lead agent.

Design rule enforced here: **no bare claim can reach the qualification engine.**
Every fact about a company is wrapped in ``Evidenced[T]``, which cannot be
constructed without a list of :class:`Evidence` objects carrying a source URL and
the verbatim quote that supports the claim.  A claim whose quote could not be
found in the fetched page is never created in the first place (see
``research.extractor``), so "hallucinated but confident" is structurally
impossible rather than merely discouraged.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
class SourceAuthority(str, Enum):
    """How much a source is allowed to prove.

    Ordering matters: several gates require evidence at or above a floor.
    """

    COMPANY_OWNED = "company_owned"          # the company's own domain
    OFFICIAL_REGISTRY = "official_registry"  # companies house, MCA, etc.
    TIER1_PRESS = "tier1_press"              # established funding/news outlets
    AGGREGATOR = "aggregator"                # startup databases / trackers
    SOCIAL_PROFILE = "social_profile"        # linkedin & co
    SEARCH_SNIPPET = "search_snippet"        # a SERP snippet, unfetched
    UNKNOWN = "unknown"


AUTHORITY_RANK: dict[SourceAuthority, int] = {
    SourceAuthority.COMPANY_OWNED: 5,
    SourceAuthority.OFFICIAL_REGISTRY: 5,
    SourceAuthority.TIER1_PRESS: 4,
    SourceAuthority.AGGREGATOR: 3,
    SourceAuthority.SOCIAL_PROFILE: 2,
    SourceAuthority.SEARCH_SNIPPET: 1,
    SourceAuthority.UNKNOWN: 0,
}


class Evidence(BaseModel):
    """A single verifiable support for one claim."""

    model_config = ConfigDict(frozen=True)

    url: str
    quote: str = Field(description="Verbatim text from the source supporting the claim.")
    authority: SourceAuthority = SourceAuthority.UNKNOWN
    title: str | None = None
    fetched_at: datetime = Field(default_factory=utcnow)
    note: str | None = None

    @property
    def rank(self) -> int:
        return AUTHORITY_RANK[self.authority]

    def short(self, n: int = 220) -> str:
        q = re.sub(r"\s+", " ", self.quote).strip()
        return q if len(q) <= n else q[: n - 1] + "…"


class ClaimStatus(str, Enum):
    UNKNOWN = "unknown"            # never established -> gates fail
    ASSERTED = "asserted"          # one source
    CORROBORATED = "corroborated"  # two or more independent sources
    REJECTED = "rejected"          # actively contradicted


class Evidenced(BaseModel, Generic[T]):
    """A value that carries its provenance, or is explicitly unknown."""

    value: T | None = None
    status: ClaimStatus = ClaimStatus.UNKNOWN
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = 0.0

    @classmethod
    def unknown(cls) -> Evidenced[T]:
        return cls()

    @classmethod
    def of(cls, value: T, evidence: list[Evidence], confidence: float | None = None) -> Evidenced[T]:
        if not evidence:
            raise ValueError("Evidenced.of() requires at least one Evidence; use Evidenced.unknown().")
        hosts = {_host(e.url) for e in evidence}
        status = ClaimStatus.CORROBORATED if len(hosts) > 1 else ClaimStatus.ASSERTED
        if confidence is None:
            best = max(e.rank for e in evidence)
            confidence = min(0.99, 0.30 + 0.12 * best + (0.15 if len(hosts) > 1 else 0.0))
        return cls(value=value, status=status, evidence=list(evidence), confidence=confidence)

    @property
    def known(self) -> bool:
        return self.value is not None and self.status in (ClaimStatus.ASSERTED, ClaimStatus.CORROBORATED)

    @property
    def best_authority(self) -> SourceAuthority:
        if not self.evidence:
            return SourceAuthority.UNKNOWN
        return max(self.evidence, key=lambda e: e.rank).authority

    def meets(self, floor: SourceAuthority) -> bool:
        return self.known and AUTHORITY_RANK[self.best_authority] >= AUTHORITY_RANK[floor]


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    h = (m.group(1) if m else url or "").lower()
    return h[4:] if h.startswith("www.") else h


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #
class AmountType(str, Enum):
    """What a monetary figure actually measures.

    The task is explicit that valuation / TAM / ACV / grants / projections must
    not be confused with funding raised or revenue, so the parser classifies
    every figure and the funding gate accepts only the qualifying kinds.
    """

    FUNDING_RAISED = "funding_raised"
    REVENUE = "revenue"
    ARR = "arr"
    # --- never accepted as evidence for the $1M-$5M criterion ---
    VALUATION = "valuation"
    TAM = "tam"
    ACV = "acv"
    AUM = "aum"
    GRANT = "grant"
    FUND_SIZE = "fund_size"         # a VC/PE vehicle, not an operating company
    PROJECTION = "projection"
    DEBT = "debt"
    UNKNOWN = "unknown"


QUALIFYING_AMOUNT_TYPES: frozenset[AmountType] = frozenset(
    {AmountType.FUNDING_RAISED, AmountType.REVENUE, AmountType.ARR}
)


class MoneyAmount(BaseModel):
    model_config = ConfigDict(frozen=True)

    raw: str
    amount_original: Decimal
    currency: str
    amount_usd: Decimal
    fx_rate: Decimal
    fx_date: str
    amount_type: AmountType = AmountType.UNKNOWN
    is_cumulative: bool = False   # "has raised a total of ..." vs a single round
    context: str = ""
    start: int = 0                # offset in the source text, for sentence extraction
    end: int = 0

    @property
    def qualifies_as_criterion_input(self) -> bool:
        return self.amount_type in QUALIFYING_AMOUNT_TYPES

    def human(self) -> str:
        v = float(self.amount_usd)
        s = f"${v/1_000_000:.2f}M" if v >= 1_000_000 else f"${v/1_000:.0f}K"
        if self.currency != "USD":
            s += f" ({self.currency} {self.amount_original:,.0f})"
        return s


# --------------------------------------------------------------------------- #
# People & email
# --------------------------------------------------------------------------- #
class Person(BaseModel):
    name: str
    title: str | None = None
    linkedin: str | None = None

    @property
    def is_founder_or_ceo(self) -> bool:
        t = (self.title or "").lower()
        return any(k in t for k in ("ceo", "chief executive", "founder", "co-founder", "cofounder", "managing director"))


class EmailStatus(str, Enum):
    """The verification ladder.  Only VERIFIED reaches the qualified output."""

    NOT_FOUND = "not_found"
    ROLE_ONLY = "role_only"                # info@/hello@ - never a founder email
    FOUND_UNVERIFIED = "found_unverified"  # seen on a low-authority page
    SOURCE_VERIFIED = "source_verified"    # on an authoritative source, attributed
    VERIFIED = "verified"                  # source-verified + syntax + MX + deliverability
    INVALID = "invalid"


# How good an answer each rung is, so a second attempt can only ever improve a
# record rather than quietly replace a stronger finding with a weaker one.
EMAIL_STATUS_RANK: dict[EmailStatus, int] = {
    EmailStatus.INVALID: 0,
    EmailStatus.NOT_FOUND: 1,
    EmailStatus.ROLE_ONLY: 2,
    EmailStatus.FOUND_UNVERIFIED: 3,
    EmailStatus.SOURCE_VERIFIED: 4,
    EmailStatus.VERIFIED: 5,
}


class EmailRecord(BaseModel):
    address: str | None = None
    status: EmailStatus = EmailStatus.NOT_FOUND
    owner_name: str | None = None
    syntax_ok: bool = False
    mx_ok: bool = False
    mx_hosts: list[str] = Field(default_factory=list)
    domain_matches_company: bool = False
    is_role_account: bool = False
    is_disposable: bool = False
    is_catch_all: bool | None = None   # None = not probed
    deliverability: str | None = None  # deliverable / undeliverable / unknown / risky
    verifier: str | None = None        # which provider produced `deliverability`
    evidence: list[Evidence] = Field(default_factory=list)
    role_fallback: str | None = None   # generic company address, stored separately
    notes: list[str] = Field(default_factory=list)

    @property
    def is_verified(self) -> bool:
        return self.status is EmailStatus.VERIFIED

    @property
    def verification_strength(self) -> str:
        """How strong the verification actually is, in plain words.

        A reviewer deserves to know the difference between "a verification
        service confirmed this mailbox accepts mail" and "we found it published
        on the company's own site and the domain accepts mail at all".
        """
        if self.status is not EmailStatus.VERIFIED:
            return "not verified"
        if self.deliverability == "deliverable" and self.verifier not in (None, "none"):
            return f"confirmed deliverable by {self.verifier}"
        if self.deliverability and self.verifier not in (None, "none"):
            return f"{self.verifier} answered '{self.deliverability}' - not a delivery confirmation"
        if self.evidence and self.evidence[0].authority is SourceAuthority.AGGREGATOR:
            return "contact database + MX; no delivery check was performed"
        return "published on an authoritative source + MX; no delivery check was performed"


# --------------------------------------------------------------------------- #
# US presence
# --------------------------------------------------------------------------- #
class USSignalKind(str, Enum):
    HQ_IN_US = "hq_in_us"                    # hard disqualifier
    US_OFFICE = "us_office"                  # hard disqualifier
    US_SUBSIDIARY = "us_subsidiary"          # hard disqualifier
    US_INCORPORATION = "us_incorporation"    # hard disqualifier
    US_JOB_POSTING = "us_job_posting"        # hard disqualifier
    US_PHONE = "us_phone"                    # weak
    US_ADDRESS_MENTION = "us_address_mention"  # weak
    US_LOCALE_PAGE = "us_locale_page"        # weak
    US_PRESS_DATELINE = "us_press_dateline"  # weak


HARD_US_SIGNALS: frozenset[USSignalKind] = frozenset(
    {
        USSignalKind.HQ_IN_US,
        USSignalKind.US_OFFICE,
        USSignalKind.US_SUBSIDIARY,
        USSignalKind.US_INCORPORATION,
        USSignalKind.US_JOB_POSTING,
    }
)


class USSignal(BaseModel):
    kind: USSignalKind
    evidence: Evidence

    @property
    def is_hard(self) -> bool:
        return self.kind in HARD_US_SIGNALS


class USPresenceLevel(str, Enum):
    NONE = "none"
    MINIMAL = "minimal"
    SIGNIFICANT = "significant"
    UNKNOWN = "unknown"


class USPresenceAssessment(BaseModel):
    level: USPresenceLevel = USPresenceLevel.UNKNOWN
    hq_country: str | None = None
    signals: list[USSignal] = Field(default_factory=list)
    pages_checked: list[str] = Field(default_factory=list)
    rationale: str = ""

    @property
    def hard_signals(self) -> list[USSignal]:
        return [s for s in self.signals if s.is_hard]

    @property
    def weak_signal_count(self) -> int:
        """Distinct *kinds* of weak signal, not the number of times each was seen.

        One +1 phone number in a site-wide footer produced a `us_phone` signal on
        every page crawled, so three pages meant three signals - past the
        tolerance of one, and the company was disqualified for a single fact
        repeated by the template it was printed in.
        """
        return len({s.kind for s in self.signals if not s.is_hard})


# --------------------------------------------------------------------------- #
# Company
# --------------------------------------------------------------------------- #
def canonical_id(name: str, domain: str | None) -> str:
    key = (domain or "").lower().strip() or re.sub(r"[^a-z0-9]+", "", name.lower())
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


class CompanyProfile(BaseModel):
    """Everything known about one candidate company."""

    id: str
    name: str
    domain: str | None = None

    website: Evidenced[str] = Field(default_factory=Evidenced.unknown)
    description: Evidenced[str] = Field(default_factory=Evidenced.unknown)
    sector: Evidenced[str] = Field(default_factory=Evidenced.unknown)
    country: Evidenced[str] = Field(default_factory=Evidenced.unknown)
    hq_city: Evidenced[str] = Field(default_factory=Evidenced.unknown)

    funding: Evidenced[MoneyAmount] = Field(default_factory=Evidenced.unknown)
    tech_platform: Evidenced[bool] = Field(default_factory=Evidenced.unknown)
    us_presence: Evidenced[USPresenceAssessment] = Field(default_factory=Evidenced.unknown)
    founder: Evidenced[Person] = Field(default_factory=Evidenced.unknown)
    email: EmailRecord = Field(default_factory=EmailRecord)

    # Provenance of *discovery* only. Deliberately separate from evidence:
    # finding a company somewhere proves nothing about it.
    discovered_via: list[str] = Field(default_factory=list)
    discovery_cell: str | None = None

    first_seen_run: str | None = None
    last_seen_run: str | None = None
    pages_fetched: list[str] = Field(default_factory=list)

    @classmethod
    def new(cls, name: str, domain: str | None = None, **kw) -> CompanyProfile:
        return cls(id=canonical_id(name, domain), name=name.strip(), domain=domain, **kw)

    def all_evidence(self) -> list[Evidence]:
        out: list[Evidence] = []
        for f in (self.website, self.description, self.sector, self.country, self.hq_city,
                  self.funding, self.tech_platform, self.us_presence, self.founder):
            out.extend(f.evidence)
        out.extend(self.email.evidence)
        seen, uniq = set(), []
        for e in out:
            k = (e.url, e.quote[:80])
            if k not in seen:
                seen.add(k)
                uniq.append(e)
        return uniq


# --------------------------------------------------------------------------- #
# Qualification
# --------------------------------------------------------------------------- #
class Gate(str, Enum):
    FUNDING_OR_REVENUE = "funding_or_revenue_pass"
    TECHNOLOGY_PLATFORM = "technology_platform_pass"
    US_PRESENCE = "us_presence_pass"
    FOUNDER_IDENTIFIED = "founder_identified"
    EMAIL_VERIFIED = "email_verified"


class GateResult(BaseModel):
    gate: Gate
    passed: bool
    reason: str
    evidence: list[Evidence] = Field(default_factory=list)


class Qualification(BaseModel):
    """Deterministic verdict.

    ``qualified`` is derived from the gates alone in :meth:`build`; ``confidence``
    and ``fit_score`` are advisory and are computed *after* the verdict, so no
    score can promote a company that failed a hard requirement.
    """

    gates: list[GateResult]
    qualified: bool
    confidence: float = 0.0
    fit_score: float = 0.0
    fit_reasons: list[str] = Field(default_factory=list)
    near_boundary: bool = False

    @classmethod
    def build(cls, gates: list[GateResult]) -> Qualification:
        required = set(Gate)
        present = {g.gate for g in gates}
        missing = required - present
        if missing:
            raise ValueError(f"Qualification requires a result for every gate; missing {sorted(m.value for m in missing)}")
        return cls(gates=gates, qualified=all(g.passed for g in gates))

    def failed_gates(self) -> list[GateResult]:
        return [g for g in self.gates if not g.passed]

    def first_failure_reason(self) -> str:
        f = self.failed_gates()
        return f[0].reason if f else ""


class Lead(BaseModel):
    """A company plus its verdict - the unit the UI and exports work with."""

    company: CompanyProfile
    qualification: Qualification
    run_id: str
    created_at: datetime = Field(default_factory=utcnow)

    def export_row(self) -> dict:
        c = self.company
        us = c.us_presence.value
        return {
            "company_name": c.name,
            "description": (c.description.value or "")[:400],
            "industry_sector": c.sector.value or "",
            "funding_or_revenue": c.funding.value.human() if c.funding.value else "",
            "funding_type": c.funding.value.amount_type.value if c.funding.value else "",
            "ceo_or_cofounder": c.founder.value.name if c.founder.value else "",
            "founder_title": (c.founder.value.title or "") if c.founder.value else "",
            "verified_email": c.email.address if c.email.is_verified else "",
            "website": c.website.value or (f"https://{c.domain}" if c.domain else ""),
            "country": c.country.value or "",
            "us_presence": us.level.value if us else "",
            "qualified": self.qualification.qualified,
            "confidence": round(self.qualification.confidence, 3),
            "tvb_fit_score": round(self.qualification.fit_score, 3),
            "evidence_urls": " | ".join(sorted({e.url for e in c.all_evidence()})[:8]),
        }
