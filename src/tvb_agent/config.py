"""Configuration. Every secret comes from the environment - nothing is hardcoded.

Reads, in order of precedence:
  1. real environment variables
  2. Streamlit secrets (``st.secrets``) when running inside Streamlit
  3. a local ``.env`` file (developer convenience, git-ignored)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:  # optional developer convenience
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _secret(name: str) -> str | None:
    val = os.getenv(name)
    if val:
        return val.strip()
    try:  # Streamlit Cloud stores keys in st.secrets, not the process env
        import streamlit as st  # type: ignore

        if hasattr(st, "secrets") and name in st.secrets:
            v = str(st.secrets[name]).strip()
            return v or None
    except Exception:
        pass
    return None


def _int(name: str, default: int) -> int:
    try:
        return int(_secret(name) or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_secret(name) or default)
    except ValueError:
        return default


@dataclass
class Budget:
    """Hard limits so a run can never spiral. Surfaced in the UI as sliders."""

    target_qualified: int = 15
    max_searches: int = 700
    # Run 9 gave discovery 45% of the budget, built a 357-candidate frontier,
    # researched 64 of them and then stopped for want of searches. Finding
    # companies was never the bottleneck; qualifying them is.
    discovery_search_share: float = 0.20   # rest is reserved for per-company probes
    max_companies_researched: int = 250
    max_pages_fetched: int = 900
    max_llm_calls: int = 250
    max_email_verifications: int = 80
    max_contact_lookups: int = 200
    max_runtime_seconds: int = 2700
    concurrency: int = 8

    @property
    def max_discovery_searches(self) -> int:
        """Searches the discovery loop may spend.

        Without this split the discovery waves consume the entire search budget
        and the research stage - which needs a few targeted probes per company to
        establish funding, founder and US presence - gets nothing.
        """
        return max(4, int(self.max_searches * self.discovery_search_share))

    @classmethod
    def from_env(cls) -> Budget:
        return cls(
            target_qualified=_int("TARGET_QUALIFIED", 15),
            max_searches=_int("MAX_SEARCHES", 700),
            max_companies_researched=_int("MAX_COMPANIES", 250),
            max_pages_fetched=_int("MAX_PAGES", 900),
            max_llm_calls=_int("MAX_LLM_CALLS", 250),
            max_email_verifications=_int("MAX_EMAIL_VERIFICATIONS", 80),
            max_contact_lookups=_int("MAX_CONTACT_LOOKUPS", 200),
            max_runtime_seconds=_int("MAX_RUNTIME_SECONDS", 2700),
            concurrency=_int("CONCURRENCY", 8),
        )


@dataclass
class Settings:
    # --- search providers (first configured one wins; others are fallbacks) ---
    serper_api_key: str | None = field(default_factory=lambda: _secret("SERPER_API_KEY"))
    tavily_api_key: str | None = field(default_factory=lambda: _secret("TAVILY_API_KEY"))
    brave_api_key: str | None = field(default_factory=lambda: _secret("BRAVE_API_KEY"))
    google_cse_key: str | None = field(default_factory=lambda: _secret("GOOGLE_CSE_KEY"))
    google_cse_cx: str | None = field(default_factory=lambda: _secret("GOOGLE_CSE_CX"))
    allow_ddg_fallback: bool = field(default_factory=lambda: (_secret("ALLOW_DDG_FALLBACK") or "true").lower() == "true")

    # --- llm providers ---
    gemini_api_key: str | None = field(default_factory=lambda: _secret("GEMINI_API_KEY"))
    openai_api_key: str | None = field(default_factory=lambda: _secret("OPENAI_API_KEY"))
    groq_api_key: str | None = field(default_factory=lambda: _secret("GROQ_API_KEY"))
    anthropic_api_key: str | None = field(default_factory=lambda: _secret("ANTHROPIC_API_KEY"))
    # Free LLM tiers cap requests per minute, not just per day.
    llm_max_rpm: int = field(default_factory=lambda: _int("LLM_MAX_RPM", 8))
    llm_concurrency: int = field(default_factory=lambda: _int("LLM_CONCURRENCY", 2))
    gemini_model: str = field(default_factory=lambda: _secret("GEMINI_MODEL") or "gemini-3.6-flash")
    openai_model: str = field(default_factory=lambda: _secret("OPENAI_MODEL") or "gpt-4o-mini")
    groq_model: str = field(default_factory=lambda: _secret("GROQ_MODEL") or "llama-3.3-70b-versatile")

    # --- email verification ---
    hunter_api_key: str | None = field(default_factory=lambda: _secret("HUNTER_API_KEY"))
    zerobounce_api_key: str | None = field(default_factory=lambda: _secret("ZEROBOUNCE_API_KEY"))
    abstract_api_key: str | None = field(default_factory=lambda: _secret("ABSTRACT_EMAIL_API_KEY"))
    # Contact enrichment. Optional: without it the agent finds only addresses
    # published on pages it can read, which is a much smaller set.
    apollo_api_key: str | None = field(default_factory=lambda: _secret("APOLLO_API_KEY"))
    enable_smtp_probe: bool = field(default_factory=lambda: (_secret("ENABLE_SMTP_PROBE") or "false").lower() == "true")

    # --- behaviour ---
    db_path: str = field(default_factory=lambda: _secret("DB_PATH") or "data/tvb_agent.sqlite3")
    user_agent: str = field(
        default_factory=lambda: _secret("USER_AGENT")
        or "TVB-Lead-Agent/1.0 (+https://github.com/; research crawler; contact via repo issues)"
    )
    respect_robots: bool = field(default_factory=lambda: (_secret("RESPECT_ROBOTS") or "true").lower() == "true")
    per_host_delay_seconds: float = field(default_factory=lambda: _float("PER_HOST_DELAY", 1.0))
    http_timeout_seconds: float = field(default_factory=lambda: _float("HTTP_TIMEOUT", 20.0))
    cache_ttl_seconds: int = field(default_factory=lambda: _int("CACHE_TTL_SECONDS", 86400))

    # --- qualification thresholds (documented in the README) ---
    min_amount_usd: float = field(default_factory=lambda: _float("MIN_AMOUNT_USD", 1_000_000))
    max_amount_usd: float = field(default_factory=lambda: _float("MAX_AMOUNT_USD", 5_000_000))
    boundary_tolerance: float = field(default_factory=lambda: _float("BOUNDARY_TOLERANCE", 0.10))
    max_weak_us_signals: int = field(default_factory=lambda: _int("MAX_WEAK_US_SIGNALS", 1))

    budget: Budget = field(default_factory=Budget.from_env)

    # ----------------------------------------------------------------- #
    @property
    def has_search(self) -> bool:
        return bool(self.serper_api_key or self.tavily_api_key or self.brave_api_key
                    or (self.google_cse_key and self.google_cse_cx) or self.allow_ddg_fallback)

    @property
    def has_llm(self) -> bool:
        return bool(self.gemini_api_key or self.openai_api_key or self.groq_api_key or self.anthropic_api_key)

    @property
    def has_email_verifier(self) -> bool:
        return bool(self.hunter_api_key or self.zerobounce_api_key or self.abstract_api_key)

    def health(self) -> list[dict]:
        """Rows for the UI health panel: what is live and what degrades without it."""
        return [
            {"capability": "Web search", "provider": self.search_provider_name(),
             "configured": bool(self.serper_api_key or self.tavily_api_key or self.brave_api_key or (self.google_cse_key and self.google_cse_cx)),
             "degraded_without": "Falls back to DuckDuckGo HTML scraping: lower recall and easily rate-limited."},
            {"capability": "LLM extraction", "provider": self.llm_provider_name(),
             "configured": self.has_llm,
             "degraded_without": "Falls back to rule-based regex extraction: weaker founder/description extraction, funding parsing unaffected."},
            {"capability": "Email deliverability", "provider": self.email_verifier_name(),
             "configured": self.has_email_verifier,
             "degraded_without": "Verification rests on syntax + MX + authoritative-source attribution only (still never guessed)."},
            {"capability": "Contact enrichment", "provider": self.contact_provider_name(),
             "configured": bool(self.hunter_api_key or self.apollo_api_key),
             "degraded_without": "Only founder addresses published on a readable page can be found, which is a small minority of companies."},
        ]

    def search_provider_name(self) -> str:
        if self.serper_api_key:
            return "Serper"
        if self.tavily_api_key:
            return "Tavily"
        if self.brave_api_key:
            return "Brave"
        if self.google_cse_key and self.google_cse_cx:
            return "Google CSE"
        return "DuckDuckGo (fallback)" if self.allow_ddg_fallback else "none"

    def llm_provider_name(self) -> str:
        if self.gemini_api_key:
            return "Gemini"
        if self.openai_api_key:
            return "OpenAI"
        if self.groq_api_key:
            return "Groq"
        if self.anthropic_api_key:
            return "Anthropic"
        return "rule-based (fallback)"

    def contact_provider_name(self) -> str:
        """Hunter first: it cites the pages it saw an address on."""
        names = []
        if self.hunter_api_key:
            names.append("Hunter (cited sources)")
        if self.apollo_api_key:
            names.append("Apollo")
        return " + ".join(names) or "none"

    def email_verifier_name(self) -> str:
        names = []
        if self.zerobounce_api_key:
            names.append("ZeroBounce")
        if self.hunter_api_key:
            names.append("Hunter")
        if self.abstract_api_key:
            names.append("Abstract")
        names.append("DNS/MX (built-in)")
        return " + ".join(names)


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings
