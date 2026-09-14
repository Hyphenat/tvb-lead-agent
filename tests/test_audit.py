"""The audit re-checks a finished lead list from scratch.

Its job is to catch the case where the pipeline believed something that is not
true, so the tests here deliberately feed it a lead whose evidence does not hold.
"""

import json

import httpx
import pytest
import respx

from tvb_agent.audit import audit_records, load_records

PAGE = ("<html><body><p>Acme Technologies has raised $2.4 million in a seed round "
        "led by Blume Ventures. Jane Doe is the Co-founder and CEO.</p></body></html>")


def lead(email="jane.doe@acme.io", quote="has raised $2.4 million in a seed round led by Blume Ventures"):
    return {
        "company_name": "Acme Technologies",
        "verified_email": email,
        "evidence": [{"url": "https://acme.io/about", "quote": quote, "authority": "company_owned"}],
    }


@pytest.mark.asyncio
@respx.mock
async def test_a_sound_lead_audits_clean(settings):
    respx.get("https://acme.io/about").mock(
        return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"}))
    report = await audit_records([lead()], settings)
    assert report.clean_leads == 1
    assert report.leads[0].evidence_verified == 1
    assert report.leads[0].email_mx_ok


@pytest.mark.asyncio
@respx.mock
async def test_evidence_that_no_longer_holds_is_flagged(settings):
    """The quote is not on the page: exactly what the audit exists to catch."""
    respx.get("https://acme.io/about").mock(
        return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"}))
    bad = lead(quote="Acme raised $40 million in a Series B led by Sequoia Capital")
    report = await audit_records([bad], settings)
    assert report.clean_leads == 0
    assert "None of the quoted evidence could be found at its source." in report.leads[0].issues


@pytest.mark.asyncio
@respx.mock
async def test_unreachable_source_is_reported_not_assumed_good(settings):
    respx.get("https://acme.io/about").mock(return_value=httpx.Response(404, text=""))
    report = await audit_records([lead()], settings)
    assert report.leads[0].evidence[0].reachable is False
    assert report.clean_leads == 0


@pytest.mark.asyncio
@respx.mock
async def test_role_and_invalid_emails_are_flagged(settings):
    respx.get("https://acme.io/about").mock(
        return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"}))

    role = await audit_records([lead(email="info@acme.io")], settings)
    assert any("role account" in i for i in role.leads[0].issues)

    broken = await audit_records([lead(email="not-an-email")], settings)
    assert any("not syntactically valid" in i for i in broken.leads[0].issues)

    nomx = await audit_records([lead(email="jane@nomx.io")], settings)
    assert any("no MX record" in i for i in nomx.leads[0].issues)


@pytest.mark.asyncio
@respx.mock
async def test_lead_without_evidence_is_flagged(settings):
    bare = {"company_name": "Ghost Co", "verified_email": "a@acme.io", "evidence": []}
    report = await audit_records([bare], settings)
    assert any("no evidence" in i.lower() for i in report.leads[0].issues)


def test_load_records_accepts_both_export_shapes(tmp_path):
    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps([lead()]))
    assert len(load_records(flat)) == 1

    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"leads": [lead(), lead()]}))
    assert len(load_records(wrapped)) == 2


@pytest.mark.asyncio
@respx.mock
async def test_report_serialises_for_review(settings):
    respx.get("https://acme.io/about").mock(
        return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"}))
    report = await audit_records([lead()], settings)
    payload = report.to_dict()
    assert payload["leads_total"] == 1 and payload["leads_clean"] == 1
    assert json.dumps(payload)
    assert "AUDIT" in report.summary()


# --------------------------------------------------------------------------- #
# Leads banked under older, weaker rules
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("founder,email,domain,reason", [
    # Run 1: a trade journalist recorded as the company's founder, with the
    # magazine that covered the company recorded as its website.
    ("Helen Siwak", "helen@ecoluxluv.com", "retail-insider.com", "publication"),
    # Run 10: marketing copy recorded as a person, an office mailbox as their address.
    ("Ex-Pipedrive Founder", "tallinn@lift99.co", "lift99.co", "job description"),
    ("Priya Raman", "info@zetacare.in", "zetacare.in", "shared mailbox"),
])
def test_a_lead_banked_under_older_rules_is_not_grandfathered_in(founder, email, domain, reason):
    """A fix applies retroactively. Otherwise closing a hole still leaves the
    lead it let through sitting in the export."""
    from tvb_agent.models import (
        CompanyProfile,
        EmailRecord,
        EmailStatus,
        Evidence,
        Evidenced,
        Person,
        SourceAuthority,
    )
    from tvb_agent.validation.validators import lead_fails_current_rules

    ev = [Evidence(url=f"https://{domain}/team", quote=f"{founder} leads the company.",
                   authority=SourceAuthority.COMPANY_OWNED)]
    profile = CompanyProfile.new("Acme", domain)
    profile.founder = Evidenced[Person].of(Person(name=founder, title="Founder"), ev)
    profile.email = EmailRecord(address=email, status=EmailStatus.VERIFIED)

    assert lead_fails_current_rules(profile) is not None


def test_a_sound_lead_still_passes_on_re_read():
    from decimal import Decimal

    from tvb_agent.models import (
        AmountType,
        CompanyProfile,
        EmailRecord,
        EmailStatus,
        Evidence,
        Evidenced,
        MoneyAmount,
        Person,
        SourceAuthority,
    )
    from tvb_agent.validation.validators import lead_fails_current_rules

    ev = [Evidence(url="https://zetacare.in/team", quote="Priya Raman, Co-founder & CEO.",
                   authority=SourceAuthority.COMPANY_OWNED)]
    money_ev = [Evidence(url="https://zetacare.in/about",
                         quote="Zeta Care raised $2.4 million in a seed round.",
                         authority=SourceAuthority.COMPANY_OWNED)]
    profile = CompanyProfile.new("Zeta Care", "zetacare.in")
    profile.founder = Evidenced[Person].of(Person(name="Priya Raman", title="Co-founder & CEO"), ev)
    profile.email = EmailRecord(address="priya.raman@zetacare.in", status=EmailStatus.VERIFIED)
    profile.funding = Evidenced[MoneyAmount].of(
        MoneyAmount(raw="$2.4 million", currency="USD", amount_original=Decimal("2400000"),
                    amount_usd=Decimal("2400000"), amount_type=AmountType.FUNDING_RAISED,
                    fx_date="2026-09-01", fx_rate=1.0),
        money_ev)

    assert lead_fails_current_rules(profile) is None


def test_a_lead_whose_figure_is_outside_the_current_band_is_dropped_on_re_read():
    """The stored verdict is restored verbatim from JSON. A lead banked under a
    different MIN/MAX_AMOUNT_USD was re-exported as qualified with nothing
    re-evaluating it."""
    from decimal import Decimal

    from tvb_agent.models import (
        AmountType,
        CompanyProfile,
        EmailRecord,
        EmailStatus,
        Evidence,
        Evidenced,
        MoneyAmount,
        Person,
        SourceAuthority,
    )
    from tvb_agent.validation.validators import lead_fails_current_rules

    ev = [Evidence(url="https://acme.io/team", quote="Ana Reyes, CEO.",
                   authority=SourceAuthority.COMPANY_OWNED)]
    profile = CompanyProfile.new("Acme", "acme.io")
    profile.founder = Evidenced[Person].of(Person(name="Ana Reyes", title="CEO"), ev)
    profile.email = EmailRecord(address="ana.reyes@acme.io", status=EmailStatus.VERIFIED)
    profile.funding = Evidenced[MoneyAmount].of(
        MoneyAmount(raw="$52 million", currency="USD", amount_original=Decimal("52000000"),
                    amount_usd=Decimal("52000000"), amount_type=AmountType.FUNDING_RAISED,
                    fx_date="2026-09-01", fx_rate=1.0), ev)

    assert "outside the current" in (lead_fails_current_rules(profile) or "")
