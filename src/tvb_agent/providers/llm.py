"""LLM access for extraction only.

The model is never asked to *decide* anything - it is asked to *locate* facts in
text that has already been fetched, and to return the exact sentence it read
them in.  Everything it returns is then checked against the source text by
``research.extractor``; anything it invented is dropped.  That is why a weak
model, or no model at all, degrades quality without compromising integrity.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx

from ..config import Settings
from .base import (
    NotConfigured,
    ParseFailed,
    ProviderChain,
    ProviderError,
    QuotaExhausted,
    RateLimited,
    Timeout,
    with_retries,
)


def _extract_json(text: str) -> Any:
    """Models wrap JSON in prose or fences more often than they should."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        s, e = t.find(opener), t.rfind(closer)
        if s != -1 and e > s:
            try:
                return json.loads(t[s : e + 1])
            except json.JSONDecodeError:
                continue
    raise ParseFailed("LLM did not return parseable JSON")


class ModelNotFound(ProviderError):
    """The named model has been retired. Try the next one rather than giving up."""

    fall_through = False


def _raise_for_status(status: int, provider: str) -> None:
    if status == 429:
        raise RateLimited(f"{provider}: rate limited")
    if status in (401, 403):
        raise QuotaExhausted(f"{provider}: auth rejected ({status})")
    if status >= 500:
        raise Timeout(f"{provider}: upstream {status}")
    if status >= 400:
        raise ParseFailed(f"{provider}: HTTP {status}")


class LLMProvider:
    name = "base"
    is_model = True

    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client = client
        self.settings = settings

    async def complete_json(self, system: str, user: str, *, max_tokens: int = 4096) -> Any:  # pragma: no cover
        raise NotImplementedError


class GeminiProvider(LLMProvider):
    """Gemini, with automatic recovery from model retirement.

    Google retires model names on a schedule and answers 404 with a message
    naming the replacement.  A pinned name that silently 404s would disable
    extraction for an entire run, so a 404 falls through to the next known name
    and the working one is remembered for the rest of the run.
    """

    name = "gemini"

    # Newest first. The configured model is always tried before these.
    FALLBACK_MODELS = ("gemini-3.6-flash", "gemini-2.5-flash", "gemini-2.0-flash",
                       "gemini-1.5-flash")

    def __init__(self, client, settings):
        super().__init__(client, settings)
        self._model: str | None = None

    def _candidates(self) -> list[str]:
        if self._model:
            return [self._model]
        configured = self.settings.gemini_model
        out = [configured] if configured else []
        out += [m for m in self.FALLBACK_MODELS if m != configured]
        return out

    async def complete_json(self, system: str, user: str, *, max_tokens: int = 4096) -> Any:
        key = self.settings.gemini_api_key
        if not key:
            raise NotConfigured("gemini")

        last: Exception | None = None
        for model in self._candidates():
            try:
                result = await self._call(key, model, system, user, max_tokens)
                self._model = model      # remember what worked
                return result
            except ModelNotFound as e:
                last = e
                continue
        if last:
            raise last
        raise ParseFailed("gemini: no usable model")

    async def _call(self, key: str, model: str, system: str, user: str, max_tokens: int) -> Any:
        async def _go():
            r = await self.client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": key},
                json={
                    "system_instruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"temperature": 0.0, "maxOutputTokens": max_tokens,
                                         "responseMimeType": "application/json"},
                },
            )
            if r.status_code == 404:
                raise ModelNotFound(f"gemini: model {model} is not available")
            _raise_for_status(r.status_code, "gemini")
            data = r.json()
            candidate = (data.get("candidates") or [{}])[0]
            parts = ((candidate.get("content") or {}).get("parts") or [])
            # Skip reasoning parts and join the rest; thinking models put the
            # answer after the thought, not in parts[0].
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            if not text.strip():
                finish = candidate.get("finishReason", "")
                raise ParseFailed(f"gemini returned no text (finishReason={finish or 'unknown'})")
            return _extract_json(text)

        return await with_retries(_go)


class OpenAIProvider(LLMProvider):
    name = "openai"

    async def complete_json(self, system: str, user: str, *, max_tokens: int = 4096) -> Any:
        key = self.settings.openai_api_key
        if not key:
            raise NotConfigured("openai")

        async def _go():
            r = await self.client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": self.settings.openai_model, "temperature": 0,
                      "max_tokens": max_tokens, "response_format": {"type": "json_object"},
                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            )
            _raise_for_status(r.status_code, "openai")
            return _extract_json(r.json()["choices"][0]["message"]["content"])

        return await with_retries(_go)


class GroqProvider(LLMProvider):
    name = "groq"

    async def complete_json(self, system: str, user: str, *, max_tokens: int = 4096) -> Any:
        key = self.settings.groq_api_key
        if not key:
            raise NotConfigured("groq")

        async def _go():
            r = await self.client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": self.settings.groq_model, "temperature": 0,
                      "max_tokens": max_tokens, "response_format": {"type": "json_object"},
                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            )
            _raise_for_status(r.status_code, "groq")
            return _extract_json(r.json()["choices"][0]["message"]["content"])

        return await with_retries(_go)


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    async def complete_json(self, system: str, user: str, *, max_tokens: int = 4096) -> Any:
        key = self.settings.anthropic_api_key
        if not key:
            raise NotConfigured("anthropic")

        async def _go():
            r = await self.client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                json={"model": "claude-3-5-haiku-latest", "max_tokens": max_tokens, "temperature": 0,
                      "system": system, "messages": [{"role": "user", "content": user}]},
            )
            _raise_for_status(r.status_code, "anthropic")
            return _extract_json(r.json()["content"][0]["text"])

        return await with_retries(_go)


class NullLLMProvider(LLMProvider):
    """Always-available terminator for the chain.

    Returning ``None`` makes the extractor fall back to its deterministic
    rule-based path rather than raising, so the pipeline runs with zero LLM keys.
    """

    name = "rule_based"
    is_model = False

    def __init__(self, *a, **k):
        pass

    async def complete_json(self, system: str, user: str, *, max_tokens: int = 4096) -> Any:
        return None


class FixtureLLMProvider(LLMProvider):
    """Scripted responses for tests."""

    name = "fixture"

    def __init__(self, responses: list[Any] | None = None, by_marker: dict[str, Any] | None = None):
        self.responses = list(responses or [])
        self.by_marker = by_marker or {}
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, system: str, user: str, *, max_tokens: int = 4096) -> Any:
        self.calls.append((system, user))
        for marker, resp in self.by_marker.items():
            if marker.lower() in user.lower():
                return resp
        return self.responses.pop(0) if self.responses else None


class LLMService:
    """Serialises and paces model calls.

    Free tiers are limited per minute, not just per day, so issuing one call per
    research worker guarantees 429s under any useful concurrency. A small
    semaphore plus a minimum spacing keeps extraction working instead of having
    most of it silently fall back to regexes.
    """

    def __init__(self, chain: ProviderChain[LLMProvider], on_event=None, max_calls: int = 10_000,
                 max_rpm: int = 12, concurrency: int = 2):
        self.chain = chain
        self.on_event = on_event or (lambda *a, **k: None)
        self.calls = 0
        self.max_calls = max_calls
        self._base_interval = 60.0 / max(max_rpm, 1)
        self._min_interval = self._base_interval
        self._sem = asyncio.Semaphore(max(concurrency, 1))
        self._next_slot = 0.0
        self._pace_lock = asyncio.Lock()
        self.rate_limit_hits = 0
        self._max_interval = 20.0
        # Past this many 429s the free tier is clearly exhausted. Continuing to
        # wait longer and longer makes an optional component the bottleneck for
        # the whole run; the deterministic extractor carries on without it.
        self.give_up_after_rate_limits = 8
        self.disabled_for_run = False

    async def _slow_down(self) -> None:
        """Widen the spacing after a 429.

        Published per-minute limits are not the whole story - they vary by model
        and by account age - so the pacing is treated as a starting guess and
        corrected from what the provider actually says.
        """
        async with self._pace_lock:
            self.rate_limit_hits += 1
            if self.rate_limit_hits >= self.give_up_after_rate_limits and not self.disabled_for_run:
                self.disabled_for_run = True
                self.on_event(
                    f"Model rate-limited {self.rate_limit_hits} times; the free tier is spent. "
                    f"Continuing with deterministic extraction only for the rest of this run - "
                    f"slightly weaker descriptions and founder names, everything else unchanged.",
                    "warn")
                return
            previous = self._min_interval
            self._min_interval = min(self._min_interval * 1.6, self._max_interval)
            if self._min_interval > previous * 1.2:
                self.on_event(
                    f"Rate limited; slowing model calls to one every "
                    f"{self._min_interval:.1f}s ({60 / self._min_interval:.0f}/min).", "warn")

    async def _pace(self) -> None:
        async with self._pace_lock:
            now = time.monotonic()
            wait = self._next_slot - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_slot = now + self._min_interval

    @classmethod
    def build(cls, client: httpx.AsyncClient, settings: Settings, on_event=None) -> LLMService:
        provs: list[LLMProvider] = []
        if settings.gemini_api_key:
            provs.append(GeminiProvider(client, settings))
        if settings.openai_api_key:
            provs.append(OpenAIProvider(client, settings))
        if settings.groq_api_key:
            provs.append(GroqProvider(client, settings))
        if settings.anthropic_api_key:
            provs.append(AnthropicProvider(client, settings))
        provs.append(NullLLMProvider())
        return cls(ProviderChain(providers=provs), on_event=on_event,
                   max_calls=settings.budget.max_llm_calls,
                   max_rpm=settings.llm_max_rpm, concurrency=settings.llm_concurrency)

    @property
    def active_provider(self) -> str:
        return self.chain.active_name

    @property
    def has_model(self) -> bool:
        if self.disabled_for_run:
            return False
        return any(getattr(p, "is_model", False) for p in self.chain.available())

    async def complete_json(self, system: str, user: str, *, max_tokens: int = 4096) -> Any:
        if self.calls >= self.max_calls or self.disabled_for_run:
            return None
        async with self._sem:
            await self._pace()
            return await self._complete(system, user, max_tokens)

    async def _complete(self, system: str, user: str, max_tokens: int) -> Any:
        for provider in self.chain.available():
            try:
                res = await provider.complete_json(system, user, max_tokens=max_tokens)
                if getattr(provider, "is_model", False):
                    self.calls += 1
                return res
            except NotConfigured:
                self.chain.disable(provider.name)
            except QuotaExhausted as e:
                self.on_event(f"LLM '{provider.name}' unavailable ({e}) - falling back.", "warn")
                self.chain.disable(provider.name)
            except ModelNotFound as e:
                self.on_event(f"LLM '{provider.name}': {e}", "warn")
            except RateLimited as e:
                await self._slow_down()
                self.on_event(f"LLM '{provider.name}' call failed: {e}", "debug")
            except (Timeout, ParseFailed) as e:
                self.on_event(f"LLM '{provider.name}' call failed: {e}", "debug")
            except Exception as e:  # pragma: no cover
                self.on_event(f"LLM '{provider.name}' unexpected {type(e).__name__}: {e}", "debug")
        return None
