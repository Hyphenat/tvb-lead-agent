"""Failure injection: the run must degrade, never crash.

A lead agent that dies on the first 429 is useless in production, and a reviewer
clicking the button during a rate-limit window would see nothing at all.
"""

import asyncio
import re

import httpx
import pytest
import respx

from tvb_agent.providers.base import (
    Blocked,
    ParseFailed,
    ProviderChain,
    QuotaExhausted,
    RateLimited,
    with_retries,
)
from tvb_agent.providers.email_verify import EmailVerificationService
from tvb_agent.providers.fetcher import Fetcher
from tvb_agent.providers.llm import LLMService, _extract_json
from tvb_agent.providers.search import SearchService


@pytest.fixture
async def client():
    async with httpx.AsyncClient(timeout=5.0) as c:
        yield c


# ------------------------------------------------------------------ search --
@pytest.mark.asyncio
@respx.mock
async def test_search_falls_through_from_an_exhausted_provider(settings, client):
    settings.serper_api_key = "a"
    settings.tavily_api_key = "b"
    respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(403, json={"error": "quota"}))
    respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(200, json={"results": [
            {"title": "Acme", "url": "https://acme.io", "content": "x"}]}))

    svc = SearchService.build(client, settings)
    results = await svc.search("anything")
    assert [r.url for r in results] == ["https://acme.io"]


@pytest.mark.asyncio
@respx.mock
async def test_search_returns_empty_rather_than_raising_when_all_fail(settings, client):
    settings.serper_api_key = "a"
    respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(500, json={}))
    svc = SearchService.build(client, settings)
    assert await svc.search("anything") == []


@pytest.mark.asyncio
@respx.mock
async def test_rate_limited_search_is_retried_then_gives_up(settings, client):
    settings.serper_api_key = "a"
    route = respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(429, json={}))
    svc = SearchService.build(client, settings)
    assert await svc.search("anything") == []
    assert route.call_count >= 2, "a 429 should be retried before giving up"


# --------------------------------------------------------------------- llm --
@pytest.mark.asyncio
@respx.mock
async def test_llm_chain_ends_at_the_rule_based_terminator(settings, client):
    settings.gemini_api_key = "a"
    respx.post(re.compile(r"https://generativelanguage\.googleapis\.com/.*")).mock(
        return_value=httpx.Response(401, json={}))
    svc = LLMService.build(client, settings)
    assert await svc.complete_json("s", "u") is None, "must degrade to None, not raise"


@pytest.mark.asyncio
@respx.mock
async def test_llm_budget_is_enforced(settings, client):
    settings.gemini_api_key = "a"
    settings.budget.max_llm_calls = 2
    respx.post(re.compile(r"https://generativelanguage\.googleapis\.com/.*")).mock(
        return_value=httpx.Response(200, json={"candidates": [
            {"content": {"parts": [{"text": '{"ok": true}'}]}}]}))
    svc = LLMService.build(client, settings)
    for _ in range(5):
        await svc.complete_json("s", "u")
    assert svc.calls <= 2


def test_malformed_model_output_is_recovered_or_rejected_cleanly():
    assert _extract_json('```json\n{"a":1}\n```') == {"a": 1}
    assert _extract_json('Sure! {"a": 1} hope that helps') == {"a": 1}
    with pytest.raises(ParseFailed):
        _extract_json("no json here at all")


# ----------------------------------------------------------------- fetcher --
@pytest.mark.asyncio
@respx.mock
async def test_fetch_safe_never_raises(settings, store, client):
    respx.get("https://broken.example/").mock(side_effect=httpx.ConnectError("refused"))
    f = Fetcher(client, settings, store)
    page = await f.fetch_safe("https://broken.example/")
    assert not page.ok and page.status == 0


@pytest.mark.asyncio
@respx.mock
async def test_robots_disallow_is_respected(settings, store, client):
    settings.respect_robots = True
    respx.get("https://blocked.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /"))
    respx.get("https://blocked.example/page").mock(
        return_value=httpx.Response(200, text="<html><body>secret</body></html>"))
    f = Fetcher(client, settings, store)
    with pytest.raises(Blocked):
        await f.fetch("https://blocked.example/page")


@pytest.mark.asyncio
@respx.mock
async def test_non_html_content_is_not_parsed_as_text(settings, store, client):
    respx.get("https://x.example/f").mock(
        return_value=httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"}))
    f = Fetcher(client, settings, store)
    page = await f.fetch("https://x.example/f")
    assert page.text == ""


@pytest.mark.asyncio
@respx.mock
async def test_pages_are_served_from_cache_on_the_second_read(settings, store, client):
    settings.cache_ttl_seconds = 3600
    route = respx.get("https://c.example/p").mock(
        return_value=httpx.Response(200, text="<html><body>hello world</body></html>"))
    f = Fetcher(client, settings, store)
    await f.fetch("https://c.example/p")
    second = await f.fetch("https://c.example/p")
    assert second.from_cache and route.call_count == 1


# ------------------------------------------------------------------ email --
@pytest.mark.asyncio
async def test_email_verifier_without_providers_reports_unknown(settings):
    svc = EmailVerificationService([])
    res = await svc.deliverability("a@b.io")
    assert res.status == "unknown"
    assert not svc.has_deliverability_provider


@pytest.mark.asyncio
@respx.mock
async def test_email_verifier_falls_through_on_quota(settings, client):
    settings.zerobounce_api_key = "a"
    settings.hunter_api_key = "b"
    respx.get(re.compile(r"https://api\.zerobounce\.net/.*")).mock(
        return_value=httpx.Response(403, json={}))
    respx.get(re.compile(r"https://api\.hunter\.io/.*")).mock(
        return_value=httpx.Response(200, json={"data": {"status": "valid", "score": 95}}))
    svc = EmailVerificationService.build(client, settings)
    res = await svc.deliverability("a@b.io")
    assert res.status == "deliverable" and res.provider == "hunter"


@pytest.mark.asyncio
async def test_email_verification_budget_is_enforced(settings):
    from tvb_agent.providers.email_verify import (
        DeliverabilityResult,
        FixtureDeliverabilityProvider,
    )

    p = FixtureDeliverabilityProvider(default=DeliverabilityResult("deliverable", "fixture"))
    svc = EmailVerificationService([p], max_checks=2)
    for _ in range(5):
        await svc.deliverability("a@b.io")
    assert len(p.calls) <= 2


# ------------------------------------------------------------------ chain --
@pytest.mark.asyncio
async def test_with_retries_stops_on_non_retryable_errors():
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise QuotaExhausted("spent")

    with pytest.raises(QuotaExhausted):
        await with_retries(boom, attempts=3, base_delay=0.01)
    assert calls["n"] == 1, "a quota error must not be retried"


@pytest.mark.asyncio
async def test_with_retries_retries_transient_errors():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimited("slow down")
        return "ok"

    assert await with_retries(flaky, attempts=4, base_delay=0.01) == "ok"
    assert calls["n"] == 3


def test_provider_chain_disables_and_reports():
    class P:
        def __init__(self, name):
            self.name = name

    chain = ProviderChain(providers=[P("a"), P("b")])
    assert chain.active_name == "a"
    chain.disable("a")
    assert chain.active_name == "b"
    chain.disable("b")
    assert chain.active_name == "none"


# ----------------------------------------------------------------- budgets --
def test_discovery_gets_only_a_share_of_the_search_budget(settings):
    """Discovery must not consume the credits research needs for its probes."""
    settings.budget.max_searches = 100
    assert 0 < settings.budget.max_discovery_searches < 100
    # The split leaves the larger share for research: finding companies was
    # never the bottleneck, qualifying them is.
    expected = int(settings.budget.max_searches * settings.budget.discovery_search_share)
    assert settings.budget.max_discovery_searches == expected
    assert settings.budget.max_discovery_searches < settings.budget.max_searches / 2


def test_discovery_allowance_has_a_floor(settings):
    settings.budget.max_searches = 4
    assert settings.budget.max_discovery_searches >= 4


@pytest.mark.asyncio
@respx.mock
async def test_search_service_stops_at_its_ceiling(settings, client):
    settings.serper_api_key = "a"
    settings.budget.max_searches = 3
    route = respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(200, json={"organic": [
            {"title": "x", "link": "https://a.io", "snippet": "s"}]}))
    svc = SearchService.build(client, settings)
    for _ in range(10):
        await svc.search("q")
    assert route.call_count == 3
    assert svc.budget_exhausted


@pytest.mark.asyncio
@respx.mock
async def test_gemini_recovers_from_a_retired_model_name(settings, client):
    """A pinned model name that 404s must not disable extraction for a whole run."""
    from tvb_agent.providers.llm import GeminiProvider

    settings.gemini_api_key = "AIzaSyD-abcdefghijklmnop"
    settings.gemini_model = "gemini-1.0-retired"

    def respond(request):
        if "gemini-1.0-retired" in str(request.url):
            return httpx.Response(404, json={"error": {"message": "no longer available"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]})

    respx.post(url__regex=r"https://generativelanguage\.googleapis\.com/.*").mock(side_effect=respond)

    provider = GeminiProvider(client, settings)
    assert await provider.complete_json("s", "u") == {"ok": True}
    # The working model is remembered, so later calls skip the dead one.
    assert provider._model in GeminiProvider.FALLBACK_MODELS
    assert provider._candidates() == [provider._model]


@pytest.mark.asyncio
@respx.mock
async def test_an_unresponsive_host_is_abandoned(settings, store, client):
    """One dead host cost a real run about four minutes of timeouts."""
    route = respx.get(url__regex=r"https://dead\.example/.*").mock(
        side_effect=httpx.ConnectTimeout("timeout"))
    f = Fetcher(client, settings, store)
    f.host_failure_limit = 3

    for i in range(6):
        await f.fetch_safe(f"https://dead.example/page{i}")

    assert "dead.example" in f.hosts_abandoned
    assert route.call_count == 3, "should stop hitting the host once written off"


@pytest.mark.asyncio
@respx.mock
async def test_a_recovering_host_is_not_abandoned(settings, store, client):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] in (1, 2):
            raise httpx.ConnectTimeout("timeout")
        return httpx.Response(200, text="<html><body>ok content here</body></html>",
                              headers={"content-type": "text/html"})

    respx.get(url__regex=r"https://flaky\.example/.*").mock(side_effect=flaky)
    f = Fetcher(client, settings, store)
    f.host_failure_limit = 3

    await f.fetch_safe("https://flaky.example/a")
    await f.fetch_safe("https://flaky.example/b")
    await f.fetch_safe("https://flaky.example/c")     # succeeds, resets the count
    await f.fetch_safe("https://flaky.example/d")
    assert "flaky.example" not in f.hosts_abandoned


@pytest.mark.asyncio
async def test_llm_calls_are_paced_for_free_tier_limits():
    """Eight concurrent research workers would otherwise guarantee 429s."""
    import time

    from tvb_agent.providers.llm import FixtureLLMProvider

    svc = LLMService(ProviderChain(providers=[FixtureLLMProvider(by_marker={"x": {"ok": 1}})]),
                     max_rpm=60, concurrency=2)
    started = time.monotonic()
    await asyncio.gather(*[svc.complete_json("s", "x") for _ in range(4)])
    elapsed = time.monotonic() - started
    assert elapsed >= 2.5, f"calls were not paced ({elapsed:.2f}s for 4 at 60rpm)"


@pytest.mark.asyncio
@respx.mock
async def test_gemini_answer_is_read_past_the_reasoning_parts(settings, client):
    """Newer Gemini models return a thought part before the answer.

    Reading parts[0] blindly yields empty text, which surfaced in a real run as
    "LLM did not return parseable JSON" on every single call - extraction
    silently degraded to regexes for the whole run.
    """
    from tvb_agent.providers.llm import GeminiProvider

    settings.gemini_api_key = "AIzaSyD-abcdefghijklmnop"
    respx.post(url__regex=r"https://generativelanguage\.googleapis\.com/.*").mock(
        return_value=httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "Let me think about this...", "thought": True},
            {"text": '{"country": "India"}'},
        ]}}]}))
    provider = GeminiProvider(client, settings)
    assert await provider.complete_json("s", "u") == {"country": "India"}


@pytest.mark.asyncio
@respx.mock
async def test_an_empty_gemini_response_is_reported_clearly(settings, client):
    from tvb_agent.providers.llm import GeminiProvider

    settings.gemini_api_key = "AIzaSyD-abcdefghijklmnop"
    respx.post(url__regex=r"https://generativelanguage\.googleapis\.com/.*").mock(
        return_value=httpx.Response(200, json={"candidates": [
            {"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "", "thought": True}]}}]}))
    provider = GeminiProvider(client, settings)
    with pytest.raises(ParseFailed) as exc:
        await provider.complete_json("s", "u")
    assert "MAX_TOKENS" in str(exc.value)


@pytest.mark.asyncio
async def test_llm_pacing_widens_after_rate_limits():
    """Published per-minute limits vary by model and account, so the pace is a
    starting guess that must be corrected from what the provider actually says."""
    from tvb_agent.providers.llm import LLMProvider

    class AlwaysLimited(LLMProvider):
        name = "limited"
        is_model = True

        def __init__(self):
            pass

        async def complete_json(self, system, user, *, max_tokens=4096):
            raise RateLimited("slow down")

    svc = LLMService(ProviderChain(providers=[AlwaysLimited()]), max_rpm=600, concurrency=1)
    before = svc._min_interval
    for _ in range(3):
        await svc.complete_json("s", "u")
    assert svc.rate_limit_hits == 3
    assert svc._min_interval > before
    assert svc._min_interval <= svc._max_interval


@pytest.mark.asyncio
async def test_the_model_bows_out_once_its_tier_is_spent():
    """An optional component must never become the pipeline's bottleneck.

    A real run throttled itself to one model call every 30 seconds and spent 17
    minutes researching 16 companies. Past a point, waiting longer is strictly
    worse than continuing without the model.
    """
    from tvb_agent.providers.llm import LLMProvider

    class AlwaysLimited(LLMProvider):
        name = "limited"
        is_model = True

        def __init__(self):
            pass

        async def complete_json(self, system, user, *, max_tokens=4096):
            raise RateLimited("slow down")

    svc = LLMService(ProviderChain(providers=[AlwaysLimited()]), max_rpm=6000, concurrency=1)
    svc.give_up_after_rate_limits = 3
    for _ in range(5):
        await svc.complete_json("s", "u")

    assert svc.disabled_for_run
    assert svc.has_model is False, "extraction must fall back to the deterministic path"
    assert svc._min_interval <= svc._max_interval


@pytest.mark.asyncio
async def test_the_model_is_not_called_when_rules_already_answered():
    """Model calls are the scarce resource; rules are free."""
    from tvb_agent.models import SourceAuthority
    from tvb_agent.providers.llm import FixtureLLMProvider
    from tvb_agent.research.extractor import GroundedExtractor, Source

    text = ("Zeta Care is a care coordination platform for clinics across India.\n"
            "Zeta Care is headquartered in Bengaluru, India.\n"
            "Priya Raman\nCo-Founder & CEO\n"
            "Contact her at priya.raman@zetacare.in for partnership enquiries.")
    src = Source(url="https://zetacare.in/", text=text, authority=SourceAuthority.COMPANY_OWNED)

    provider = FixtureLLMProvider(responses=[{}])
    svc = LLMService(ProviderChain(providers=[provider]), max_rpm=6000)
    result = await GroundedExtractor(svc).extract("Zeta Care", [src])

    assert result.by_field("founder") and result.by_field("email")
    assert provider.calls == [], "the model should not be called when rules already answered"


# --------------------------------------------------------------------------- #
# Resolving a company's own site without spending a search credit
# --------------------------------------------------------------------------- #
def test_a_probed_domain_is_only_adopted_when_the_page_names_the_company():
    """The probe guesses an address; it does not guess an answer. A parked page,
    or a page about something else, is discarded."""
    from tvb_agent.agent import _looks_parked

    assert _looks_parked("This domain is for sale. Buy this domain today.")
    assert _looks_parked("coming soon")
    assert not _looks_parked("Flowt is a workflow automation platform for African "
                             "businesses. " * 6)


def test_the_probe_widens_to_the_market_the_headline_pointed_at():
    from tvb_agent.agent import _cctld_for

    assert _cctld_for("Kenyan AI startup Flowt raises pre-seed funding") == "co.ke"
    assert _cctld_for("The Jakarta-based company SPUN raises $1.8M") == "co.id"
    assert _cctld_for("a seed round was announced today") is None


# --------------------------------------------------------------------------- #
# Not guessing at pages the site has already pointed at
# --------------------------------------------------------------------------- #
def test_conventional_paths_are_only_guessed_where_the_site_is_silent():
    """Measured over a real run database: 3,568 of 8,015 fetches were 404s on
    exactly these guessed paths - 45% of all fetching spent on pages that were
    not there, on sites whose own navigation already linked the page."""
    import httpx
    import respx

    from tvb_agent.config import Settings
    from tvb_agent.providers.fetcher import Fetcher
    from tvb_agent.research.crawler import crawl_company_site

    home = ('<html><head><title>Acme</title></head><body>'
            '<a href="/about">About</a><a href="/our-people">Team</a>'
            '<a href="/get-in-touch">Contact</a>'
            '<p>Acme is a platform for teams.</p></body></html>')

    async def go():
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/robots.txt"):
                return httpx.Response(404)
            if url.rstrip("/") in ("https://acme.io", "https://acme.io/about",
                                   "https://acme.io/our-people", "https://acme.io/get-in-touch"):
                return httpx.Response(200, text=home, headers={"content-type": "text/html"})
            return httpx.Response(404, text="nope", headers={"content-type": "text/html"})

        with respx.mock:
            respx.route().mock(side_effect=handler)
            async with httpx.AsyncClient() as client:
                fetcher = Fetcher(client, Settings())
                await crawl_company_site(fetcher, "acme.io")
                return [str(c.request.url) for c in respx.calls]

    urls = asyncio.run(go())
    # The site linked its own team and contact pages, so those are not guessed.
    assert not any(u.endswith(("/team", "/our-team", "/contact", "/contact-us")) for u in urls), \
        "guessed at a page the site had already linked"
    # But an imprint is legally required and routinely unlinked, so it is still tried.
    assert any("impressum" in u for u in urls), "stopped looking for the imprint"
