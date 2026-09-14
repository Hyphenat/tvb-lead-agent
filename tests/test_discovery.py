"""Discovery: the search space, the frontier, dedup, and candidate extraction."""

import pytest

from tvb_agent.discovery.candidate_extractor import (
    Candidate,
    candidates_from_article,
    candidates_from_link_page,
    candidates_from_search,
    clean_company_name,
    is_probable_company_host,
    looks_like_link_hub,
)
from tvb_agent.discovery.frontier import Frontier
from tvb_agent.discovery.query_planner import (
    GEOGRAPHIES,
    PHRASINGS,
    SECTORS,
    SOURCE_SHAPES,
    WINDOWS,
    QueryPlanner,
)
from tvb_agent.providers.base import SearchResult
from tvb_agent.storage import Store, normalise_domain, normalise_name


# ----------------------------------------------------------------- planner --
def test_search_space_is_large_enough_to_keep_finding_new_ground():
    size = len(SECTORS) * len(GEOGRAPHIES) * len(PHRASINGS) * len(WINDOWS) * len(SOURCE_SHAPES)
    assert size > 50_000, f"cell space only {size}"


def test_planner_prefers_unexplored_cells():
    planner = QueryPlanner(seed=1)
    first = planner.propose_cells(6)
    # Mark them heavily used, then re-plan with that history.
    stats = {c.key: {"times_used": 25, "candidates_found": 0, "qualified_found": 0,
                     "sector": c.sector, "geography": c.geography} for c in first}
    planner2 = QueryPlanner(cell_stats=stats, seed=1)
    second = planner2.propose_cells(6)
    assert {c.key for c in first} != {c.key for c in second}


def test_rendered_queries_are_not_repeated_verbatim():
    planner = QueryPlanner(seed=2)
    cell = planner.propose_cells(1)[0]
    first = planner.render_queries(cell, per_cell=2)
    planner.seen_queries.update(first)
    second = planner.render_queries(cell, per_cell=2)
    assert not (set(first) & set(second))


def test_queries_contain_no_duplicated_adjacent_words():
    planner = QueryPlanner(seed=3)
    for cell in planner.propose_cells(25):
        for q in planner.render_queries(cell, per_cell=2):
            words = [w.lower() for w in q.split()]
            assert all(a != b for a, b in zip(words, words[1:], strict=False)), q


def test_targeted_queries_cover_every_gate_we_need_evidence_for():
    probes = QueryPlanner().targeted_queries("Acme", "acme.io")
    assert {"funding", "founder", "email", "location", "us_presence"} <= set(probes)
    assert all(probes[k] for k in probes)


# ---------------------------------------------------------------- frontier --
def test_frontier_merges_name_only_and_domain_bearing_candidates():
    f = Frontier()
    assert f.add(Candidate("Zeta Care", domain=None, discovered_via=["https://news/a"]))
    assert not f.add(Candidate("Zeta Care", domain="zetacare.in", discovered_via=["https://vc/p"]))
    assert f.pending == 1
    merged = f.pop_batch(1)[0]
    assert merged.domain == "zetacare.in"
    assert len(merged.discovered_via) == 2


def test_frontier_skips_companies_from_previous_runs():
    f = Frontier(known_keys={"zetacare.in"})
    assert not f.add(Candidate("Zeta Care", domain="zetacare.in"))
    assert f.skipped_known == 1


def test_frontier_does_not_reissue_processed_candidates():
    f = Frontier()
    f.add(Candidate("Acme", domain="acme.io"))
    f.pop_batch(1)
    assert not f.add(Candidate("Acme", domain="acme.io"))
    assert f.pending == 0


def test_frontier_prioritises_corroborated_candidates():
    f = Frontier()
    f.add(Candidate("One", domain="one.io", discovered_via=["a"]))
    f.add(Candidate("Two", domain="two.io", discovered_via=["a", "b", "c"]))
    assert f.pop_batch(1)[0].name == "Two"


# -------------------------------------------------------------- extraction --
def test_headline_yields_the_company_not_the_publisher():
    res = [SearchResult("Acme Technologies raises $2.5M seed round",
                        "https://techcrunch.com/2026/acme", "Bengaluru-based Acme raised")]
    cands = candidates_from_search(res)
    assert [c.name for c in cands] == ["Acme Technologies"]
    assert cands[0].domain is None, "a press host must never be taken as the company domain"


def test_company_site_result_becomes_a_candidate_with_its_domain():
    res = [SearchResult("Zeta Health | Care platform", "https://zetahealth.io/", "We build")]
    cands = candidates_from_search(res)
    assert cands[0].domain == "zetahealth.io"


def test_social_and_aggregator_results_are_not_candidates():
    res = [SearchResult("John Doe", "https://linkedin.com/in/johndoe", "profile"),
           SearchResult("Acme", "https://crunchbase.com/organization/acme", "profile")]
    assert candidates_from_search(res) == [] or all(c.domain is None for c in candidates_from_search(res))


def test_link_hub_expansion_returns_portfolio_companies():
    links = [("https://acme.io/", "Acme"), ("https://zeta.co/", "Zeta Labs"),
             ("https://techcrunch.com/x", "Press"), ("https://vc.com/team", "Team"),
             ("https://deep.com/a/b/c", "Deep link")]
    cands = candidates_from_link_page(links, "https://vc.com/portfolio")
    names = {c.name for c in cands}
    assert names == {"Acme", "Zeta Labs"}


def test_article_mining_finds_secondary_companies():
    text = ("Bengaluru-based Acme Technologies, a SaaS platform, announced today.\n"
            "Zeta Labs raises $3 million in seed funding led by Foo.\n"
            "The startup raised more money and the company raises questions.")
    names = {c.name for c in candidates_from_article(text, "https://news/a")}
    assert "Acme Technologies" in names and "Zeta Labs" in names
    assert "The startup" not in names


@pytest.mark.parametrize("raw", ["The", "startup", "Top 10 startups", "million funding round", ""])
def test_junk_names_are_rejected(raw):
    assert clean_company_name(raw) is None


def test_legal_suffixes_are_stripped():
    assert clean_company_name("Acme Technologies Pvt. Ltd.") == "Acme Technologies"


def test_link_hub_detection():
    assert looks_like_link_hub("https://vc.com/portfolio", "Our Portfolio", "companies we back")
    assert looks_like_link_hub("https://x.com/blog", "Top 20 startups to watch", "")
    assert not looks_like_link_hub("https://acme.io/", "Acme - platform", "we build software")


def test_non_company_hosts_are_recognised():
    assert not is_probable_company_host("https://linkedin.com/company/x")
    assert not is_probable_company_host("https://techcrunch.com/x")
    assert not is_probable_company_host("https://mca.gov.in/x")
    assert is_probable_company_host("https://acme.io/")


# ------------------------------------------------------------------ dedup --
@pytest.mark.parametrize("a,b", [
    ("Acme Technologies Pvt. Ltd.", "Acme Technologies"),
    ("ACME Technologies Private Limited", "Acme Technologies"),
    ("Zeta Labs GmbH", "Zeta Labs"),
    ("The Venture Build Ltd", "Venture Build"),
])
def test_name_normalisation_collapses_legal_variants(a, b):
    assert normalise_name(a) == normalise_name(b)


@pytest.mark.parametrize("a,b", [
    ("Acme Labs", "Acme Software"),
    ("Zeta Health", "Zeta Pay"),
    ("Nova Systems", "Nova Robotics"),
])
def test_name_normalisation_keeps_different_companies_apart(a, b):
    """Over-eager folding would silently merge two unrelated businesses."""
    assert normalise_name(a) != normalise_name(b)


def test_frontier_keeps_same_named_companies_on_different_domains_apart():
    f = Frontier()
    assert f.add(Candidate("Acme", domain="acme.io"))
    assert f.add(Candidate("Acme", domain="acme-robotics.com"))
    assert f.pending == 2


def test_domain_normalisation():
    assert normalise_domain("https://WWW.Acme.co.uk/about?x=1") == "acme.co.uk"


def test_store_finds_a_company_by_an_alternative_name(tmp_path):
    from tvb_agent.models import CompanyProfile

    store = Store(tmp_path / "d.sqlite3")
    store.start_run("r1", {})
    store.upsert_company(CompanyProfile.new("Acme Technologies Pvt. Ltd.", "acme.io"), "r1")
    assert store.find_company("Acme Technologies", None)
    assert store.find_company("anything", "acme.io")
    assert store.find_company("Unrelated Co", None) is None
    store.close()


# --------------------------------------------------------------------------- #
# Headline shapes taken from how funding coverage is actually written. An
# extractor tuned only on "Acme raises $2M" collapses on real titles, where the
# company name almost always sits behind a nationality, a sector word or a
# city qualifier.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("title,expected", [
    ("Indian fintech Jar raises $22.6M in Series B led by Tiger Global", "Jar"),
    ("Lagos-based Moniepoint raises $110m Series C", "Moniepoint"),
    ("Berlin's Pliant raises EUR 18M Series A", "Pliant"),
    ("Zetwerk secures $120 million in fresh funding", "Zetwerk"),
    ("Nigerian healthtech Famasi Africa bags $1.5m seed", "Famasi Africa"),
    ("Dutch startup Cradle raises $24M to design proteins", "Cradle"),
    ("Kenya's Turaco closes $10m Series A round", "Turaco"),
    ("Singapore-based Nium announces new funding round", "Nium"),
    ("French AI startup Dust nets EUR 5M in seed funding", "Dust"),
    ("Warsaw-based Vue Storefront raises $20M", "Vue Storefront"),
    ("Exclusive: Chennai's Agnikul Cosmos secures funding", "Agnikul Cosmos"),
    ("Swedish proptech company Hemnet lands EUR 4M", "Hemnet"),
    ("Breaking: UAE-based Tabby raises $200M Series D", "Tabby"),
    ("iZettle raises new round", "iZettle"),
])
def test_company_is_recovered_from_real_headline_shapes(title, expected):
    from tvb_agent.discovery.candidate_extractor import company_from_headline

    assert company_from_headline(title) == expected


@pytest.mark.parametrize("title", [
    "Meet the 10 startups in Techstars London's 2026 class",
    "Top 20 fintech startups to watch in 2026",
    "How to raise a seed round in Europe",
    "The state of European venture in 2026",
])
def test_listicles_and_explainers_yield_no_company(title):
    from tvb_agent.discovery.candidate_extractor import company_from_headline

    assert company_from_headline(title) is None


def test_publisher_is_never_taken_as_the_company_from_a_headline():
    res = [SearchResult("Indian fintech Jar raises $22.6M",
                        "https://techcrunch.com/2026/jar", "Bengaluru-based Jar raised")]
    cands = candidates_from_search(res)
    assert cands and cands[0].name == "Jar"
    assert cands[0].domain is None


# --------------------------------------------------------------------------- #
# Lessons from the first real run. Link expansion pulled in ~60 "companies" that
# were venture funds, blogs and directories, because an investor list looks
# identical to a portfolio page under a keyword test.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url,title,text", [
    ("https://shizune.co/investors", "Top investors in Portugal",
     "A list of investors and VC firms for founders raising a seed round"),
    ("https://investorhunt.co/countries/spain", "Investor directory",
     "Find investors and angel investors by country"),
    ("https://x.com/vcs", "VC firms investing in fintech", "active investors and funds investing"),
])
def test_investor_directories_are_not_mined_for_companies(url, title, text):
    from tvb_agent.discovery.candidate_extractor import looks_like_investor_directory

    assert looks_like_investor_directory(url, title, text)
    assert not looks_like_link_hub(url, title, text)


@pytest.mark.parametrize("url,title,text", [
    ("https://p72.vc/portfolio", "Our Portfolio", "the companies we back"),
    ("https://acc.example/batch", "Our 2026 cohort", "startups in this batch"),
])
def test_genuine_company_lists_are_still_mined(url, title, text):
    assert looks_like_link_hub(url, title, text)


@pytest.mark.parametrize("raw", [
    "Fintech", "Openvc", "Eif", "Mexicobusiness", "Failory", "Shizune",
    "Fintech Ventures", "Startups", "Capital Partners", "EIF",
])
def test_categories_publishers_and_acronyms_are_not_companies(raw):
    """Every one of these was produced as a 'company' by a real run."""
    assert clean_company_name(raw) is None


@pytest.mark.parametrize("raw", ["Zeta Care", "Moniepoint", "Vue Storefront", "iZettle", "Jar"])
def test_real_company_names_still_survive(raw):
    assert clean_company_name(raw) == raw


def test_a_publisher_is_never_adopted_as_the_company_website():
    """The false positive this whole guard exists to prevent.

    A real run produced a "qualified lead" for QuoteMachine whose website was
    retail-insider.com - the trade magazine that covered it - and whose
    "founder" was a journalist who writes for that magazine. Every field was a
    real fact about a real organisation; they were simply two different
    organisations.
    """
    res = [SearchResult("QuoteMachine raises CAD 1.8M seed round",
                        "https://retail-insider.com/2026/quotemachine-seed",
                        "Montreal-based retail SaaS QuoteMachine has raised")]
    cands = candidates_from_search(res)
    assert cands and cands[0].name == "QuoteMachine"
    assert cands[0].domain is None, "the publisher must never become the company's domain"


def test_headline_candidates_are_researched_before_directory_scrapings():
    """A real run spent most of its budget on directory entries while genuine
    startups named in funding headlines waited behind them."""
    f = Frontier()
    f.add(Candidate("DirectoryEntry", domain="a.io", source_kind="link_hub"))
    f.add(Candidate("SiteHit", domain="b.io", source_kind="company_site"))
    f.add(Candidate("HeadlineCo", domain=None, source_kind="headline"))
    assert [c.name for c in f.pop_batch(3)] == ["HeadlineCo", "SiteHit", "DirectoryEntry"]


def test_merging_keeps_the_strongest_provenance():
    f = Frontier()
    f.add(Candidate("Zeta Care", domain=None, source_kind="headline"))
    f.add(Candidate("Zeta Care", domain="zetacare.in", source_kind="link_hub"))
    merged = f.pop_batch(1)[0]
    assert merged.domain == "zetacare.in"
    assert merged.source_kind == "headline"


@pytest.mark.parametrize("host", [
    "rocketreach.co", "parsers.vc", "clutch.co", "vestbee.com", "startupgenome.com",
    "retail-insider.com", "theglobeandmail.com", "ziprecruiter.com", "craft.co",
])
def test_directories_media_and_contact_scrapers_are_not_companies(host):
    """Every one of these consumed research budget in a real run."""
    assert not is_probable_company_host(f"https://{host}/something")


def test_link_pages_do_not_invent_names_from_domains():
    """Navigation and partner links became "companies" in a real run.

    "Estvca", "Seedblink", "Join" and "Ebooks" were all produced by deriving a
    name from the domain when the link had no usable label.
    """
    links = [
        ("https://acme.io/", "Acme Health"),      # a real portfolio entry
        ("https://seedblink.com/", ""),           # unlabelled - a partner link
        ("https://join.com/", "Join"),            # generic word, not a name
        ("https://estvca.ee/", "   "),            # whitespace only
    ]
    names = {c.name for c in candidates_from_link_page(links, "https://vc.com/portfolio")}
    assert names == {"Acme Health"}


def test_logo_alt_text_is_used_as_the_company_name():
    """Portfolio pages are grids of logos; the name lives in the alt attribute."""
    from tvb_agent.providers.fetcher import extract_links

    html = ('<a href="https://acme.io"><img alt="Acme Health"/></a>'
            '<a href="https://zeta.co"><img title="Zeta Labs"/></a>'
            '<a href="/about">About</a>')
    links = extract_links(html, "https://vc.com/portfolio")
    assert ("https://acme.io", "Acme Health") in links
    assert ("https://zeta.co", "Zeta Labs") in links


@pytest.mark.parametrize("host", ["ebooks.tau.edu.ng", "mit.edu", "x.gov.uk", "dept.ac.in"])
def test_academic_and_government_hosts_are_not_companies(host):
    """A fixed suffix list missed ebooks.tau.edu.ng in a real run."""
    assert not is_probable_company_host(f"https://{host}/page")


# --------------------------------------------------------------------------- #
# The name filter has to cut hard enough to keep directories and buttons out,
# without cutting so hard that real companies disappear. Both directions are
# asserted here because tightening one has repeatedly broken the other.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", [
    "New Relic", "Next Insurance", "Top Hat", "One Medical", "Up Bank",
    "Acme Health", "Zeta Labs", "Moniepoint", "Vue Storefront", "iZettle",
    "Jar", "Famasi Africa", "Agnikul Cosmos", "Helios Grid", "Nova Pay",
])
def test_real_company_names_survive_the_filter(name):
    assert clean_company_name(name) == name


@pytest.mark.parametrize("label", [
    "Join", "Apply Now", "Read more", "Learn more", "Get Started", "View all",
    "Our Team", "Subscribe", "Download", "Pricing", "Log in",
])
def test_navigation_labels_are_rejected(label):
    """A link reading "Join" is a sign-up button. One became a "company"."""
    assert clean_company_name(label) is None


# --------------------------------------------------------------------------- #
# Names that are not companies
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", [
    "Tel Aviv Israel", "Bengaluru", "Lagos Nigeria",   # datelines, not companies
    "Sponsor Us", "Consultancy", "Advertise", "Directory", "Awards",
])
def test_places_and_section_labels_are_not_companies(name):
    from tvb_agent.discovery.candidate_extractor import is_generic_name

    assert is_generic_name(name), f"{name!r} should not reach research"


@pytest.mark.parametrize("name", [
    "New Relic", "Zeta Care", "Amartha", "Stockbit", "Chickin", "Wonderful",
    "Nova Pay", "Helios Grid",
])
def test_real_company_names_survive_the_place_and_label_filters(name):
    from tvb_agent.discovery.candidate_extractor import is_generic_name

    assert not is_generic_name(name), f"{name!r} is a real company"


def test_single_word_link_labels_must_match_the_site_they_point_at():
    """Run 7 researched "Assets", "Blocks" and "Conduct" as though they were
    companies. A portfolio entry names the site it links to; a sidebar label
    does not."""
    from tvb_agent.discovery.candidate_extractor import candidates_from_link_page

    links = [
        ("https://chickin.co.id/", "Chickin"),        # real entry
        ("https://www.stockbit.com/", "Stockbit"),    # real entry
        ("https://zetacare.io/", "Zeta Care"),        # two words: passes anyway
        ("https://assets.example.com/x", "Assets"),   # sidebar label
        ("https://somesite.org/", "Conduct"),         # sidebar label
    ]
    names = {c.name for c in candidates_from_link_page(links, "https://hub.example/portfolio")}
    assert names == {"Chickin", "Stockbit", "Zeta Care"}


def test_in_band_amount_queries_use_a_currency_the_market_reports_in():
    from tvb_agent.discovery.query_planner import amount_phrases_for

    assert not any("£" in p for p in amount_phrases_for("korea"))
    assert not any("€" in p for p in amount_phrases_for("indonesia"))
    assert any("€" in p for p in amount_phrases_for("germany"))
    assert any("£" in p for p in amount_phrases_for("uk"))


def test_no_single_source_shape_takes_over_a_run():
    from collections import Counter

    from tvb_agent.discovery.query_planner import QueryPlanner

    cells = QueryPlanner(seed=7).propose_cells(10)
    top = Counter(c.source_shape for c in cells).most_common(1)[0][1]
    assert top <= 5, "one shape monopolised the run; the agent stops finding new sources"


# --------------------------------------------------------------------------- #
# The headline that found a company is evidence about it
# --------------------------------------------------------------------------- #
def test_the_discovery_headline_becomes_quotable_evidence():
    """Run 7 discarded every discovery snippet and then rejected the company
    for having no verifiable funding - while the figure sat in the headline
    that had surfaced it."""
    import asyncio

    from tvb_agent.agent import LeadAgent
    from tvb_agent.config import Settings
    from tvb_agent.discovery.candidate_extractor import Candidate
    from tvb_agent.research.extractor import GroundedExtractor
    from tvb_agent.validation.validators import SourceText, validate_country, validate_funding

    candidate = Candidate(
        name="Flowt", domain=None,
        discovered_via=["https://disruptafrica.com/2026/08/26/kenyan-ai-startup-flowt-raises-pre-seed/"],
        hint_snippet="Kenyan AI startup Flowt raises pre-seed funding round. Flowt, a "
                     "Nairobi-based workflow automation platform, has raised $2 million in a "
                     "pre-seed round. Founder Alice Wanjiru said the platform serves 300 businesses.",
    )
    source = LeadAgent._discovery_excerpt(candidate, None)
    assert source is not None

    result = asyncio.run(GroundedExtractor(None).extract("Flowt", [source]))
    texts = [SourceText(url=source.url, text=source.text, authority=source.authority)]
    funding = validate_funding(result, Settings(), texts, "Flowt")
    assert funding.value is not None and funding.value.amount_usd == 2_000_000
    assert validate_country(result, None, texts, "Flowt").value == "Kenya"
    assert "Alice Wanjiru" in [str(c.value) for c in result.by_field("founder")]


@pytest.mark.parametrize("url,snippet,reason", [
    ("https://random-shop.example/products/hat", "Acme raises $2 million seed.", "no standing"),
    ("https://techcrunch.com/2026/01/01/other-co-raises/", "OtherCo raises $2 million.", "not about Acme"),
])
def test_a_discovery_snippet_without_standing_or_attribution_is_not_evidence(url, snippet, reason):
    from tvb_agent.agent import LeadAgent
    from tvb_agent.discovery.candidate_extractor import Candidate

    candidate = Candidate(name="Acme", domain=None, discovered_via=[url], hint_snippet=snippet)
    assert LeadAgent._discovery_excerpt(candidate, None) is None, reason


def test_the_headline_travels_with_the_candidate_not_just_the_snippet():
    """The title carries the company name and the amount; the snippet often
    never repeats the name. Keeping only the snippet meant the excerpt failed
    its own attribution check, and run 9 researched 293 candidates with nothing
    readable attached to them."""
    from tvb_agent.agent import LeadAgent
    from tvb_agent.discovery.candidate_extractor import candidates_from_search
    from tvb_agent.providers.base import SearchResult

    results = [SearchResult(
        title="Flowt raises $2 million seed round",
        url="https://disruptafrica.com/2026/08/26/flowt-raises-pre-seed-round/",
        snippet="The Nairobi-based workflow automation platform will use the round to hire.",
    )]
    candidate = next(c for c in candidates_from_search(results) if c.name == "Flowt")

    assert "Flowt raises $2 million" in candidate.hint_snippet
    assert "Nairobi-based" in candidate.hint_snippet
    # And because the name is now in the passage, it survives attribution.
    assert LeadAgent._discovery_excerpt(candidate, None) is not None


# --------------------------------------------------------------------------- #
# Reading the headline before spending the budget
# --------------------------------------------------------------------------- #
def test_a_headline_that_rules_a_company_out_stops_it_becoming_research():
    """Run 12 researched 80 companies and only 5 cleared the four non-email
    gates - while the headlines that found them had already said which ones
    could. A company whose round is far outside the band never becomes research."""
    from tvb_agent.discovery.candidate_extractor import Candidate
    from tvb_agent.discovery.frontier import Frontier

    frontier = Frontier()
    accepted = frontier.add(Candidate(
        name="Flowt", source_kind="headline",
        hint_snippet="Kenyan AI startup Flowt raises pre-seed round. Flowt, a Nairobi-based "
                     "workflow automation platform, has raised $2 million. Founder Alice "
                     "Wanjiru said the platform serves 300 businesses."))
    refused = frontier.add(Candidate(
        name="Amartha", source_kind="headline",
        hint_snippet="Indonesian fintech Amartha raises $27.5 million Series C led by Norfund."))
    us = frontier.add(Candidate(
        name="USCo", source_kind="headline",
        hint_snippet="San Francisco-based USCo raises $3 million seed round."))

    assert accepted and not refused and not us
    assert frontier.pending == 1
    assert frontier.ruled_out == 2


def test_the_best_evidenced_candidate_is_researched_first():
    from tvb_agent.discovery.candidate_extractor import Candidate
    from tvb_agent.discovery.frontier import Frontier

    frontier = Frontier()
    frontier.add(Candidate(name="Thin", source_kind="headline",
                           hint_snippet="Thin announces a new product."))
    frontier.add(Candidate(name="Rich", source_kind="headline",
                           hint_snippet="Nairobi-based Rich raises $2 million seed. "
                                        "Founder Alice Wanjiru said the platform is growing."))
    frontier.add(Candidate(name="Middling", source_kind="headline",
                           hint_snippet="Berlin-based Middling raises $3 million seed."))

    assert [c.name for c in frontier.pop_batch(3)] == ["Rich", "Middling", "Thin"]


@pytest.mark.parametrize("text,expected_type", [
    # An investor's name containing "fund" silently reclassified a real $27.5M
    # round as a VC vehicle, which hid it from the band check entirely.
    ("Amartha raises $27.5 million Series C led by Norfund.", True),
    ("Flowt raises $2 million in seed funding led by Sequoia Fund.", True),
    # Actual fund raises are still not company funding.
    ("Acme Ventures closes $50 million maiden fund to back African startups.", False),
    ("The firm closed its fund at $120 million.", False),
])
def test_an_investors_name_is_not_a_fund_size(text, expected_type):
    from tvb_agent.validation.money import parse_amounts

    amounts = parse_amounts(text)
    assert amounts
    assert amounts[0].qualifies_as_criterion_input is expected_type
