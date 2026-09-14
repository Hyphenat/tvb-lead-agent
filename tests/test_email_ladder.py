"""The email subsystem carries the most risk, so it gets the most tests.

A wrong address is worse than a missing one: it burns a founder relationship
and it is invisible until it bounces on TVB's side.
"""

import pytest

from tvb_agent.models import EmailStatus, Evidence, Evidenced, Person, SourceAuthority
from tvb_agent.providers.email_verify import (
    DeliverabilityResult,
    EmailVerificationService,
    FixtureDeliverabilityProvider,
    find_emails_in_text,
    is_disposable,
    is_freemail,
    is_role_account,
    is_valid_syntax,
)
from tvb_agent.research.extractor import Claim, ExtractionResult
from tvb_agent.validation.email import (
    local_part_matches_name,
    registrable_root,
    validate_email,
)


def ev(url="https://acme.io/team", quote="Contact Jane Doe at jane.doe@acme.io for enquiries.",
       authority=SourceAuthority.COMPANY_OWNED):
    return Evidence(url=url, quote=quote, authority=authority)


def email_claim(addr, quote=None, authority=SourceAuthority.COMPANY_OWNED, owner=""):
    quote = quote or f"Contact Jane Doe at {addr} for enquiries."
    return Claim(field="email", value=addr, evidence=ev(quote=quote, authority=authority),
                 extra={"owner": owner})


FOUNDER = Evidenced[Person].of(Person(name="Jane Doe", title="Co-founder & CEO"), [ev()])


def verifier(status="deliverable", catch_all=None, has_provider=True):
    providers = []
    if has_provider:
        providers = [FixtureDeliverabilityProvider(
            default=DeliverabilityResult(status, "fixture", is_catch_all=catch_all))]
    return EmailVerificationService(providers)


# --------------------------------------------------------------- extraction --
def test_obfuscated_addresses_are_found():
    assert find_emails_in_text("write to jane [at] acme.io") == ["jane@acme.io"]
    assert find_emails_in_text("ravi at zeta dot co dot in") == ["ravi@zeta.co.in"]


def test_prose_is_not_mistaken_for_an_address():
    assert find_emails_in_text("See our docs at example.com for details") == []
    assert find_emails_in_text("Our office at 12 Main Street") == []


def test_classification_helpers():
    assert is_role_account("info@acme.io") and is_role_account("sales.team@acme.io")
    assert not is_role_account("jane.doe@acme.io")
    assert is_disposable("x@mailinator.com")
    assert is_freemail("jane@gmail.com")
    assert is_valid_syntax("a@b.co") and not is_valid_syntax("not-an-email")


def test_registrable_root_handles_subdomains_and_public_suffixes():
    assert registrable_root("mail.acme.io") == "acme"
    assert registrable_root("acme.co.uk") == "acme"


def test_local_part_matching_confirms_but_never_constructs():
    assert local_part_matches_name("jane.doe@acme.io", "Jane Doe")
    assert local_part_matches_name("jdoe@acme.io", "Jane Doe")
    assert not local_part_matches_name("bob@acme.io", "Jane Doe")


def test_no_pattern_generation_exists_in_the_codebase():
    """Guard against a future change that starts inventing addresses."""
    import inspect

    from tvb_agent.validation import email as email_module

    src = inspect.getsource(email_module)
    for forbidden in ('f"{first}.{last}@', 'f"{first}@', "'.'.join([first"):
        assert forbidden not in src, f"address construction pattern found: {forbidden}"


# -------------------------------------------------------------------- ladder --
@pytest.mark.asyncio
async def test_verified_requires_everything(settings):
    res = ExtractionResult(claims=[email_claim("jane.doe@acme.io")])
    rec = await validate_email(res, FOUNDER, "acme.io", verifier("deliverable"))
    assert rec.status is EmailStatus.VERIFIED
    assert rec.mx_ok and rec.domain_matches_company


@pytest.mark.asyncio
async def test_role_address_never_becomes_the_founder_contact(settings):
    res = ExtractionResult(claims=[email_claim("info@acme.io")])
    rec = await validate_email(res, FOUNDER, "acme.io", verifier())
    assert rec.status is EmailStatus.ROLE_ONLY
    assert rec.address is None
    assert rec.role_fallback == "info@acme.io"


@pytest.mark.asyncio
async def test_low_authority_source_is_not_enough(settings):
    res = ExtractionResult(claims=[email_claim(
        "jane.doe@acme.io", authority=SourceAuthority.SOCIAL_PROFILE)])
    rec = await validate_email(res, FOUNDER, "acme.io", verifier())
    assert rec.status is EmailStatus.FOUND_UNVERIFIED


@pytest.mark.asyncio
async def test_address_not_attributable_to_the_founder_is_not_verified(settings):
    res = ExtractionResult(claims=[email_claim(
        "operations@other-co.io", quote="Reach the operations desk at operations@other-co.io.")])
    rec = await validate_email(res, FOUNDER, "acme.io", verifier())
    assert rec.status is not EmailStatus.VERIFIED


@pytest.mark.asyncio
async def test_catch_all_domain_is_never_promoted_to_verified(settings):
    """A catch-all accepts anything, so 'deliverable' from one proves nothing."""
    res = ExtractionResult(claims=[email_claim("jane.doe@acme.io")])
    rec = await validate_email(res, FOUNDER, "acme.io", verifier("deliverable", catch_all=True))
    assert rec.status is EmailStatus.SOURCE_VERIFIED
    assert any("catch-all" in n.lower() for n in rec.notes)


@pytest.mark.asyncio
async def test_undeliverable_address_is_marked_invalid(settings):
    res = ExtractionResult(claims=[email_claim("jane.doe@acme.io")])
    rec = await validate_email(res, FOUNDER, "acme.io", verifier("undeliverable"))
    assert rec.status is EmailStatus.INVALID


@pytest.mark.asyncio
async def test_risky_address_is_not_promoted(settings):
    res = ExtractionResult(claims=[email_claim("jane.doe@acme.io")])
    rec = await validate_email(res, FOUNDER, "acme.io", verifier("risky"))
    assert rec.status is EmailStatus.SOURCE_VERIFIED


@pytest.mark.asyncio
async def test_domain_without_mx_is_invalid(settings):
    """conftest stubs any domain containing 'nomx' as having no MX records."""
    res = ExtractionResult(claims=[email_claim(
        "jane.doe@nomx.io", quote="Contact Jane Doe at jane.doe@nomx.io for enquiries.")])
    rec = await validate_email(res, FOUNDER, "nomx.io", verifier())
    assert rec.status is EmailStatus.INVALID
    assert not rec.mx_ok


@pytest.mark.asyncio
async def test_without_a_provider_source_plus_mx_is_enough_but_is_recorded(settings):
    res = ExtractionResult(claims=[email_claim("jane.doe@acme.io")])
    rec = await validate_email(res, FOUNDER, "acme.io", verifier(has_provider=False))
    assert rec.status is EmailStatus.VERIFIED
    assert any("deliverability" in n.lower() for n in rec.notes), \
        "the limitation must travel with the lead"


@pytest.mark.asyncio
async def test_no_founder_means_no_verified_email(settings):
    res = ExtractionResult(claims=[email_claim("jane.doe@acme.io")])
    rec = await validate_email(res, Evidenced[Person].unknown(), "acme.io", verifier())
    assert rec.status is EmailStatus.FOUND_UNVERIFIED


@pytest.mark.asyncio
async def test_disposable_addresses_are_discarded(settings):
    res = ExtractionResult(claims=[email_claim(
        "jane.doe@mailinator.com", quote="Contact Jane Doe at jane.doe@mailinator.com.")])
    rec = await validate_email(res, FOUNDER, "acme.io", verifier())
    assert rec.status is not EmailStatus.VERIFIED


@pytest.mark.asyncio
async def test_freemail_only_counts_from_the_company_own_site(settings):
    off_site = ExtractionResult(claims=[email_claim(
        "jane.doe@gmail.com", quote="Contact Jane Doe at jane.doe@gmail.com.",
        authority=SourceAuthority.TIER1_PRESS)])
    rec = await validate_email(off_site, FOUNDER, "acme.io", verifier())
    assert rec.status is EmailStatus.FOUND_UNVERIFIED

    on_site = ExtractionResult(claims=[email_claim(
        "jane.doe@gmail.com", quote="Contact Jane Doe at jane.doe@gmail.com.",
        authority=SourceAuthority.COMPANY_OWNED)])
    rec2 = await validate_email(on_site, FOUNDER, "acme.io", verifier())
    assert rec2.status is EmailStatus.VERIFIED


@pytest.mark.asyncio
async def test_personal_address_is_preferred_over_role_address(settings):
    res = ExtractionResult(claims=[email_claim("info@acme.io"), email_claim("jane.doe@acme.io")])
    rec = await validate_email(res, FOUNDER, "acme.io", verifier())
    assert rec.address == "jane.doe@acme.io"
    assert rec.role_fallback == "info@acme.io"


# --------------------------------------------------------------------------- #
# What happens when the deliverability provider runs out
# --------------------------------------------------------------------------- #
def test_zerobounce_credit_exhaustion_is_a_quota_error_not_an_unknown_answer(respx_mock):
    """ZeroBounce answers HTTP 200 with sub_status "exceeded_api_credits".
    Reading that as an ordinary "unknown" leaves the provider enabled and every
    later address stuck one rung below verified - a run with five credits left
    then finishes with zero qualified leads."""
    import asyncio

    import httpx

    from tvb_agent.config import Settings
    from tvb_agent.providers.base import QuotaExhausted
    from tvb_agent.providers.email_verify import ZeroBounceProvider

    respx_mock.get("https://api.zerobounce.net/v2/validate").mock(
        return_value=httpx.Response(200, json={"status": "unknown",
                                               "sub_status": "exceeded_api_credits"}))

    async def go():
        async with httpx.AsyncClient() as client:
            provider = ZeroBounceProvider(client, Settings(zerobounce_api_key="k"))
            with pytest.raises(QuotaExhausted):
                await provider.check("founder@acme.io")

    asyncio.run(go())


# --------------------------------------------------------------------------- #
# Addresses that exist only inside a mailto: link
# --------------------------------------------------------------------------- #
def test_an_address_published_only_as_a_mailto_link_is_still_read():
    """Run 8 rejected every company it researched for having no email address on
    any page it read. A great many sites publish the address only in the href -
    the visible text is the person's name, "Contact", or an icon."""
    from tvb_agent.providers.fetcher import extract_text

    html = ('<html><head><title>Impressum</title></head><body>'
            '<h1>Helios Grid GmbH</h1>'
            '<p>Vertreten durch: Dr. Lena Brandt</p>'
            '<p>E-Mail: <a href="mailto:lena.brandt@heliosgrid.de">Lena Brandt</a></p>'
            '<p><a href="mailto:info@heliosgrid.de"><img src="x" alt="mail"></a></p>'
            '</body></html>')
    text, _ = extract_text(html)

    assert "lena.brandt@heliosgrid.de" in text
    assert "info@heliosgrid.de" in text
    # The link's own label travels with the address, because that label is very
    # often the founder's name - which is what attributes the address to them.
    assert "Lena Brandt" in text.split("lena.brandt@heliosgrid.de")[0].rsplit("\n", 1)[-1]


def test_a_mailto_only_imprint_produces_an_attributed_founder_address():
    import asyncio

    from tvb_agent.models import SourceAuthority
    from tvb_agent.providers.fetcher import extract_text
    from tvb_agent.research.extractor import GroundedExtractor, Source
    from tvb_agent.validation.validators import validate_founder

    html = ('<html><body><p>Vertreten durch: Dr. Lena Brandt (Geschäftsführerin)</p>'
            '<p>E-Mail: <a href="mailto:lena.brandt@heliosgrid.de">Lena Brandt</a></p>'
            '</body></html>')
    text, _ = extract_text(html)
    source = Source(url="https://heliosgrid.de/impressum", text=text,
                    authority=SourceAuthority.COMPANY_OWNED)
    result = asyncio.run(GroundedExtractor(None).extract("Helios Grid", [source]))

    assert validate_founder(result).value.name == "Lena Brandt"
    assert "lena.brandt@heliosgrid.de" in [str(c.value) for c in result.by_field("email")]


@pytest.mark.parametrize("address,expected", [
    # Run 9 rejected real founder addresses of these shapes as "not attributed".
    ("l.brandt@heliosgrid.de", True),
    ("lena.b@heliosgrid.de", True),
    ("brandt@heliosgrid.de", True),
    ("lena.brandt@heliosgrid.de", True),
    ("lbrandt@heliosgrid.de", True),
    # And these still are not her.
    ("info@heliosgrid.de", False),
    ("john.smith@heliosgrid.de", False),
    ("lb@heliosgrid.de", False),      # two letters prove nothing
    ("sales@heliosgrid.de", False),
])
def test_an_address_carrying_the_founders_own_name_is_attributed_to_them(address, expected):
    from tvb_agent.validation.email import local_part_matches_name

    assert local_part_matches_name(address, "Lena Brandt") is expected


@pytest.mark.parametrize("address", [
    "tallinn@lift99.co",   # run 10 put this forward as a founder contact
    "london@acme.com",
    "berlin@acme.de",
    "singapore@acme.sg",
])
def test_an_office_mailbox_is_a_role_account_not_a_person(address):
    """A city is a desk, not a person."""
    from tvb_agent.providers.email_verify import is_role_account

    assert is_role_account(address)


@pytest.mark.parametrize("address", ["priya@zetacare.io", "lena.brandt@heliosgrid.de"])
def test_personal_addresses_are_not_mistaken_for_office_mailboxes(address):
    from tvb_agent.providers.email_verify import is_role_account

    assert not is_role_account(address)


def test_a_single_shared_word_does_not_attribute_an_address_to_someone():
    """"Pipedrive" appears on any page about the company; it identifies nobody."""
    from tvb_agent.validation.email import quote_attributes_to

    assert not quote_attributes_to(
        "Our community was built by an Ex-Pipedrive founder in Tallinn.",
        "Ex-Pipedrive Founder")
    assert quote_attributes_to(
        "Contact Priya Raman, Co-founder and CEO, at this address.", "Priya Raman")


# --------------------------------------------------------------------------- #
# Addresses that must never be invented
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("prose", [
    "Learn more at acme dot com",
    "Meet the founder at acme dot com",
    "She joined us at stripe dot com in 2019",
    "Read our story at banyu dot co dot id",
    "Follow us at acme [dot] io",
])
def test_ordinary_prose_never_becomes_an_email_address(prose):
    """"our founder at acme dot com" produced founder@acme.com - an address that
    exists nowhere, manufactured from an English sentence and then shipped as a
    verified founder contact. This is the one failure the project cannot afford."""
    from tvb_agent.providers.email_verify import find_emails_in_text

    assert find_emails_in_text(prose) == []


@pytest.mark.parametrize("text,expected", [
    ("priya.raman [at] zetacare [dot] in", "priya.raman@zetacare.in"),
    ("chidi (at) novapay (dot) ng", "chidi@novapay.ng"),
    ("f.fussek at kaikosystems dot com", "f.fussek@kaikosystems.com"),
    ("lena.brandt@heliosgrid.de", "lena.brandt@heliosgrid.de"),
])
def test_genuine_obfuscation_is_still_read(text, expected):
    from tvb_agent.providers.email_verify import find_emails_in_text

    assert expected in find_emails_in_text(text)


@pytest.mark.parametrize("address", [
    # components beyond the first, and digit suffixes
    "the.team@acme.com", "new.business@acme.com", "info2@acme.com", "team2@acme.com",
    # the languages whose imprint pages this agent deliberately fetches
    "kontakt@acme.de", "contacto@acme.es", "contatti@acme.it", "presse@acme.de",
    "hi@acme.com", "hey@acme.com", "reception@acme.com",
])
def test_shared_mailboxes_are_never_a_founder_contact(address):
    from tvb_agent.providers.email_verify import is_role_account

    assert is_role_account(address)


@pytest.mark.parametrize("address,name", [
    ("crawford@acme.com", "Tom Ford"),
    ("s.bernstein@acme.com", "Ben Stein"),
    ("markus@acme.com", "Mark Ross"),
    ("martinez@acme.com", "Jean Martin"),
    ("goldberg@acme.com", "Karl Berg"),
    ("alexandra@acme.com", "Alex Kim"),
])
def test_a_colleague_is_not_the_founder_because_their_name_starts_the_same(address, name):
    """Matching the name as a bare substring made crawford@ belong to Tom Ford.
    Any founder whose name is a common prefix was attributed to somebody else's
    address on the same domain."""
    from tvb_agent.validation.email import local_part_matches_name

    assert not local_part_matches_name(address, name)


@pytest.mark.parametrize("address", [
    "ceo@acme.com", "founder@acme.com", "cofounder@acme.de", "md@acme.co.uk",
])
def test_the_named_officers_own_desk_is_their_contact(address):
    """The brief asks for "name and email of the CEO or Co-founder". Once we have
    independently established who the CEO is, ceo@acme.com is both: it reaches
    that person. Refusing it confuses "shared inbox" with "not personal"."""
    from tvb_agent.providers.email_verify import is_founder_office_account, is_role_account

    assert is_founder_office_account(address)
    assert not is_role_account(address)


@pytest.mark.parametrize("address", [
    "info@acme.com", "hello@acme.com", "sales@acme.com", "kontakt@acme.de",
    "tallinn@acme.com", "support@acme.com",
])
def test_a_shared_company_inbox_is_still_not_a_founder_contact(address):
    from tvb_agent.providers.email_verify import is_founder_office_account, is_role_account

    assert is_role_account(address)
    assert not is_founder_office_account(address)
