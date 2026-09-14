"""The qualification engine.

Five hard gates, evaluated deterministically from evidenced fields.  The verdict
is computed **before** any score exists, and :meth:`Qualification.build` derives
``qualified`` from the gate booleans alone, so there is no code path by which a
high confidence or a strong TVB-fit score can rescue a company that failed a
requirement.  Scores are advisory: they order the list, they never populate it.
"""

from __future__ import annotations

from ..config import Settings
from ..models import (
    CompanyProfile,
    EmailStatus,
    Gate,
    GateResult,
    Qualification,
    USPresenceLevel,
)
from .fit import tvb_fit_score

PASSING_US_LEVELS = (USPresenceLevel.NONE, USPresenceLevel.MINIMAL)


def _gate_funding(c: CompanyProfile, s: Settings) -> GateResult:
    f = c.funding
    if not f.known or f.value is None:
        return GateResult(gate=Gate.FUNDING_OR_REVENUE, passed=False,
                          reason="No verifiable funding or revenue figure was found.")
    amt = f.value
    if not amt.qualifies_as_criterion_input:
        return GateResult(gate=Gate.FUNDING_OR_REVENUE, passed=False, evidence=f.evidence,
                          reason=f"Figure found is a {amt.amount_type.value.replace('_', ' ')}, "
                                 f"which does not evidence funding raised or revenue.")
    usd = float(amt.amount_usd)
    if usd < s.min_amount_usd:
        return GateResult(gate=Gate.FUNDING_OR_REVENUE, passed=False, evidence=f.evidence,
                          reason=f"{amt.human()} is below the ${s.min_amount_usd/1e6:.0f}M minimum.")
    if usd > s.max_amount_usd:
        return GateResult(gate=Gate.FUNDING_OR_REVENUE, passed=False, evidence=f.evidence,
                          reason=f"{amt.human()} is above the ${s.max_amount_usd/1e6:.0f}M maximum.")
    label = {"funding_raised": "funding raised", "revenue": "revenue", "arr": "ARR"}.get(
        amt.amount_type.value, amt.amount_type.value)
    return GateResult(gate=Gate.FUNDING_OR_REVENUE, passed=True, evidence=f.evidence,
                      reason=f"{amt.human()} of {label} is within ${s.min_amount_usd/1e6:.0f}M-"
                             f"${s.max_amount_usd/1e6:.0f}M.")


def _gate_technology(c: CompanyProfile) -> GateResult:
    t = c.tech_platform
    if t.known and t.value:
        return GateResult(gate=Gate.TECHNOLOGY_PLATFORM, passed=True, evidence=t.evidence,
                          reason="Evidence of an operating technology product or platform was found.")
    return GateResult(gate=Gate.TECHNOLOGY_PLATFORM, passed=False, evidence=t.evidence,
                      reason="No evidence that the company operates a technology platform "
                             "(a sector label alone is not sufficient).")


def _gate_us_presence(c: CompanyProfile) -> GateResult:
    u = c.us_presence
    if not u.known or u.value is None:
        return GateResult(gate=Gate.US_PRESENCE, passed=False,
                          reason="Headquarters country could not be established, so a minimal "
                                 "US presence cannot be confirmed.")
    a = u.value
    if a.level in PASSING_US_LEVELS:
        return GateResult(gate=Gate.US_PRESENCE, passed=True, evidence=u.evidence, reason=a.rationale)
    return GateResult(gate=Gate.US_PRESENCE, passed=False, evidence=u.evidence,
                      reason=a.rationale or "Significant US presence detected.")


def _gate_founder(c: CompanyProfile) -> GateResult:
    f = c.founder
    if not f.known or f.value is None:
        return GateResult(gate=Gate.FOUNDER_IDENTIFIED, passed=False,
                          reason="No named CEO or co-founder could be identified from the evidence.")
    if not f.value.is_founder_or_ceo:
        return GateResult(gate=Gate.FOUNDER_IDENTIFIED, passed=False, evidence=f.evidence,
                          reason=f"{f.value.name} was identified but the role "
                                 f"({f.value.title or 'unknown'}) is not CEO or co-founder.")
    return GateResult(gate=Gate.FOUNDER_IDENTIFIED, passed=True, evidence=f.evidence,
                      reason=f"{f.value.name} identified as {f.value.title}.")


_EMAIL_REASONS = {
    EmailStatus.NOT_FOUND: "No email address was found.",
    EmailStatus.ROLE_ONLY: "Only a generic role address was found; a founder contact is required.",
    EmailStatus.FOUND_UNVERIFIED: "An address was found but could not be verified to the standard required.",
    EmailStatus.SOURCE_VERIFIED: "Address is source-verified but deliverability could not be confirmed.",
    EmailStatus.INVALID: "The address failed verification.",
}


def _gate_email(c: CompanyProfile) -> GateResult:
    e = c.email
    if e.is_verified:
        return GateResult(gate=Gate.EMAIL_VERIFIED, passed=True, evidence=e.evidence,
                          reason=f"{e.address} verified (source-attributed, MX valid"
                                 + (f", {e.verifier} says {e.deliverability}" if e.deliverability == "deliverable" else "")
                                 + ").")
    reason = _EMAIL_REASONS.get(e.status, "Email is not verified.")
    if e.notes:
        reason = f"{reason} {e.notes[0]}"
    return GateResult(gate=Gate.EMAIL_VERIFIED, passed=False, evidence=e.evidence, reason=reason)


def _confidence(c: CompanyProfile, gates: list[GateResult]) -> float:
    """Advisory only - computed after the verdict and unable to influence it."""
    parts = [
        c.funding.confidence,
        c.tech_platform.confidence,
        c.us_presence.confidence,
        c.founder.confidence,
        0.9 if c.email.is_verified else (0.5 if c.email.address else 0.0),
    ]
    base = sum(parts) / len(parts)
    corroboration = sum(1 for f in (c.funding, c.tech_platform, c.us_presence, c.founder)
                        if f.status.value == "corroborated")
    penalty = 0.08 * len([g for g in gates if not g.passed])
    return max(0.0, min(0.99, base + 0.04 * corroboration - penalty))


def _near_boundary(c: CompanyProfile, s: Settings) -> bool:
    """Flag amounts close to the band edge, where FX drift could change the call."""
    if not c.funding.value:
        return False
    usd = float(c.funding.value.amount_usd)
    tol = s.boundary_tolerance
    return (abs(usd - s.min_amount_usd) / s.min_amount_usd <= tol
            or abs(usd - s.max_amount_usd) / s.max_amount_usd <= tol)


def qualify(company: CompanyProfile, settings: Settings) -> Qualification:
    gates = [
        _gate_funding(company, settings),
        _gate_technology(company),
        _gate_us_presence(company),
        _gate_founder(company),
        _gate_email(company),
    ]
    # Verdict first, from the gates alone.
    q = Qualification.build(gates)
    # Scores afterwards. They order the list; they cannot change membership.
    q.confidence = _confidence(company, gates)
    q.fit_score, q.fit_reasons = tvb_fit_score(company)
    q.near_boundary = _near_boundary(company, settings)
    return q
