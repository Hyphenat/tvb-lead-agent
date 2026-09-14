"""Provider diagnostics.

Two levels of check:

* a **format** check, which is instant and free and catches the commonest
  mistake - pasting the wrong kind of credential entirely;
* a **live** check, which spends one call (or a free balance endpoint where the
  provider offers one) and is the only thing that actually proves a key works.

This exists because a key that is present but invalid is worse than a key that
is absent: the health panel reports the provider as configured, every call fails
quietly behind the fallback chain, and the run looks merely disappointing rather
than misconfigured.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass

import httpx

from .config import Settings


@dataclass
class KeyCheck:
    provider: str
    capability: str
    configured: bool
    format_ok: bool | None = None      # None when there is no known format
    format_note: str = ""
    live_ok: bool | None = None        # None when not checked
    live_note: str = ""

    @property
    def verdict(self) -> str:
        if not self.configured:
            return "not set"
        if self.live_ok is True:
            return "working"   # a successful call outranks any format heuristic
        if self.live_ok is False:
            return "FAILING"
        if self.format_ok is False:
            return "wrong format"
        return "set (unverified)"


# Prefix / shape rules published by each provider.
_FORMATS: dict[str, tuple[re.Pattern, str]] = {
    "serper": (re.compile(r"^[0-9a-f]{40}$", re.I), "40 hexadecimal characters"),
    "tavily": (re.compile(r"^tvly-", re.I), "starts with 'tvly-'"),
    "brave": (re.compile(r"^BSA", re.I), "starts with 'BSA'"),
    # Google issues classic "AIza..." keys and newer "AQ..." credentials; both
    # authenticate. A format rule that only knew the older shape produced a
    # false alarm, which is why the live check outranks it below.
    "gemini": (re.compile(r"^(?:AIza[0-9A-Za-z_\-]{10,}|AQ\.[0-9A-Za-z_\-]{10,})$"),
               "starts with 'AIza' or 'AQ.'"),
    "openai": (re.compile(r"^sk-"), "starts with 'sk-'"),
    "groq": (re.compile(r"^gsk_"), "starts with 'gsk_'"),
    "anthropic": (re.compile(r"^sk-ant-"), "starts with 'sk-ant-'"),
    "zerobounce": (re.compile(r"^[0-9a-f]{32}$", re.I), "32 hexadecimal characters"),
    "hunter": (re.compile(r"^[0-9a-f]{40}$", re.I), "40 hexadecimal characters"),
}


def check_format(provider: str, key: str | None) -> tuple[bool | None, str]:
    if not key:
        return None, ""
    rule = _FORMATS.get(provider)
    if not rule:
        return None, ""
    pattern, description = rule
    if pattern.match(key.strip()):
        return True, ""
    return False, (f"does not look like a {provider} key ({description}) - "
                   f"this is usually a credential copied from the wrong place")


def collect(settings: Settings) -> list[KeyCheck]:
    rows: list[KeyCheck] = []

    def add(provider: str, capability: str, key: str | None) -> None:
        fmt_ok, note = check_format(provider, key)
        rows.append(KeyCheck(provider=provider, capability=capability,
                             configured=bool(key), format_ok=fmt_ok, format_note=note))

    add("serper", "search", settings.serper_api_key)
    add("tavily", "search", settings.tavily_api_key)
    add("brave", "search", settings.brave_api_key)
    add("gemini", "extraction", settings.gemini_api_key)
    add("openai", "extraction", settings.openai_api_key)
    add("groq", "extraction", settings.groq_api_key)
    add("anthropic", "extraction", settings.anthropic_api_key)
    add("zerobounce", "email", settings.zerobounce_api_key)
    add("hunter", "email", settings.hunter_api_key)
    add("abstract", "email", settings.abstract_api_key)
    add("apollo", "contacts", settings.apollo_api_key)
    return rows


async def check_live(settings: Settings, rows: list[KeyCheck] | None = None) -> list[KeyCheck]:
    """Actually call each configured provider once.

    Where a provider exposes a balance or account endpoint it is used, because
    it costs nothing; otherwise the cheapest real call is made.
    """
    rows = rows or collect(settings)
    by_provider = {r.provider: r for r in rows}

    async with httpx.AsyncClient(timeout=20.0) as client:
        async def run(provider: str, make_call) -> None:
            """``make_call`` is a factory, not a coroutine.

            Building every coroutine up front and abandoning the unconfigured
            ones leaves never-awaited coroutines behind - harmless here, but it
            is exactly the shape of bug that hides a real missed await.
            """
            row = by_provider.get(provider)
            if not row or not row.configured:
                return
            try:
                ok, note = await make_call()
                row.live_ok, row.live_note = ok, note
            except httpx.HTTPError as e:
                row.live_ok = False
                row.live_note = f"network error: {type(e).__name__}"
            except Exception as e:  # pragma: no cover - defensive
                row.live_ok = False
                row.live_note = f"{type(e).__name__}: {e}"

        async def serper():
            r = await client.post("https://google.serper.dev/search",
                                  headers={"X-API-KEY": settings.serper_api_key or ""},
                                  json={"q": "test", "num": 1})
            if r.status_code == 200:
                return True, "1 search credit spent"
            return False, _http_note(r.status_code)

        async def tavily():
            r = await client.post("https://api.tavily.com/search",
                                  json={"api_key": settings.tavily_api_key, "query": "test",
                                        "max_results": 1})
            return (True, "1 search credit spent") if r.status_code == 200 else (False, _http_note(r.status_code))

        async def brave():
            r = await client.get("https://api.search.brave.com/res/v1/web/search",
                                 headers={"X-Subscription-Token": settings.brave_api_key or "",
                                          "Accept": "application/json"},
                                 params={"q": "test", "count": 1})
            return (True, "1 query spent") if r.status_code == 200 else (False, _http_note(r.status_code))

        async def gemini():
            from .providers.llm import GeminiProvider

            models = [settings.gemini_model] + [
                m for m in GeminiProvider.FALLBACK_MODELS if m != settings.gemini_model]
            detail = ""
            for model in models:
                r = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    params={"key": settings.gemini_api_key},
                    json={"contents": [{"role": "user", "parts": [{"text": "Reply with: ok"}]}],
                          "generationConfig": {"maxOutputTokens": 5}})
                if r.status_code == 200:
                    note = f"model {model} responded"
                    if model != settings.gemini_model:
                        note += (f" (your configured model '{settings.gemini_model}' is retired; "
                                 f"the agent will use {model} automatically)")
                    return True, note
                if r.status_code != 404:
                    with contextlib.suppress(Exception):
                        detail = (r.json().get("error") or {}).get("message", "")[:300]
                    return False, f"{_http_note(r.status_code)}{' - ' + detail if detail else ''}"
                with contextlib.suppress(Exception):
                    detail = (r.json().get("error") or {}).get("message", "")[:200]
            return False, f"no available model (last: {detail})" if detail else "no available model"

        async def openai():
            r = await client.get("https://api.openai.com/v1/models",
                                 headers={"Authorization": f"Bearer {settings.openai_api_key}"})
            return (True, "no tokens spent") if r.status_code == 200 else (False, _http_note(r.status_code))

        async def groq():
            r = await client.get("https://api.groq.com/openai/v1/models",
                                 headers={"Authorization": f"Bearer {settings.groq_api_key}"})
            return (True, "no tokens spent") if r.status_code == 200 else (False, _http_note(r.status_code))

        async def zerobounce():
            r = await client.get("https://api.zerobounce.net/v2/getcredits",
                                 params={"api_key": settings.zerobounce_api_key})
            if r.status_code != 200:
                return False, _http_note(r.status_code)
            try:
                credits = int((r.json() or {}).get("Credits", -1))
            except Exception:
                credits = -1
            if credits < 0:
                return False, "key rejected (credits reported as -1)"
            note = f"{credits} verification credits remaining"
            if credits < 20:
                note += (" - LOW. Once these run out, emails are verified by authoritative-source "
                         "attribution plus MX only, which is recorded on each lead. Add a Hunter "
                         "key for 50 more free verifications a month.")
            return True, note

        async def hunter():
            r = await client.get("https://api.hunter.io/v2/account",
                                 params={"api_key": settings.hunter_api_key})
            if r.status_code != 200:
                return False, _http_note(r.status_code)
            data = (r.json() or {}).get("data") or {}
            requests = (data.get("requests") or {})
            bits = []
            for label, key in (("searches", "searches"), ("verifications", "verifications")):
                block = requests.get(key) or {}
                used, available = block.get("used"), block.get("available")
                if used is not None and available is not None:
                    bits.append(f"{max(available - used, 0)} {label} left this month")
            note = ", ".join(bits) or "no credits spent"
            if not bits:
                return True, note
            return True, (note + ". Searches are what the contact finder spends; "
                          "verifications are what the deliverability check spends.")

        async def apollo():
            """Ask about a public figure at a public domain - one credit at most.

            The point of the check is whether this plan releases addresses at
            all: Apollo answers HTTP 200 with a literal "email_not_unlocked@"
            placeholder when it will not, which looks like success until a run
            has spent an hour discovering otherwise.
            """
            r = await client.post(
                "https://api.apollo.io/api/v1/people/match",
                headers={"x-api-key": settings.apollo_api_key or "",
                         "Content-Type": "application/json", "accept": "application/json"},
                json={"first_name": "Tim", "last_name": "Cook", "domain": "apple.com"},
            )
            if r.status_code in (401, 403):
                return False, ("key rejected, or this plan does not allow API access. "
                               "Apollo requires an account registered with a work email "
                               "address for API email access.")
            if r.status_code == 429:
                return False, "HTTP 429 - rate limited; try again in a minute"
            if r.status_code >= 400:
                return False, _http_note(r.status_code)
            person = (r.json() or {}).get("person") or {}
            email = (person.get("email") or "")
            if email.startswith("email_not_unlocked"):
                return False, ("the key works but this plan will NOT release email addresses "
                               "through the API - enrichment will find nothing. A work-email "
                               "account or a paid plan is required.")
            if not email:
                return True, ("key accepted. No address for the test person, which is normal; "
                              "whether your plan releases addresses is still unproven.")
            return True, f"key accepted and addresses are released (status '{person.get('email_status')}')"

        for provider, make_call in (("serper", serper), ("tavily", tavily), ("brave", brave),
                                    ("apollo", apollo),
                                    ("gemini", gemini), ("openai", openai), ("groq", groq),
                                    ("zerobounce", zerobounce), ("hunter", hunter)):
            await run(provider, make_call)

    return rows


def _http_note(status: int) -> str:
    return {
        400: "HTTP 400 - request rejected (often a malformed key)",
        401: "HTTP 401 - key not accepted",
        403: "HTTP 403 - key rejected or quota exhausted",
        404: "HTTP 404 - endpoint or model not found",
        429: "HTTP 429 - rate limited (the key itself may be fine)",
    }.get(status, f"HTTP {status}")
