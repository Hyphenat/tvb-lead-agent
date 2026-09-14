"""Each gate's validator, including the cases that must NOT pass."""

import pytest

from tvb_agent.models import (
    ClaimStatus,
    Evidence,
    Evidenced,
    SourceAuthority,
    USPresenceLevel,
)
from tvb_agent.research.extractor import Claim, ExtractionResult
from tvb_agent.validation.validators import (
    SourceText,
    company_name_tokens,
    mentions_company,
    sentence_containing,
    validate_country,
    validate_founder,
    validate_funding,
    validate_technology,
    validate_us_presence,
)


def ev(url="https://acme.io/about", quote="x" * 30, authority=SourceAuthority.COMPANY_OWNED):
    return Evidence(url=url, quote=quote, authority=authority)


def claim(field, value, quote="x" * 30, authority=SourceAuthority.COMPANY_OWNED, **extra):
    return Claim(field=field, value=value, evidence=ev(quote=quote, authority=authority), extra=extra)


def result(*claims) -> ExtractionResult:
    return ExtractionResult(claims=list(claims))


# ------------------------------------------------------------------ funding --
def test_funding_from_company_site_is_accepted(settings):
    src = SourceText(url="https://acme.io/about", authority=SourceAuthority.COMPANY_OWNED,
                     text="We raised $2.4 million in a seed round in 2025.")
    got = validate_funding(result(), settings, sources=[src], company_name="Acme")
    assert got.known
    assert float(got.value.amount_usd) == pytest.approx(2_400_000)


def test_third_party_figure_needs_the_company_named_in_the_sentence(settings):
    """A press piece covering two startups must not leak one figure onto the other."""
    text = ("Bengaluru-based Zeta Care raised $2.4 million in seed funding. "
            "Separately, Lagos-based Nova Pay raised $3.1 million in a pre-Series A round.")
    src = SourceText(url="https://techcrunch.com/x", authority=SourceAuthority.TIER1_PRESS, text=text)

    zeta = validate_funding(result(), settings, sources=[src], company_name="Zeta Care")
    nova = validate_funding(result(), settings, sources=[src], company_name="Nova Pay")
    assert float(zeta.value.amount_usd) == pytest.approx(2_400_000)
    assert float(nova.value.amount_usd) == pytest.approx(3_100_000)


def test_low_authority_source_cannot_evidence_funding(settings):
    src = SourceText(url="https://randomblog.example/post", authority=SourceAuthority.UNKNOWN,
                     text="Acme raised $2.4 million in seed funding.")
    assert not validate_funding(result(), settings, sources=[src], company_name="Acme").known


@pytest.mark.parametrize("text", [
    "Acme was valued at $3 million post-money.",
    "The market Acme serves is worth $3 million annually.",
    "Acme received a $3 million grant from the innovation scheme.",
    "Acme Ventures closed a $3 million maiden fund.",
    "Acme expects to reach $3 million revenue by 2028.",
])
def test_non_funding_figures_never_satisfy_the_gate(settings, text):
    src = SourceText(url="https://acme.io/x", authority=SourceAuthority.COMPANY_OWNED, text=text)
    assert not validate_funding(result(), settings, sources=[src], company_name="Acme").known


def test_no_figure_at_all_leaves_the_field_unknown(settings):
    src = SourceText(url="https://acme.io/x", authority=SourceAuthority.COMPANY_OWNED,
                     text="Acme builds software for clinics.")
    got = validate_funding(result(), settings, sources=[src], company_name="Acme")
    assert not got.known
    assert got.status is ClaimStatus.UNKNOWN


# --------------------------------------------------------------- technology --
def test_platform_language_plus_product_page_passes():
    src = SourceText(url="https://acme.io/", authority=SourceAuthority.COMPANY_OWNED,
                     text="Acme is a platform with an API and a dashboard for clinics.")
    got = validate_technology(result(), ["https://acme.io/product"], [src])
    assert got.known and got.value is True


def test_services_only_business_is_rejected():
    desc = claim("description", "We are a boutique consulting firm advising hospitals.",
                 quote="We are a boutique consulting firm advising hospitals on strategy.")
    src = SourceText(url="https://acme.io/", authority=SourceAuthority.COMPANY_OWNED,
                     text="We are a boutique consulting firm. Our solutions help hospitals.")
    got = validate_technology(result(desc), ["https://acme.io/"], [src])
    assert not got.known


def test_no_product_evidence_leaves_unknown():
    src = SourceText(url="https://acme.io/", authority=SourceAuthority.COMPANY_OWNED,
                     text="Acme. Contact us.")
    assert not validate_technology(result(), ["https://acme.io/"], [src]).known


# ------------------------------------------------------------------ country --
def test_country_from_extracted_claim():
    got = validate_country(result(claim("country", "India")), None, [])
    assert got.value == "India"


def test_country_from_headquarters_sentence():
    src = SourceText(url="https://acme.io/about", authority=SourceAuthority.COMPANY_OWNED,
                     text="Acme is headquartered in Lagos, Nigeria and serves West Africa.")
    assert validate_country(result(), None, [src], company_name="Acme").value == "Nigeria"


def test_country_falls_back_to_country_code_tld():
    got = validate_country(result(), "acme.co.in", [])
    assert got.value == "India"
    assert got.confidence < 0.7, "a TLD inference should not be high-confidence"


def test_country_unknown_when_nothing_says_where():
    assert not validate_country(result(), "acme.io", []).known


# -------------------------------------------------------------- US presence --
def _country(name="India"):
    return Evidenced[str].of(name, [ev()])


def test_non_us_with_no_signals_passes(settings):
    got = validate_us_presence(result(), _country(), ["https://acme.io/about"], settings)
    assert got.known and got.value.level in (USPresenceLevel.NONE, USPresenceLevel.MINIMAL)


@pytest.mark.parametrize("kind", ["us_office", "us_subsidiary", "us_incorporation", "us_job_posting"])
def test_any_hard_signal_disqualifies(settings, kind):
    got = validate_us_presence(result(claim("us_signal", kind)), _country(), [], settings)
    assert got.value.level is USPresenceLevel.SIGNIFICANT


def test_unknown_headquarters_never_passes(settings):
    """The rule that keeps unverifiable companies out of the list."""
    got = validate_us_presence(result(), Evidenced[str].unknown(), [], settings)
    assert not got.known


def test_us_headquarters_is_significant(settings):
    got = validate_us_presence(result(), _country("United States"), [], settings)
    assert got.value.level is USPresenceLevel.SIGNIFICANT


def test_weak_signals_are_tolerated_up_to_the_threshold(settings):
    settings.max_weak_us_signals = 1
    one = validate_us_presence(result(claim("us_signal", "us_phone")), _country(), [], settings)
    assert one.value.level is USPresenceLevel.MINIMAL

    two = validate_us_presence(
        result(claim("us_signal", "us_phone"),
               claim("us_signal", "us_address_mention", quote="y" * 30)),
        _country(), [], settings)
    assert two.value.level is USPresenceLevel.SIGNIFICANT


# ------------------------------------------------------------------ founder --
def test_ceo_and_cofounder_titles_are_accepted():
    for title in ("Co-founder & CEO", "Founder", "CEO", "Managing Director"):
        got = validate_founder(result(claim("founder", "Jane Doe", title=title)))
        assert got.known, title


def test_other_executives_are_not_founders():
    for title in ("CTO", "Head of Sales", "VP Engineering", "Chief Marketing Officer"):
        got = validate_founder(result(claim("founder", "Jane Doe", title=title)))
        assert not got.known, title


def test_founder_with_stronger_title_wins():
    got = validate_founder(result(
        claim("founder", "Bob Smith", title="Managing Director"),
        claim("founder", "Jane Doe", title="Co-founder & CEO"),
    ))
    assert got.value.name == "Jane Doe"


# ------------------------------------------------------------------ helpers --
def test_company_name_tokens_drop_generic_words():
    assert "acme" in company_name_tokens("Acme Technologies Ltd")
    assert "technologies" not in company_name_tokens("Acme Technologies Ltd")


def test_mentions_company_handles_spacing_variants():
    assert mentions_company("ZetaCare announced a round", "Zeta Care")
    assert mentions_company("Zeta Care announced a round", "Zeta Care")
    assert not mentions_company("Nova Pay announced a round", "Zeta Care")


def test_sentence_containing_cuts_at_boundaries():
    text = "First sentence here. Acme raised $2 million today. Third sentence."
    idx = text.index("$2 million")
    got = sentence_containing(text, idx, idx + 10)
    assert "Acme raised" in got
    assert "First sentence" not in got


# ------------------------------------------------- rule-based fallbacks --
@pytest.mark.asyncio
async def test_description_and_sector_work_without_an_llm():
    """The brief requires description and sector in the output, so neither may
    depend on an optional API key being present."""

    from tvb_agent.research.extractor import GroundedExtractor, Source

    text = ("Zeta Care is a care coordination platform used by clinics across India.\n"
            "Our software connects providers, patients and social care teams through a "
            "single dashboard and API for every hospital we serve.")
    src = Source(url="https://zetacare.in/", text=text, authority=SourceAuthority.COMPANY_OWNED)

    res = await GroundedExtractor(None).extract("Zeta Care", [src])
    desc = res.first("description")
    sector = res.first("sector")
    assert desc and "care coordination platform" in str(desc.value)
    assert sector and "Health" in str(sector.value)
    # Both must still be grounded in the page.
    for claim_ in (desc, sector):
        assert claim_.evidence.url == "https://zetacare.in/"
        assert claim_.evidence.quote


def test_meta_description_is_kept_in_the_page_text():
    from tvb_agent.providers.fetcher import extract_text

    html = ('<html><head><title>Acme</title>'
            '<meta name="description" content="Acme is a payments platform for marketplaces in Kenya.">'
            '</head><body><p>Welcome</p></body></html>')
    text, title = extract_text(html)
    assert "payments platform for marketplaces" in text
    assert title == "Acme"


def test_boilerplate_is_not_used_as_a_description():
    import asyncio

    from tvb_agent.research.extractor import GroundedExtractor, Source

    text = ("We use cookies to improve your experience on this website and for analytics.\n"
            "Acme is a logistics software platform that helps fleets plan routes efficiently.")
    src = Source(url="https://acme.io/", text=text, authority=SourceAuthority.COMPANY_OWNED)
    res = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        GroundedExtractor(None).extract("Acme", [src]))
    desc = res.first("description")
    assert desc and "cookies" not in str(desc.value).lower()


# ------------------------------------------------ country must be a country --
@pytest.mark.parametrize("raw,expected", [
    ("India", "India"),
    ("USA", "United States"),
    ("Bengaluru", "India"),
    # A real run rejected a company with "Non-US headquarters (North)". A
    # sentence fragment accepted as a country is how an unverifiable company
    # slips past the US-presence gate.
    ("North", None),
    ("the leading provider", None),
    ("Series A", None),
    ("", None),
])
def test_only_real_countries_are_accepted(raw, expected):
    from tvb_agent.validation.geo import normalise_country

    assert normalise_country(raw) == expected


def test_an_unrecognisable_country_leaves_us_presence_unknown(settings):
    """Which means the gate fails, rather than passing on a fragment."""
    from tvb_agent.research.extractor import Claim as C

    ev_ = ev(quote="The company operates across the North region of the country and beyond.")
    res = result(C(field="country", value="North", evidence=ev_, extra={}))
    country = validate_country(res, None, [])
    assert not country.known
    assert not validate_us_presence(res, country, [], settings).known


# ------------------------------- is it an operating company at all? ---------
def own_site(text: str):
    return [SourceText(url="https://x.io/", text=text, authority=SourceAuthority.COMPANY_OWNED)]


@pytest.mark.parametrize("text", [
    "We are a seed fund backing European founders.",
    "Speedinvest is a venture capital firm investing across Europe.",
    "Our portfolio companies span fintech and healthtech.",
    "Latest news and analysis for the fintech industry. Advertise with us.",
    "We help companies build pitch decks. Our clients include leading startups.",
])
def test_funds_publications_and_agencies_are_skipped(text):
    """A real run spent most of its budget researching these before failing them."""
    from tvb_agent.validation.validators import looks_like_non_operating_company

    assert looks_like_non_operating_company(own_site(text))


@pytest.mark.parametrize("text", [
    # The phrase "seed fund" sits inside "seed funding" - substring matching
    # threw away a perfectly good company on exactly this.
    "Orbit Sim raised $2.2 million in seed funding in 2025.",
    "Zeta Care is a care coordination platform used by clinics across India.",
    "Nova Pay is an embedded finance platform for marketplaces in Nigeria.",
    "We raised a seed round last year and now serve 200 customers.",
])
def test_real_companies_are_not_skipped(text):
    from tvb_agent.validation.validators import looks_like_non_operating_company

    assert looks_like_non_operating_company(own_site(text)) is None


def test_only_the_companys_own_words_count():
    """A press article about a VC must not condemn the company it covers."""
    from tvb_agent.validation.validators import looks_like_non_operating_company

    press = [SourceText(url="https://press.com/x", authority=SourceAuthority.TIER1_PRESS,
                        text="The round was led by Acme Ventures, a venture capital firm.")]
    assert looks_like_non_operating_company(press) is None


@pytest.mark.parametrize("text", [
    # Run 7 skipped Rapyd, Torq and Zocks - three real operating companies -
    # because one phrase appeared somewhere on their site.
    "Rapyd is a global fintech platform. Our clients process payments in 100 "
    "countries. Book a demo. Pricing plans start from $99 per month.",
    "Torq. Newsroom. Security automation platform. Book a demo and see our "
    "platform run hyperautomation workflows.",
    "Zocks is an AI assistant for financial advisors and family office teams. "
    "Request a demo. Our platform integrates with your CRM.",
])
def test_one_stray_phrase_does_not_condemn_an_operating_company(text):
    from tvb_agent.validation.validators import looks_like_non_operating_company

    assert looks_like_non_operating_company(own_site(text)) is None


def test_two_agreeing_phrases_still_skip_when_nothing_is_being_sold():
    from tvb_agent.validation.validators import looks_like_non_operating_company

    text = ("Breaking news and analysis. Our newsroom covers the startup "
            "ecosystem daily. Read the latest news and analysis.")
    assert looks_like_non_operating_company(own_site(text))


# ------------------------------- how press states where a company is --------
@pytest.mark.parametrize("text,expected", [
    ("The Jakarta-based company Acme said the round was led by Init 6.", "Indonesia"),
    ("Acme, a Nairobi-based workflow automation platform, has raised $2 million.", "Kenya"),
    ("Kenyan AI startup Acme raises a pre-seed round.", "Kenya"),
    ("The Vietnamese studio Acme said the round was led by Makers Fund.", "Vietnam"),
    ("Acme is a Berlin-based logistics platform.", "Germany"),
])
def test_country_is_read_the_way_press_actually_writes_it(text, expected):
    """Run 7 could not establish a country for real non-US companies whose
    location sat in the opening clause of the headline that found them."""
    from tvb_agent.research.extractor import ExtractionResult
    from tvb_agent.validation.validators import validate_country

    src = [SourceText(url="https://technode.global/2026/01/01/acme-raises-seed-round/",
                      text=text, authority=SourceAuthority.TIER1_PRESS)]
    assert validate_country(ExtractionResult(), None, src, "Acme").value == expected


def test_a_location_in_a_sentence_about_someone_else_is_not_adopted():
    from tvb_agent.research.extractor import ExtractionResult
    from tvb_agent.validation.validators import validate_country

    src = [SourceText(url="https://technode.global/2026/01/01/x-raises-seed-round/",
                      text="The round was led by Berlin-based Cherry Ventures.",
                      authority=SourceAuthority.TIER1_PRESS)]
    assert not validate_country(ExtractionResult(), None, src, "Acme").known


@pytest.mark.parametrize("host,name,expected", [
    ("zetacare.io", "Zeta Care", True),
    ("usenova.com", "Nova Pay", True),          # the prefix startups add
    ("www.stockbit.com", "Stockbit", True),
    ("retail-insider.com", "QuoteMachine", False),   # the magazine that covered it
    ("techcrunch.com", "Flowt", False),
    ("shizune.co", "Amartha", False),
])
def test_only_a_companys_own_domain_is_adopted_as_its_website(host, name, expected):
    """A real run adopted the magazine that covered a company as the company's
    website and then attributed a journalist to it as founder."""
    from tvb_agent.agent import LeadAgent

    assert LeadAgent._host_matches_name(host, name) is expected


# --------------------------------------------------------------------------- #
# The false positive of run 10, and everything that let it through
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", [
    "Ex-Pipedrive Founder",   # the exact string run 10 offered TVB as a founder
    "Serial Entrepreneur",
    "Former CEO",
    "Head Of Growth",
    "Founding Partner",
])
def test_a_job_description_is_never_accepted_as_a_founder(name):
    """Run 10's single 'qualified' lead named a person called 'Ex-Pipedrive
    Founder'. Marketing copy read as a name is the precise failure the evidence
    pipeline exists to prevent, and a wrong lead is worse than no lead."""
    from tvb_agent.validation.validators import names_a_role_not_a_person

    assert names_a_role_not_a_person(name)


@pytest.mark.parametrize("name", [
    "Priya Raman", "Lena Brandt", "Chidi Okonkwo", "Jean-Luc Picard",
    "Ana de Souza", "Ahmed Al-Mansouri",
])
def test_real_people_are_still_accepted(name):
    from tvb_agent.validation.validators import names_a_role_not_a_person

    assert not names_a_role_not_a_person(name)


def test_a_role_phrase_cannot_reach_the_founder_field():
    import asyncio

    from tvb_agent.research.extractor import GroundedExtractor, Source
    from tvb_agent.validation.validators import validate_founder

    text = "Lift99 was built by an Ex-Pipedrive Founder, and is now run by the community."
    source = Source(url="https://lift99.co/", text=text, authority=SourceAuthority.COMPANY_OWNED)
    result = asyncio.run(GroundedExtractor(None).extract("Lift99", [source]))
    assert not validate_founder(result).known


@pytest.mark.parametrize("text", [
    "Lift99 is a startup hub for founders in Tallinn. Book a desk and join our "
    "community of founders.",
    "Our coworking space in Berlin has meeting rooms available and membership plans.",
])
def test_a_workspace_is_not_a_company_tvb_can_invest_in(text):
    from tvb_agent.validation.validators import looks_like_non_operating_company

    assert looks_like_non_operating_company(own_site(text))


@pytest.mark.parametrize("domain", ["serena.vc", "byfounders.vc", "hv.capital",
                                    "point9.fund", "acme-ventures.com"])
def test_a_funds_own_address_gives_it_away(domain):
    """Run 13 offered TVB "Serena" at serena.vc - a French venture firm - as a
    qualifying lead. A .vc domain is a fund's address essentially always, and no
    amount of reading the copy is needed to know it."""
    from tvb_agent.validation.validators import domain_is_an_investor

    assert domain_is_an_investor(domain)


@pytest.mark.parametrize("domain", ["zetacare.in", "novapay.ng", "heliosgrid.de",
                                    "orbitsim.pl", "flowt.co.ke"])
def test_real_company_domains_are_not_mistaken_for_funds(domain):
    from tvb_agent.validation.validators import domain_is_an_investor

    assert domain_is_an_investor(domain) is None


def test_a_fund_banked_by_an_earlier_build_is_dropped_on_re_read():
    from tvb_agent.models import (
        CompanyProfile,
        EmailRecord,
        EmailStatus,
        Evidence,
        Evidenced,
        Person,
    )
    from tvb_agent.validation.validators import lead_fails_current_rules

    ev = [Evidence(url="https://serena.vc/team", quote="Maxime Furet, CEO & Co-founder.",
                   authority=SourceAuthority.COMPANY_OWNED)]
    profile = CompanyProfile.new("Serena", "serena.vc")
    profile.founder = Evidenced[Person].of(Person(name="Maxime Furet", title="CEO & Co-founder"), ev)
    profile.email = EmailRecord(address="maxime@serena.vc", status=EmailStatus.VERIFIED)

    assert lead_fails_current_rules(profile) is not None


# --------------------------------------------------------------------------- #
# Findings from the full-code audit
# --------------------------------------------------------------------------- #
def test_the_largest_figure_decides_the_band_not_the_most_convenient_one():
    """"raised $2.5M seed ... closed a $45M Series B ... $52M total" qualified on
    the $2.5M, and the export carried no trace of the other two numbers. The band
    is a statement about the size of the company."""
    from tvb_agent.validation.money import best_qualifying_amount, parse_amounts

    text = ("Acme raised $2.5 million in a seed round in 2021. The company has now closed a "
            "$45 million Series B, bringing its total funding to $52 million.")
    best = best_qualifying_amount(parse_amounts(text), 1_000_000, 5_000_000)
    assert best is not None
    assert float(best.amount_usd) == 52_000_000, "picked a figure that fit instead of the truth"


@pytest.mark.parametrize("text", [
    "Acme secured a $4 million contract with a national carrier this year.",
    "The company arrived at a settlement of $3 million with its former supplier.",
])
def test_ordinary_words_are_not_read_as_recurring_revenue(text):
    """"arr" matched inside "carrier" and "arrived", so a customer contract and a
    litigation settlement both arrived at the funding gate as qualifying ARR."""
    from tvb_agent.validation.money import best_qualifying_amount, parse_amounts

    assert best_qualifying_amount(parse_amounts(text), 1_000_000, 5_000_000) is None


def test_a_bakery_does_not_operate_a_technology_platform():
    """"app" matched inside "happens", and a search snippet was enough standing."""
    from tvb_agent.research.extractor import ExtractionResult
    from tvb_agent.validation.validators import validate_technology

    text = ("Bakkerij Van Dam is a family bakery in Utrecht. Whatever happens, we bake fresh "
            "bread every morning since 1921. Visit our shop.")
    src = [SourceText(url="https://bakkerij.nl/", text=text,
                      authority=SourceAuthority.COMPANY_OWNED)]
    assert not validate_technology(ExtractionResult(), [], src).known


def test_a_snippet_alone_cannot_prove_a_technology_platform():
    from tvb_agent.research.extractor import ExtractionResult
    from tvb_agent.validation.validators import validate_technology

    src = [SourceText(url="https://news.example/x", text="Acme runs a software platform.",
                      authority=SourceAuthority.SEARCH_SNIPPET)]
    assert not validate_technology(ExtractionResult(), [], src).known


@pytest.mark.parametrize("phrase", [
    "Austin, Texas", "San Francisco, California", "New York", "London and New York",
])
def test_the_united_states_is_visible_to_the_country_resolvers(phrase):
    """CITY_COUNTRY and TLD_COUNTRY hold no US entries, so every resolver was
    structurally blind to the US: "based in London and New York" returned the
    United Kingdom, and the company passed the US-presence gate with a rationale
    asserting it had no US footprint."""
    from tvb_agent.validation.geo import country_from_city, normalise_country

    assert normalise_country(phrase) == "United States"
    assert country_from_city(phrase)[0] == "United States"


def test_a_founder_named_in_someone_elses_sentence_is_not_this_companys_founder():
    """The funding and country validators both demand the sentence name the
    company. The founder validator demanded nothing, which is how a trade
    journalist became a company's founder."""
    from tvb_agent.research.extractor import Claim, ExtractionResult
    from tvb_agent.validation.validators import validate_founder

    ev = Evidence(url="https://techcrunch.com/2026/round",
                  quote="The round was led by Accel, where CEO Marta Vidal sits on the board.",
                  authority=SourceAuthority.TIER1_PRESS)
    result = ExtractionResult(claims=[
        Claim(field="founder", value="Marta Vidal", evidence=ev, extra={"title": "CEO"})])

    assert not validate_founder(result, company_name="Zeta Care").known


def test_an_inferred_country_does_not_borrow_the_standing_of_a_quotation():
    """The TLD inference generated a sentence that appears on no page and stamped
    it COMPANY_OWNED - the same rank as a companies-house filing."""
    from tvb_agent.research.extractor import ExtractionResult
    from tvb_agent.validation.validators import validate_country

    got = validate_country(ExtractionResult(), "acme.de", [], "Acme")
    assert got.value == "Germany"
    assert got.evidence[0].authority is SourceAuthority.UNKNOWN
    assert "INFERRED" in (got.evidence[0].note or "")


def test_one_footer_phone_number_does_not_disqualify_a_company():
    """Weak signals were counted per (kind, page), so a single +1 number in a
    site-wide footer became three signals across three pages."""
    from tvb_agent.models import USPresenceAssessment, USPresenceLevel, USSignal, USSignalKind

    signals = [USSignal(kind=USSignalKind.US_PHONE, evidence=Evidence(
        url=f"https://acme.de/page{i}", quote="Call us on +1 (415) 555-0132",
        authority=SourceAuthority.COMPANY_OWNED)) for i in range(3)]
    assessment = USPresenceAssessment(level=USPresenceLevel.MINIMAL, signals=signals,
                                      rationale="test")
    assert assessment.weak_signal_count == 1


# --------------------------------------------------------------------------- #
# A country has to be a statement about the company, not a word on the page
# --------------------------------------------------------------------------- #
def test_a_nationality_in_page_content_is_not_a_headquarters():
    """The Spreaker failure, pinned.

    A podcast hosting platform's home page lists shows in many languages. One of
    them was "Little Talk in Slow French", and the agent read it as a French
    headquarters - which cleared the US-presence gate for a company owned by a
    US broadcaster. A demonym only locates a company when it modifies one.
    """
    from tvb_agent.validation.geo import country_from_demonym

    assert country_from_demonym("Little Talk in Slow French",
                                must_describe_a_company=True) is None
    assert country_from_demonym("Learn Japanese with us - episode 12",
                                must_describe_a_company=True) is None
    # And the real phrasings still resolve.
    assert country_from_demonym("the French fintech startup raised",
                                must_describe_a_company=True) == "France"
    assert country_from_demonym("a Kenyan AI-driven lending startup",
                                must_describe_a_company=True) == "Kenya"
    assert country_from_demonym("the Vietnamese studio behind the app",
                                must_describe_a_company=True) == "Vietnam"
    assert country_from_demonym("Estonian-based, founded 2021",
                                must_describe_a_company=True) == "Estonia"


def test_a_banked_lead_whose_country_quote_locates_nobody_is_dropped(monkeypatch):
    """And the fix reaches backwards, into leads already on file."""
    from decimal import Decimal

    from tvb_agent.models import (
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

    def profile_with(quote: str) -> CompanyProfile:
        ev = Evidence(url="https://example.com/", quote=quote,
                      authority=SourceAuthority.COMPANY_OWNED)
        return CompanyProfile(
            id="example.com", name="Example", domain="example.com",
            website=Evidenced.of("https://example.com", [ev]),
            country=Evidenced.of("France", [ev]),
            funding=Evidenced.of(MoneyAmount(raw="$2M", amount_original=Decimal("2000000"),
                                             currency="USD", amount_usd=Decimal("2000000"),
                                             fx_rate=Decimal("1"), fx_date="2026-01-01"), [ev]),
            founder=Evidenced.of(Person(name="Marie Dupont", title="CEO"), [ev]),
            email=EmailRecord(address="marie@example.com", status=EmailStatus.VERIFIED,
                              mx_ok=True, evidence=[ev]),
        )

    bad = lead_fails_current_rules(profile_with("Little Talk in Slow French"))
    assert bad and "never says where" in bad

    assert lead_fails_current_rules(profile_with("Example is based in Paris, France.")) is None
    assert lead_fails_current_rules(
        profile_with("Example is a French software company.")) is None
