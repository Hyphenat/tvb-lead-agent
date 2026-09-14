"""Key diagnostics.

A key that is present but invalid is the worst of the three states: the health
panel reports the provider as available, every call then fails quietly behind
the fallback chain, and the run looks merely disappointing instead of
misconfigured. These checks exist to surface that before a run is spent.
"""

import httpx
import pytest
import respx

from tvb_agent.diagnostics import check_format, check_live, collect


@pytest.mark.parametrize("provider,key,expected", [
    # Google issues both shapes and both authenticate. This was originally
    # written to reject "AQ." keys; a live call proved that wrong, so the rule
    # was corrected rather than the evidence explained away.
    ("gemini", "AIzaSyD-abcdefghijklmnop", True),
    ("gemini", "AQ.Ab8RN6J_sxWkT_pxKGCkfjITMjxx6Zxp", True),
    ("gemini", "ya29.oauth-access-token", False),
    ("serper", "95f13351f27528a1c3d8fd40edac671fd64b9b5a", True),
    ("serper", "not-a-serper-key", False),
    ("zerobounce", "c967119f653f4653a8958ef4dc46824e", True),
    ("openai", "sk-proj-abc123", True),
    ("openai", "proj-abc123", False),
    ("groq", "gsk_abc123", True),
    ("anthropic", "sk-ant-abc123", True),
    ("tavily", "tvly-abc123", True),
])
def test_format_rules(provider, key, expected):
    ok, note = check_format(provider, key)
    assert ok is expected
    if not expected:
        assert note, "a rejected key must explain why"


def test_unknown_provider_and_missing_key_are_not_judged():
    assert check_format("abstract", "anything")[0] is None
    assert check_format("gemini", None)[0] is None
    assert check_format("gemini", "")[0] is None


def test_collect_reports_every_provider(settings):
    settings.serper_api_key = "95f13351f27528a1c3d8fd40edac671fd64b9b5a"
    rows = collect(settings)
    providers = {r.provider for r in rows}
    assert {"serper", "tavily", "brave", "gemini", "openai", "groq",
            "anthropic", "zerobounce", "hunter", "abstract"} <= providers
    serper = next(r for r in rows if r.provider == "serper")
    assert serper.configured and serper.format_ok is True


def test_verdicts_read_plainly(settings):
    settings.gemini_api_key = "ya29.wrong-kind-of-credential"
    row = next(r for r in collect(settings) if r.provider == "gemini")
    assert row.verdict == "wrong format"

    settings.gemini_api_key = None
    row = next(r for r in collect(settings) if r.provider == "gemini")
    assert row.verdict == "not set"


@pytest.mark.asyncio
@respx.mock
async def test_live_check_reports_a_working_key(settings):
    settings.serper_api_key = "95f13351f27528a1c3d8fd40edac671fd64b9b5a"
    respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(200, json={"organic": []}))
    rows = await check_live(settings)
    serper = next(r for r in rows if r.provider == "serper")
    assert serper.live_ok is True
    assert serper.verdict == "working"


@pytest.mark.asyncio
@respx.mock
async def test_live_check_reports_a_rejected_key(settings):
    settings.gemini_api_key = "AIzaSyD-looks-right-but-revoked"
    respx.post(url__regex=r"https://generativelanguage\.googleapis\.com/.*").mock(
        return_value=httpx.Response(400, json={"error": {"message": "API key not valid"}}))
    rows = await check_live(settings)
    gemini = next(r for r in rows if r.provider == "gemini")
    assert gemini.live_ok is False
    assert gemini.verdict == "FAILING"
    assert "API key not valid" in gemini.live_note


@pytest.mark.asyncio
@respx.mock
async def test_zerobounce_credit_balance_is_reported(settings):
    settings.zerobounce_api_key = "c967119f653f4653a8958ef4dc46824e"
    respx.get(url__regex=r"https://api\.zerobounce\.net/v2/getcredits.*").mock(
        return_value=httpx.Response(200, json={"Credits": "97"}))
    rows = await check_live(settings)
    zb = next(r for r in rows if r.provider == "zerobounce")
    assert zb.live_ok is True and "97" in zb.live_note


@pytest.mark.asyncio
@respx.mock
async def test_zerobounce_reports_minus_one_as_a_rejected_key(settings):
    """ZeroBounce answers 200 with Credits: -1 for a bad key."""
    settings.zerobounce_api_key = "c967119f653f4653a8958ef4dc46824e"
    respx.get(url__regex=r"https://api\.zerobounce\.net/v2/getcredits.*").mock(
        return_value=httpx.Response(200, json={"Credits": "-1"}))
    rows = await check_live(settings)
    zb = next(r for r in rows if r.provider == "zerobounce")
    assert zb.live_ok is False


@pytest.mark.asyncio
@respx.mock
async def test_unconfigured_providers_are_never_called(settings):
    route = respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(200, json={}))
    rows = await check_live(settings)   # nothing configured in the fixture
    assert route.call_count == 0
    assert all(r.live_ok is None for r in rows)


@pytest.mark.asyncio
@respx.mock
async def test_network_failure_is_reported_not_raised(settings):
    settings.serper_api_key = "95f13351f27528a1c3d8fd40edac671fd64b9b5a"
    respx.post("https://google.serper.dev/search").mock(side_effect=httpx.ConnectError("down"))
    rows = await check_live(settings)
    serper = next(r for r in rows if r.provider == "serper")
    assert serper.live_ok is False
    assert "network error" in serper.live_note


def test_preflight_blocks_a_run_with_no_search_provider(settings):
    """Starting a run that cannot possibly find anything wastes 15 minutes."""
    from tvb_agent.cli import _preflight

    settings.serper_api_key = None
    settings.tavily_api_key = None
    settings.brave_api_key = None
    settings.google_cse_key = None
    assert _preflight(settings) is False

    settings.serper_api_key = "95f13351f27528a1c3d8fd40edac671fd64b9b5a"
    assert _preflight(settings) is True


@pytest.mark.asyncio
@respx.mock
async def test_a_working_key_is_never_reported_as_the_wrong_format(settings):
    """Format rules are heuristics; a successful call is proof, and proof wins.

    Reported the other way round, a correct key gets flagged as broken and the
    user goes looking for a problem that does not exist.
    """
    settings.gemini_api_key = "ya29.unusual-but-apparently-valid"
    respx.post(url__regex=r"https://generativelanguage\.googleapis\.com/.*").mock(
        return_value=httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}))
    rows = await check_live(settings)
    gemini = next(r for r in rows if r.provider == "gemini")
    assert gemini.format_ok is False
    assert gemini.live_ok is True
    assert gemini.verdict == "working"


@pytest.mark.asyncio
@respx.mock
async def test_live_check_falls_through_a_retired_model(settings):
    """Google retires model names and answers 404 naming the replacement."""
    settings.gemini_api_key = "AIzaSyD-abcdefghijklmnop"
    settings.gemini_model = "gemini-1.0-retired"

    def respond(request):
        if "gemini-1.0-retired" in str(request.url):
            return httpx.Response(404, json={"error": {"message": "model is no longer available"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    respx.post(url__regex=r"https://generativelanguage\.googleapis\.com/.*").mock(side_effect=respond)
    rows = await check_live(settings)
    gemini = next(r for r in rows if r.provider == "gemini")
    assert gemini.live_ok is True
    assert "retired" in gemini.live_note


@pytest.mark.asyncio
@respx.mock
async def test_low_email_credits_are_called_out(settings):
    settings.zerobounce_api_key = "c967119f653f4653a8958ef4dc46824e"
    respx.get(url__regex=r"https://api\.zerobounce\.net/v2/getcredits.*").mock(
        return_value=httpx.Response(200, json={"Credits": "5"}))
    rows = await check_live(settings)
    zb = next(r for r in rows if r.provider == "zerobounce")
    assert zb.live_ok is True
    assert "LOW" in zb.live_note


def test_the_settings_fixture_leaks_no_real_credential(settings):
    """Two tests once passed on a machine with no keys and failed on a machine
    with them, because the fixture blanked credentials by name and a new
    provider had been added since. A test that depends on whose machine it runs
    on is not a test."""
    leaked = [name for name, value in vars(settings).items()
              if name.endswith(("_api_key", "_cse_key", "_cse_cx")) and value]
    assert not leaked, f"the developer's own keys reached the tests: {leaked}"
