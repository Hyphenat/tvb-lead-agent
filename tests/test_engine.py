"""The qualification engine, including the guarantee that scores cannot override gates."""

from decimal import Decimal

import pytest

from tvb_agent.models import (
    AmountType,
    CompanyProfile,
    EmailRecord,
    EmailStatus,
    Evidence,
    Evidenced,
    Gate,
    MoneyAmount,
    Person,
    Qualification,
    SourceAuthority,
    USPresenceAssessment,
    USPresenceLevel,
)
from tvb_agent.qualify.engine import qualify


def ev(url="https://acme.io/about"):
    return Evidence(url=url, quote="x" * 40, authority=SourceAuthority.COMPANY_OWNED)


def money(usd=2_500_000, atype=AmountType.FUNDING_RAISED):
    return MoneyAmount(raw="$2.5M", amount_original=Decimal(usd), currency="USD",
                       amount_usd=Decimal(usd), fx_rate=Decimal(1), fx_date="2026-09-01",
                       amount_type=atype)


def company(*, usd=2_500_000, atype=AmountType.FUNDING_RAISED, tech=True,
            us_level=USPresenceLevel.NONE, title="Co-founder & CEO",
            email_status=EmailStatus.VERIFIED, country="India") -> CompanyProfile:
    c = CompanyProfile.new("Acme", "acme.io")
    c.funding = Evidenced[MoneyAmount].of(money(usd, atype), [ev()])
    if tech:
        c.tech_platform = Evidenced[bool].of(True, [ev()])
    c.country = Evidenced[str].of(country, [ev()])
    c.us_presence = Evidenced[USPresenceAssessment].of(
        USPresenceAssessment(level=us_level, hq_country=country, rationale="test"), [ev()])
    c.founder = Evidenced[Person].of(Person(name="Jane Doe", title=title), [ev()])
    c.email = EmailRecord(address="jane@acme.io", status=email_status, evidence=[ev()])
    return c


def test_all_gates_passing_qualifies(settings):
    assert qualify(company(), settings).qualified


@pytest.mark.parametrize("kwargs,failing_gate", [
    ({"usd": 400_000}, Gate.FUNDING_OR_REVENUE),
    ({"usd": 9_000_000}, Gate.FUNDING_OR_REVENUE),
    ({"atype": AmountType.VALUATION}, Gate.FUNDING_OR_REVENUE),
    ({"tech": False}, Gate.TECHNOLOGY_PLATFORM),
    ({"us_level": USPresenceLevel.SIGNIFICANT}, Gate.US_PRESENCE),
    ({"title": "CTO"}, Gate.FOUNDER_IDENTIFIED),
    ({"email_status": EmailStatus.SOURCE_VERIFIED}, Gate.EMAIL_VERIFIED),
    ({"email_status": EmailStatus.NOT_FOUND}, Gate.EMAIL_VERIFIED),
    ({"email_status": EmailStatus.ROLE_ONLY}, Gate.EMAIL_VERIFIED),
])
def test_each_failure_mode_blocks_qualification(settings, kwargs, failing_gate):
    q = qualify(company(**kwargs), settings)
    assert not q.qualified
    failed = {g.gate for g in q.gates if not g.passed}
    assert failing_gate in failed
    assert all(g.reason for g in q.gates), "every gate must explain itself"


def test_every_gate_is_always_evaluated(settings):
    q = qualify(company(), settings)
    assert {g.gate for g in q.gates} == set(Gate)


def test_qualification_requires_a_result_for_every_gate():
    with pytest.raises(ValueError):
        Qualification.build([])


def test_confidence_can_never_rescue_a_failed_gate(settings):
    """The structural guarantee: the verdict is computed from gates alone."""
    for kwargs in ({"usd": 400_000}, {"tech": False}, {"title": "CTO"},
                   {"email_status": EmailStatus.SOURCE_VERIFIED},
                   {"us_level": USPresenceLevel.SIGNIFICANT}):
        q = qualify(company(**kwargs), settings)
        assert not q.qualified
        # Even forcing the scores to their maximum must not change membership.
        q.confidence = 1.0
        q.fit_score = 1.0
        assert not Qualification.build(q.gates).qualified


def test_band_edges_are_inclusive(settings):
    assert qualify(company(usd=1_000_000), settings).qualified
    assert qualify(company(usd=5_000_000), settings).qualified
    assert not qualify(company(usd=999_999), settings).qualified
    assert not qualify(company(usd=5_000_001), settings).qualified


def test_near_boundary_flagging(settings):
    assert qualify(company(usd=1_050_000), settings).near_boundary
    assert qualify(company(usd=4_800_000), settings).near_boundary
    assert not qualify(company(usd=3_000_000), settings).near_boundary


def test_unknown_fields_fail_rather_than_default_to_pass(settings):
    c = CompanyProfile.new("Blank", "blank.io")   # every field unknown
    q = qualify(c, settings)
    assert not q.qualified
    assert all(not g.passed for g in q.gates), "an empty profile must fail every gate"


def test_fit_score_is_advisory_and_bounded(settings):
    q = qualify(company(), settings)
    assert 0.0 <= q.fit_score <= 1.0
    assert 0.0 <= q.confidence <= 1.0


def test_export_row_only_publishes_verified_emails(settings):
    from tvb_agent.models import Lead

    c = company(email_status=EmailStatus.SOURCE_VERIFIED)
    lead = Lead(company=c, qualification=qualify(c, settings), run_id="r1")
    assert lead.export_row()["verified_email"] == ""

    c2 = company()
    lead2 = Lead(company=c2, qualification=qualify(c2, settings), run_id="r1")
    assert lead2.export_row()["verified_email"] == "jane@acme.io"
