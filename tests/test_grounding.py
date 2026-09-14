"""Quote grounding is the mechanism that makes model output safe to use."""

import pytest

from tvb_agent.research.grounding import snippet_around, verify_quote

SOURCE = (
    "Acme Technologies, a Bengaluru-based SaaS platform, today announced it has raised "
    "$2.5 million in a seed round led by Blume Ventures. The company was founded in 2021 "
    "by Jane Doe and Ravi Kumar. Acme serves 180 clinics across India."
)


@pytest.mark.parametrize("quote", [
    "has raised $2.5 million in a seed round led by Blume Ventures",
    "HAS   RAISED  $2.5 MILLION in a seed round led by Blume Ventures",
    "The company was founded in 2021 by Jane Doe and Ravi Kumar.",
])
def test_real_quotes_are_accepted(quote):
    assert verify_quote(quote, SOURCE).ok


@pytest.mark.parametrize("quote", [
    "Acme raised $12 million from Sequoia in a Series B round last year",
    "The company reports annual recurring revenue of $4 million",
    "Acme Technologies is headquartered in San Francisco, California",
])
def test_fabricated_quotes_are_rejected(quote):
    result = verify_quote(quote, SOURCE)
    assert not result.ok
    assert result.mode == "rejected"


def test_smart_quotes_and_dashes_do_not_break_matching():
    src = "The company said: “we have raised €3.2 million” — a milestone."
    assert verify_quote('The company said: "we have raised €3.2 million" - a milestone', src).ok


def test_truncated_quote_matches_approximately():
    quote = "it has raised $2.5 million in a seed round led by Blume Ventures and other investors"
    result = verify_quote(quote, SOURCE)
    assert result.ok
    assert result.mode == "approximate"


def test_too_short_quotes_are_refused():
    assert not verify_quote("raised", SOURCE).ok
    assert not verify_quote("", SOURCE).ok


def test_empty_source_never_grounds_anything():
    assert not verify_quote("has raised $2.5 million in a seed round", "").ok


def test_snippet_returns_real_source_text():
    snip = snippet_around("raised $2.5 million in a seed round", SOURCE)
    assert "2.5 million" in snip
    assert snip in " ".join(SOURCE.split()) or "Acme" in snip
