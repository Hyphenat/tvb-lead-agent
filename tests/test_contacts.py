"""Contact enrichment: asking a database, without taking its word for it.

Most companies never publish their founder's address, so a contact database is
the difference between finding the minority that do and finding the rest. It is
also the one source in this project that cannot show its working, so these tests
exist to pin down exactly how far it is trusted.
"""

import asyncio

import httpx
import pytest
import respx

from tvb_agent.config import Settings
from tvb_agent.models import EmailStatus, Evidence, Evidenced, Person, SourceAuthority
from tvb_agent.providers.contacts import ApolloProvider, ContactEnrichmentService
from tvb_agent.providers.email_verify import EmailVerificationService
from tvb_agent.validation.email import verify_enriched_address

MATCH_URL = "https://api.apollo.io/api/v1/people/match"


def blank() -> Settings:
    s = Settings()
    for name in vars(s):
        if name.endswith(("_api_key", "_cse_key", "_cse_cx")):
            setattr(s, name, None)
    return s


def settings_with_key() -> Settings:
    s = blank()
    s.apollo_api_key = "test-apollo"
    return s


def apollo_response(**person):
    return httpx.Response(200, json={"person": person})


def find(person="Lena Brandt", domain="heliosgrid.de", **person_fields):
    async def go():
        async with httpx.AsyncClient() as client:
            provider = ApolloProvider(client, settings_with_key())
            return await provider.find(person, domain, "Helios Grid")
    return asyncio.run(go())


def founder(name="Lena Brandt"):
    ev = [Evidence(url="https://heliosgrid.de/impressum", quote=f"{name} leads the company.",
                   authority=SourceAuthority.COMPANY_OWNED)]
    return Evidenced[Person].of(Person(name=name, title="Managing Director"), ev)


# --------------------------------------------------------------------------- #
# What the provider will and will not accept
# --------------------------------------------------------------------------- #
@respx.mock
def test_a_verified_record_for_the_right_person_is_accepted():
    respx.post(MATCH_URL).mock(return_value=apollo_response(
        first_name="Lena", last_name="Brandt", email="lena.brandt@heliosgrid.de",
        email_status="verified", title="Managing Director"))

    result = find()
    assert result is not None
    assert result.address == "lena.brandt@heliosgrid.de"
    assert result.provider_status == "verified"
    # The provenance says plainly that nobody read this on a page.
    assert "not published on a page we read" in result.provenance


@respx.mock
@pytest.mark.parametrize("status", ["unverified", "catch-all", "guessed", ""])
def test_anything_short_of_verified_is_a_guess_and_is_refused(status):
    """Apollo returns unverified and catch-all addresses too. Those are guesses,
    and this project does not ship guesses."""
    respx.post(MATCH_URL).mock(return_value=apollo_response(
        first_name="Lena", last_name="Brandt", email="lena.brandt@heliosgrid.de",
        email_status=status))

    assert find() is None


@respx.mock
def test_a_record_about_somebody_else_is_refused():
    """A wrong match is how another person's address ends up on a lead."""
    respx.post(MATCH_URL).mock(return_value=apollo_response(
        first_name="Markus", last_name="Weber", email="markus.weber@heliosgrid.de",
        email_status="verified"))

    assert find() is None


@respx.mock
def test_the_locked_placeholder_is_never_treated_as_an_address():
    """Apollo returns a literal email_not_unlocked@... string when the plan will
    not release the address. Shipping that would be worse than finding nothing."""
    from tvb_agent.providers.base import QuotaExhausted

    respx.post(MATCH_URL).mock(return_value=apollo_response(
        first_name="Lena", last_name="Brandt", email_status="verified",
        email="email_not_unlocked@domain.com"))

    with pytest.raises(QuotaExhausted):
        find()


@respx.mock
def test_a_plan_that_cannot_answer_is_dropped_rather_than_retried_all_run():
    respx.post(MATCH_URL).mock(return_value=httpx.Response(403, json={"error": "no access"}))

    async def go():
        async with httpx.AsyncClient() as client:
            service = ContactEnrichmentService(
                [ApolloProvider(client, settings_with_key())])
            first = await service.find("Lena Brandt", "heliosgrid.de")
            second = await service.find("Priya Raman", "zetacare.in")
            return first, second, service.calls

    first, second, calls = asyncio.run(go())
    assert first is None and second is None
    assert calls == 1, "a permanent refusal must not be asked again every company"


def test_nothing_is_asked_when_no_key_is_configured(settings):
    """Uses the fixture, not a bare Settings(): a bare one reads the developer's
    own .env and the test then passes or fails depending on whose machine it is."""
    async def go():
        async with httpx.AsyncClient() as client:
            service = ContactEnrichmentService.build(client, settings)
            assert not service.configured
            return await service.find("Lena Brandt", "heliosgrid.de")

    assert asyncio.run(go()) is None


# --------------------------------------------------------------------------- #
# What happens to the address once we have it
# --------------------------------------------------------------------------- #
def verify(address, person="Lena Brandt", domain="heliosgrid.de", status="verified",
           deliverability=None):
    from tvb_agent.providers.contacts import ContactFinding
    from tvb_agent.providers.email_verify import DeliverabilityResult

    finding = ContactFinding(address=address, provider="apollo",
                             provider_status=status, person_name=person)

    class _Verifier(EmailVerificationService):
        def __init__(self):
            super().__init__([])

        async def mx_for(self, domain):
            return ["mx1.example.net"]

        async def deliverability(self, address):
            if deliverability is None:
                return DeliverabilityResult("unknown", "none", detail="no provider configured")
            return DeliverabilityResult(deliverability, "zerobounce", detail=deliverability)

    return asyncio.run(verify_enriched_address(finding, founder(person), domain, _Verifier()))


def test_a_database_claim_with_no_delivery_check_is_not_verification():
    """A page-published address plus MX is reasonable evidence. A database claim
    that no page confirms and no provider checked is not - it is one assertion
    with a DNS record beside it, and this ladder was letting it through as
    VERIFIED because the branch simply did not exist."""
    record = verify("lena.brandt@heliosgrid.de")
    assert record.status is EmailStatus.SOURCE_VERIFIED
    assert not record.is_verified
    assert record.mx_ok and record.domain_matches_company
    assert record.evidence and record.evidence[0].authority is SourceAuthority.AGGREGATOR


@pytest.mark.parametrize("answer,expected", [
    ("deliverable", EmailStatus.VERIFIED),
    ("risky", EmailStatus.SOURCE_VERIFIED),
    ("unknown", EmailStatus.SOURCE_VERIFIED),
    ("undeliverable", EmailStatus.INVALID),
])
def test_every_deliverability_answer_has_its_own_rung(answer, expected):
    assert verify("lena.brandt@heliosgrid.de", deliverability=answer).status is expected


def test_an_enriched_address_must_belong_to_the_founder_we_named():
    """The enriched path checked only that the domain matched, so a colleague's
    address became the founder's verified contact."""
    for other in ("peter.schmidt@heliosgrid.de", "a.novak@heliosgrid.de"):
        record = verify(other, deliverability="deliverable")
        assert record.status is EmailStatus.FOUND_UNVERIFIED, other
        assert not record.is_verified
    # And a shared mailbox is caught one rung earlier, as a role account.
    assert verify("mail2@heliosgrid.de", deliverability="deliverable").status \
        is EmailStatus.ROLE_ONLY


def test_an_enriched_role_address_is_still_a_role_address():
    record = verify("info@heliosgrid.de")
    assert record.status is EmailStatus.ROLE_ONLY
    assert record.address is None
    assert record.role_fallback == "info@heliosgrid.de"


def test_an_address_on_a_different_domain_is_not_accepted():
    """An old employer, or a wrong match. Either way it is not evidence that this
    person can be reached there on this company's behalf."""
    record = verify("lena.brandt@someothercompany.com")
    assert record.status is EmailStatus.FOUND_UNVERIFIED
    assert not record.is_verified


# --------------------------------------------------------------------------- #
# Hunter: the provider that says where it looked
# --------------------------------------------------------------------------- #
FINDER_URL = "https://api.hunter.io/v2/email-finder"


def settings_with_hunter() -> Settings:
    s = blank()
    s.hunter_api_key = "test-hunter"
    return s


def hunter_response(**data):
    return httpx.Response(200, json={"data": data})


def hunter_find(person="Lena Brandt", domain="heliosgrid.de"):
    from tvb_agent.providers.contacts import HunterFinderProvider

    async def go():
        async with httpx.AsyncClient() as client:
            return await HunterFinderProvider(client, settings_with_hunter()).find(person, domain)
    return asyncio.run(go())


@respx.mock
def test_a_cited_address_is_accepted_and_keeps_its_citation():
    respx.get(FINDER_URL).mock(return_value=hunter_response(
        email="lena.brandt@heliosgrid.de", score=97,
        first_name="Lena", last_name="Brandt", position="Managing Director",
        sources=[{"uri": "https://heliosgrid.de/impressum", "domain": "heliosgrid.de",
                  "extracted_on": "2026-02-01"}],
        verification={"status": "valid", "date": "2026-02-01"}))

    found = hunter_find()
    assert found is not None
    assert found.address == "lena.brandt@heliosgrid.de"
    assert found.source_urls == ["https://heliosgrid.de/impressum"]
    assert "heliosgrid.de/impressum" in found.provenance


@respx.mock
def test_an_uncited_address_is_a_pattern_guess_and_is_refused():
    """With no cited page, Hunter has inferred the address from the domain's
    pattern. This project does not ship guesses - not its own, and not anybody
    else's."""
    respx.get(FINDER_URL).mock(return_value=hunter_response(
        email="lena.brandt@heliosgrid.de", score=72,
        first_name="Lena", last_name="Brandt", sources=[],
        verification={"status": "accept_all"}))

    assert hunter_find() is None


@respx.mock
def test_a_cited_address_for_the_wrong_person_is_refused():
    respx.get(FINDER_URL).mock(return_value=hunter_response(
        email="markus.weber@heliosgrid.de", score=95,
        first_name="Markus", last_name="Weber",
        sources=[{"uri": "https://heliosgrid.de/team"}]))

    assert hunter_find() is None


@respx.mock
def test_a_spent_monthly_allowance_stops_the_provider_rather_than_retrying():
    from tvb_agent.providers.base import QuotaExhausted

    respx.get(FINDER_URL).mock(return_value=httpx.Response(451, json={"errors": [{"id": "usage"}]}))

    with pytest.raises(QuotaExhausted):
        hunter_find()


def test_hunter_is_asked_before_apollo_because_it_cites_its_sources():
    async def go():
        async with httpx.AsyncClient() as client:
            s = blank()
            s.hunter_api_key = "h"
            s.apollo_api_key = "a"
            service = ContactEnrichmentService.build(client, s)
            return [p.name for p in service.providers]

    assert asyncio.run(go())[0] == "hunter"
