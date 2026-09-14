"""The interface must render without errors and show real results.

A reviewer opens a URL; if the page throws, nothing else about the project
matters. These tests exercise the actual Streamlit script.
"""

import asyncio
import os

import pytest
import respx
from streamlit.testing.v1 import AppTest

from tvb_agent.agent import LeadAgent

from .test_integration import configure, install_fake_web

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app", "streamlit_app.py"))


@pytest.fixture
def populated_db(settings, store):
    """Run the agent against the fake web so the UI has something to show."""

    @respx.mock
    async def go():
        install_fake_web()
        agent = LeadAgent(settings=settings, store=store)
        return await agent.run(seed=7)

    configure(settings)
    settings.budget.target_qualified = 2
    asyncio.run(go())
    return settings.db_path


def _app(db_path: str) -> AppTest:
    import streamlit as st

    from tvb_agent.config import get_settings

    os.environ["DB_PATH"] = db_path
    os.environ["ALLOW_DDG_FALLBACK"] = "true"
    # Streamlit caches resources for the life of the process; without clearing,
    # one test's database leaks into the next.
    st.cache_resource.clear()
    get_settings(refresh=True)
    return AppTest.from_file(APP, default_timeout=120)


def _all_text(at) -> str:
    parts = []
    for group in (at.markdown, at.caption, at.title, at.header, at.subheader):
        for el in group:
            parts.append(str(getattr(el, "value", "") or getattr(el, "body", "")))
    return " ".join(parts)


def test_app_renders_with_empty_database(tmp_path):
    at = _app(str(tmp_path / "empty.sqlite3")).run()
    assert not at.exception, at.exception
    assert any("Venture Build" in str(t.value) for t in at.title)


def test_app_shows_qualified_leads(populated_db):
    at = _app(populated_db).run()
    assert not at.exception, at.exception
    text = _all_text(at)
    assert "Zeta Care" in text or "Nova Pay" in text, "expected a qualified lead to be rendered"


def test_methodology_documents_the_rules(populated_db):
    at = _app(populated_db).run()
    assert not at.exception, at.exception
    text = _all_text(at).lower()
    assert "never passes" in text, "the unknown-HQ rule must be documented in the UI"
    assert "catch-all" in text, "the catch-all guard must be documented in the UI"
