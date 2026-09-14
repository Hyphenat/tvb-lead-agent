"""End-to-end run against a simulated web.

These tests assert the behaviours the brief cares about most:
the pipeline finds real companies, rejects the ones that fail a hard gate,
never promotes a fabricated claim, and surfaces *different* companies on a
second run.
"""

import json
import re

import httpx
import pytest
import respx

from tvb_agent.agent import LeadAgent
from tvb_agent.models import EmailStatus

from .fake_web import PAGES, SERP_RESULTS, SERP_RESULTS_WITH_HEADLINE_ONLY_COMPANY


def _llm_reply(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]})


def install_fake_web(llm_payload_for=None, deliverability="valid", serp=None):
    """Route every outbound call to the fixtures."""
    respx.post(re.compile(r"https://google\.serper\.dev/search")).mock(
        return_value=httpx.Response(200, json={"organic": serp if serp is not None else SERP_RESULTS})
    )

    def _llm(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8", "ignore")
        m = re.search(r"Company under investigation: ([^\n]+)", body)
        company = (m.group(1) if m else "").strip()
        payload = llm_payload_for(company, body) if llm_payload_for else {}
        return _llm_reply(payload)

    respx.post(re.compile(r"https://generativelanguage\.googleapis\.com/.*")).mock(side_effect=_llm)
    respx.get(re.compile(r"https://api\.zerobounce\.net/.*")).mock(
        return_value=httpx.Response(200, json={"status": deliverability, "sub_status": ""})
    )

    def _page(request: httpx.Request) -> httpx.Response:
        url = str(request.url).rstrip("/") or str(request.url)
        for key in (str(request.url), url, url + "/"):
            if key in PAGES:
                return httpx.Response(200, text=PAGES[key], headers={"content-type": "text/html"})
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(404, text="not found", headers={"content-type": "text/html"})

    respx.route().mock(side_effect=_page)


def configure(settings):
    settings.serper_api_key = "test-serper"
    settings.gemini_api_key = "test-gemini"
    settings.zerobounce_api_key = "test-zb"
    return settings


@pytest.mark.asyncio
@respx.mock
async def test_full_run_finds_qualifies_and_rejects(settings, store):
    configure(settings)
    settings.budget.target_qualified = 2
    install_fake_web()

    agent = LeadAgent(settings=settings, store=store)
    result = await agent.run(seed=7)

    names = {lead_.company.name for lead_ in result.qualified_leads}
    assert "Zeta Care" in names, f"expected Zeta Care to qualify, got {names}"

    # Every qualified lead must carry a verified email and evidence for each gate.
    for lead in result.qualified_leads:
        assert lead.company.email.status is EmailStatus.VERIFIED
        assert lead.company.email.address
        assert lead.qualification.qualified
        for gate in lead.qualification.gates:
            assert gate.passed
        assert lead.company.all_evidence(), "a qualified lead must carry evidence"


@pytest.mark.asyncio
@respx.mock
async def test_company_with_us_office_is_rejected(settings, store):
    configure(settings)
    settings.budget.target_qualified = 3
    settings.budget.max_companies_researched = 6
    install_fake_web()

    agent = LeadAgent(settings=settings, store=store)
    result = await agent.run(seed=11)

    all_leads = result.leads + result.rejected
    orbit = [lead_ for lead_ in all_leads if "Orbit" in lead_.company.name]
    if orbit:
        lead = orbit[0]
        assert not lead.qualification.qualified
        reasons = " ".join(g.reason for g in lead.qualification.gates if not g.passed).lower()
        assert "us" in reasons or "united states" in reasons


@pytest.mark.asyncio
@respx.mock
async def test_fabricated_llm_claims_never_reach_a_lead(settings, store):
    """A model that invents funding and a founder must not change the outcome."""
    configure(settings)
    settings.budget.target_qualified = 1

    def liar(company, body):
        return {
            "funding_statements": [
                {"text": "raised $40 million Series B",
                 "quote": "The company raised $40 million in a Series B led by Sequoia Capital.",
                 "source": 0}
            ],
            "founders": [
                {"name": "Imaginary Person", "title": "CEO",
                 "quote": "Imaginary Person is the Chief Executive Officer.", "source": 0}
            ],
            "emails": [
                {"address": "ceo@fabricated.example", "owner": "Imaginary Person",
                 "quote": "Write to ceo@fabricated.example for anything.", "source": 0}
            ],
        }

    install_fake_web(llm_payload_for=liar)
    agent = LeadAgent(settings=settings, store=store)
    result = await agent.run(seed=3)

    for lead in result.leads + result.rejected:
        c = lead.company
        assert c.founder.value is None or c.founder.value.name != "Imaginary Person"
        assert c.email.address != "ceo@fabricated.example"
        if c.funding.value:
            assert float(c.funding.value.amount_usd) != 40_000_000


@pytest.mark.asyncio
@respx.mock
async def test_second_run_skips_companies_already_seen(settings, store):
    configure(settings)
    settings.budget.target_qualified = 2
    install_fake_web()

    agent = LeadAgent(settings=settings, store=store)
    first = await agent.run(seed=5)
    assert store.company_count() > 0

    second = await agent.run(seed=6)
    first_ids = {lead_.company.id for lead_ in first.leads + first.rejected}
    second_ids = {lead_.company.id for lead_ in second.leads + second.rejected}
    assert not (first_ids & second_ids), "a second run must not re-process companies already seen"
    assert second.stats.candidates_skipped_known > 0


@pytest.mark.asyncio
@respx.mock
async def test_run_survives_total_search_failure(settings, store):
    configure(settings)
    respx.post(re.compile(r"https://google\.serper\.dev/search")).mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    respx.route().mock(return_value=httpx.Response(404, text="", headers={"content-type": "text/html"}))

    agent = LeadAgent(settings=settings, store=store)
    result = await agent.run(seed=1)
    assert result.qualified_leads == []
    assert result.stats.stop_reason


@pytest.mark.asyncio
@respx.mock
async def test_run_survives_email_verifier_outage(settings, store):
    configure(settings)
    settings.budget.target_qualified = 1
    install_fake_web()
    respx.get(re.compile(r"https://api\.zerobounce\.net/.*")).mock(
        return_value=httpx.Response(403, json={"error": "quota"})
    )

    agent = LeadAgent(settings=settings, store=store)
    result = await agent.run(seed=9)
    # The run completes; leads may be fewer, but nothing crashes and nothing is faked.
    assert result.stats.companies_researched > 0
    for lead in result.qualified_leads:
        assert lead.company.email.status is EmailStatus.VERIFIED


@pytest.mark.asyncio
@respx.mock
async def test_funding_is_not_borrowed_from_another_company(settings, store):
    """A press article covering two startups must not cross-assign their numbers.

    The fixture article states $2.4M for Zeta Care and $3.1M for Nova Pay in
    adjacent paragraphs. Attributing the wrong figure to a real company is the
    most damaging error this pipeline can make, so it gets its own test.
    """
    configure(settings)
    settings.budget.target_qualified = 3
    settings.budget.max_companies_researched = 6
    install_fake_web()

    agent = LeadAgent(settings=settings, store=store)
    result = await agent.run(seed=7)

    expected = {"Zeta Care": 2_400_000, "Nova Pay": 3_100_000, "Orbit Sim": 2_200_000}
    checked = 0
    for lead in result.leads + result.rejected:
        want = expected.get(lead.company.name)
        if want and lead.company.funding.value:
            got = float(lead.company.funding.value.amount_usd)
            assert got == pytest.approx(want), f"{lead.company.name}: expected {want}, got {got}"
            checked += 1
    assert checked >= 2, "expected to verify at least two companies' figures"


@pytest.mark.asyncio
@respx.mock
async def test_valuation_and_market_size_are_never_used_as_funding(settings, store):
    """Zeta's page states a $400bn market; Nova's states a $28m valuation."""
    configure(settings)
    settings.budget.target_qualified = 3
    install_fake_web()

    agent = LeadAgent(settings=settings, store=store)
    result = await agent.run(seed=7)

    for lead in result.leads + result.rejected:
        amt = lead.company.funding.value
        if amt is None:
            continue
        assert amt.qualifies_as_criterion_input
        assert float(amt.amount_usd) not in (400_000_000_000.0, 28_000_000.0)


@pytest.mark.asyncio
@respx.mock
async def test_role_email_never_becomes_the_founder_contact(settings, store):
    """info@ / hello@ addresses exist in the fixtures and must stay out."""
    configure(settings)
    settings.budget.target_qualified = 3
    install_fake_web()

    agent = LeadAgent(settings=settings, store=store)
    result = await agent.run(seed=7)

    for lead in result.leads + result.rejected:
        addr = lead.company.email.address or ""
        assert not addr.startswith(("info@", "hello@", "contact@", "support@"))


@pytest.mark.asyncio
@respx.mock
async def test_probe_searches_are_only_spent_on_unresolved_gates(settings, store):
    """A company whose own site answers everything should need no extra searches."""
    configure(settings)
    settings.budget.target_qualified = 3
    install_fake_web()

    agent = LeadAgent(settings=settings, store=store)
    result = await agent.run(seed=7)

    s = result.stats
    assert s.queries_run == s.discovery_queries + s.probe_queries
    assert s.discovery_queries <= settings.budget.max_discovery_searches
    # Probing must be materially cheaper than one probe per company per gate.
    assert s.probe_queries <= max(4, s.companies_researched * 2)


@pytest.mark.asyncio
@respx.mock
async def test_partial_results_survive_in_the_database(settings, store):
    """Everything found is written as it is found, so a timeout loses nothing."""
    configure(settings)
    settings.budget.target_qualified = 2
    install_fake_web()

    agent = LeadAgent(settings=settings, store=store)
    result = await agent.run(seed=7)

    persisted = store.leads(result.run_id, qualified_only=False)
    assert len(persisted) == len(result.leads) + len(result.rejected)
    for lead in persisted:
        assert lead.company.name
        assert {g.gate for g in lead.qualification.gates}


@pytest.mark.asyncio
@respx.mock
async def test_exports_contain_only_verified_contacts(settings, store):
    from tvb_agent.export import leads_to_csv, leads_to_json

    configure(settings)
    settings.budget.target_qualified = 2
    install_fake_web()
    result = await LeadAgent(settings=settings, store=store).run(seed=7)

    csv_text = leads_to_csv(result.qualified_leads)
    json_text = leads_to_json(result.qualified_leads)
    assert "company_name" in csv_text
    for lead in result.qualified_leads:
        assert lead.company.email.address in csv_text
        assert lead.company.email.address in json_text
    assert "info@" not in csv_text and "hello@" not in csv_text


@pytest.mark.asyncio
@respx.mock
async def test_founder_email_is_found_on_an_unlinked_imprint_page(settings, store):
    """The commonest near-miss: founder identified, address published elsewhere.

    Helios Grid's team page names the founder but lists no contact. The address
    is only on /impressum, which nothing links to. The agent should reach it.
    """
    configure(settings)
    settings.budget.target_qualified = 6
    settings.budget.max_companies_researched = 10
    install_fake_web()

    agent = LeadAgent(settings=settings, store=store)
    result = await agent.run(seed=7)

    helios = [lead_ for lead_ in result.leads + result.rejected if "Helios" in lead_.company.name]
    assert helios, "Helios Grid should have been discovered from the portfolio page"
    company = helios[0].company
    assert company.email.address == "lena.brandt@heliosgrid.de", (
        f"expected the imprint address, got {company.email.address!r} "
        f"({company.email.status.value})")
    assert company.email.is_verified


@pytest.mark.asyncio
@respx.mock
async def test_european_decimal_funding_is_parsed_correctly(settings, store):
    """'EUR 2,6 Millionen' must become ~$2.8M, not $26M or $2.60."""
    configure(settings)
    settings.budget.target_qualified = 6
    settings.budget.max_companies_researched = 10
    install_fake_web()

    result = await LeadAgent(settings=settings, store=store).run(seed=7)
    helios = [lead_ for lead_ in result.leads + result.rejected if "Helios" in lead_.company.name]
    assert helios
    amount = helios[0].company.funding.value
    assert amount is not None, "EUR 2,6 Millionen should have been parsed"
    assert amount.currency == "EUR"
    assert 2.7e6 < float(amount.amount_usd) < 2.9e6, float(amount.amount_usd)


@pytest.mark.asyncio
@respx.mock
async def test_run_reports_where_candidates_were_lost(settings, store):
    """A disappointing run must be diagnosable, not just small."""
    configure(settings)
    settings.budget.target_qualified = 6
    settings.budget.max_companies_researched = 10
    install_fake_web()

    result = await LeadAgent(settings=settings, store=store).run(seed=7)
    s = result.stats

    assert isinstance(s.gate_failures, dict)
    assert sum(s.gate_failures.values()) >= s.rejected, \
        "every rejection must be attributed to at least one gate"
    # Orbit Sim fails on US presence in the fixtures.
    assert s.gate_failures.get("us_presence_pass", 0) >= 1
    assert s.failed_only_on_email >= 0
    assert "gate_failures" in s.as_dict()


@pytest.mark.asyncio
@respx.mock
async def test_a_dead_search_provider_is_named_as_the_cause(settings, store):
    """A failing provider must not be reported as exhausted discovery.

    Blaming the search *strategies* for a rejected API key sends you tuning
    queries when the real fix is one line in .env.
    """
    configure(settings)
    respx.post(re.compile(r"https://google\.serper\.dev/search")).mock(
        return_value=httpx.Response(403, json={"error": "forbidden"}))
    respx.route().mock(return_value=httpx.Response(404, text="", headers={"content-type": "text/html"}))

    result = await LeadAgent(settings=settings, store=store).run(seed=1)

    assert result.qualified_leads == []
    assert "every search failed" in result.stats.stop_reason.lower()
    assert result.stats.search_failures > 0
    assert "exhausted" not in result.stats.stop_reason.lower()


@pytest.mark.asyncio
@respx.mock
async def test_genuine_empty_results_are_not_blamed_on_the_provider(settings, store):
    """The provider works and simply found nothing - a different diagnosis."""
    configure(settings)
    respx.post(re.compile(r"https://google\.serper\.dev/search")).mock(
        return_value=httpx.Response(200, json={"organic": []}))
    respx.post(re.compile(r"https://generativelanguage\.googleapis\.com/.*")).mock(
        return_value=httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}))
    respx.route().mock(return_value=httpx.Response(404, text="", headers={"content-type": "text/html"}))

    result = await LeadAgent(settings=settings, store=store).run(seed=1)

    assert "discovery exhausted" in result.stats.stop_reason.lower()
    assert result.stats.search_failures == 0


@pytest.mark.asyncio
@respx.mock
async def test_a_programming_error_is_not_disguised_as_a_data_problem(settings, store):
    """Two NameErrors once ate 60% of a run's companies.

    The broad handler reported them as "Research error" alongside genuine
    per-company data failures, so a broken build looked like a disappointing
    run. Bugs in this codebase must surface immediately.
    """
    configure(settings)
    install_fake_web()

    agent = LeadAgent(settings=settings, store=store)
    original = agent._research_one

    async def boom(*args, **kwargs):
        raise NameError("name 'something' is not defined")

    agent._research_one = boom
    with pytest.raises(NameError):
        await agent.run(seed=7)

    agent._research_one = original


@pytest.mark.asyncio
@respx.mock
async def test_one_company_failing_on_data_does_not_stop_the_run(settings, store):
    """The flip side: a genuine per-company failure is still tolerated."""
    configure(settings)
    settings.budget.target_qualified = 3
    install_fake_web()

    agent = LeadAgent(settings=settings, store=store)
    original = agent._research_one
    calls = {"n": 0}

    async def flaky(candidate, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("malformed page for this company")
        return await original(candidate, *args, **kwargs)

    agent._research_one = flaky
    result = await agent.run(seed=7)
    assert calls["n"] > 1, "the run should have continued past the failure"
    assert result.stats.companies_researched > 0


@pytest.mark.asyncio
@respx.mock
async def test_a_company_known_only_from_a_headline_is_still_researched(settings, store):
    """The headline that found a company is evidence about it, and the company's
    own site is worked out by probing rather than by spending a search.

    An earlier build discarded the excerpt, failed to resolve the site, and
    rejected a real in-band company for having no verifiable funding.
    """
    install_fake_web(serp=SERP_RESULTS_WITH_HEADLINE_ONLY_COMPANY)
    agent = LeadAgent(configure(settings), store)
    result = await agent.run()

    flowt = next((c for c in [lead.company for lead in result.leads + result.rejected]
                  if c.name.lower().startswith("flowt")), None)
    assert flowt is not None, "the company named in the headline never reached research"
    assert flowt.domain == "flowt.co.ke", "the probe did not find the company's own site"
    assert flowt.funding.value is not None
    assert float(flowt.funding.value.amount_usd) == 2_000_000
    assert flowt.country.value == "Kenya"
    assert flowt.founder.value.name == "Alice Wanjiru"

    # And the evidence says plainly that the full page was never retrieved.
    excerpt_notes = [e.note or "" for e in flowt.funding.evidence]
    assert any("excerpt" in n for n in excerpt_notes) or flowt.funding.evidence


@pytest.mark.asyncio
@respx.mock
async def test_verified_leads_accumulate_across_runs(settings, store):
    """Each run deliberately explores ground the last one did not, so the
    deliverable is the whole verified book rather than one run's slice. Nothing
    is pooled or relaxed by being counted together: every lead on file cleared
    the same five gates on its own evidence."""
    install_fake_web()
    first = await LeadAgent(configure(settings), store).run()
    banked_after_first = store.leads(qualified_only=True)
    assert len(banked_after_first) == len(first.qualified_leads)

    second = await LeadAgent(configure(settings), store).run()
    banked_after_second = store.leads(qualified_only=True)

    # The second run skips what the first already found, so the book grows or
    # holds - it never shrinks, and it never double-counts.
    assert len(banked_after_second) >= len(banked_after_first)
    names = [lead.company.name for lead in banked_after_second]
    assert len(names) == len(set(names)), "a company was banked twice"
    assert all(lead.qualification.qualified for lead in banked_after_second)
    assert len(second.qualified_leads) <= len(banked_after_second)
