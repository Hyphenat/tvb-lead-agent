import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest

from tvb_agent.config import Budget, Settings
from tvb_agent.storage import Store


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.db_path = str(tmp_path / "test.sqlite3")
    s.respect_robots = False
    s.per_host_delay_seconds = 0.0
    s.cache_ttl_seconds = 0
    # Blank EVERY credential, by pattern rather than by name. Listing them one
    # by one meant that adding a provider silently leaked the developer's own
    # .env into the tests: the Apollo and Hunter keys did exactly that, and two
    # tests passed on a machine with no keys while failing on a machine with
    # them. A test that depends on who is running it is not a test.
    for name in vars(s):
        if name.endswith(("_api_key", "_cse_key", "_cse_cx")):
            setattr(s, name, None)
    s.allow_ddg_fallback = False
    # Production paces model calls to respect free-tier per-minute limits.
    # Tests must not wait those intervals out.
    s.llm_max_rpm = 100_000
    s.llm_concurrency = 16
    s.budget = Budget(target_qualified=2, max_searches=60, max_companies_researched=8,
                      max_pages_fetched=60, max_runtime_seconds=60, concurrency=4)
    return s


@pytest.fixture
def store(settings) -> Store:
    st = Store(settings.db_path)
    yield st
    st.close()


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    """MX lookups never touch the network during tests."""
    from tvb_agent.providers.email_verify import MXChecker

    monkeypatch.setattr(MXChecker, "_lookup", staticmethod(lambda domain: (
        [] if "nomx" in domain else [f"mx1.{domain}", f"mx2.{domain}"]
    )))
