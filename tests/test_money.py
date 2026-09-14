"""The money parser is the deterministic core of the funding gate."""

import pytest

from tvb_agent.models import AmountType
from tvb_agent.validation.money import best_qualifying_amount, parse_amounts


@pytest.mark.parametrize(
    "text,currency,usd_approx,atype",
    [
        ("Acme raised $2.5 million in a seed round.", "USD", 2_500_000, AmountType.FUNDING_RAISED),
        ("The startup secured ₹25 crore in pre-Series A funding.", "INR", 3_000_000, AmountType.FUNDING_RAISED),
        ("Bengaluru-based Acme. Rs 40 lakh was raised in the round.", "INR", 48_000, AmountType.FUNDING_RAISED),
        ("Foo a annoncé une levée de fonds de €3,2 millions.", "EUR", 3_456_000, AmountType.FUNDING_RAISED),
        ("Singapore startup raises S$4 million Series A.", "SGD", 2_960_000, AmountType.FUNDING_RAISED),
        ("Nairobi fintech bags Ksh 300 million in funding.", "KES", 2_310_000, AmountType.FUNDING_RAISED),
        ("The company reported ARR of $1.8M last year.", "USD", 1_800_000, AmountType.ARR),
        ("Annual revenue reached £2 million.", "GBP", 2_540_000, AmountType.REVENUE),
    ],
)
def test_parses_currencies_and_types(text, currency, usd_approx, atype):
    amounts = parse_amounts(text)
    assert amounts, f"nothing parsed from {text!r}"
    a = amounts[0]
    assert a.currency == currency
    assert abs(float(a.amount_usd) - usd_approx) < usd_approx * 0.02
    assert a.amount_type is atype


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Acme was valued at $50 million post-money.", AmountType.VALUATION),
        ("The global logistics market is worth $4 billion.", AmountType.TAM),
        ("XYZ Ventures closes $5M maiden fund.", AmountType.FUND_SIZE),
        ("Company expects to reach $10 million revenue by 2028.", AmountType.PROJECTION),
        ("Received a $2 million grant from the innovation scheme.", AmountType.GRANT),
        ("Average contract value is $1.5 million per client.", AmountType.ACV),
        ("The firm has $300 million in assets under management.", AmountType.AUM),
        ("Secured $3 million in venture debt financing.", AmountType.DEBT),
    ],
)
def test_rejecting_categories_are_not_funding(text, expected):
    amounts = parse_amounts(text)
    assert amounts
    assert amounts[0].amount_type is expected
    assert not amounts[0].qualifies_as_criterion_input


def test_cumulative_total_is_preferred_over_single_round():
    text = ("The company raised $1.2 million in seed funding last year. "
            "It has raised a total of $4.2 million to date across two rounds.")
    amounts = parse_amounts(text)
    best = best_qualifying_amount(amounts, 1e6, 5e6)
    assert best is not None
    assert best.is_cumulative
    assert float(best.amount_usd) == pytest.approx(4_200_000)


def test_in_band_amount_preferred_over_out_of_band():
    text = "Acme raised $200,000 in pre-seed and later raised $3 million in seed funding."
    best = best_qualifying_amount(parse_amounts(text), 1e6, 5e6)
    assert float(best.amount_usd) == pytest.approx(3_000_000)


def test_european_and_anglo_decimal_notation():
    assert float(parse_amounts("€1.234,56 million raised")[0].amount_original) == pytest.approx(1234.56e6, rel=1e-6)
    assert float(parse_amounts("$1,234.56 million raised")[0].amount_original) == pytest.approx(1234.56e6, rel=1e-6)


def test_bare_numbers_without_currency_are_ignored():
    assert parse_amounts("The team of 25 people raised morale in 2024.") == []


def test_unknown_type_does_not_qualify():
    amounts = parse_amounts("The figure was $2 million.")
    assert amounts[0].amount_type is AmountType.UNKNOWN
    assert not amounts[0].qualifies_as_criterion_input


# --------------------------------------------------------------------------- #
# Non-English coverage. The whole point of this agent is companies outside the
# US, and their funding is usually reported in the local language.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,currency,original", [
    ("Das Startup sammelte EUR 2,6 Millionen in einer Seed-Runde ein.", "EUR", 2_600_000),
    ("La startup ha cerrado una ronda de financiación de 3,5 millones de euros.", "EUR", 3_500_000),
    ("A startup levantou R$ 12 milhões em rodada seed.", "BRL", 12_000_000),
    ("La startup ha raccolto 2,4 milioni di euro.", "EUR", 2_400_000),
    ("Het bedrijf haalde EUR 1,8 miljoen op in een investeringsronde.", "EUR", 1_800_000),
    ("Bolaget tog in 25 miljoner SEK i en seedrunda.", "SEK", 25_000_000),
    ("Spółka pozyskała 12 milionów PLN w rundzie finansowania.", "PLN", 12_000_000),
    ("La startup française a levé 4 millions d'euros.", "EUR", 4_000_000),
    ("Girişim 40 milyon TL yatırım aldı.", "TRY", 40_000_000),
    ("Empresa levantou 8 milhões de reais.", "BRL", 8_000_000),
])
def test_funding_in_local_languages(text, currency, original):
    amounts = parse_amounts(text)
    assert amounts, f"nothing parsed from {text!r}"
    a = amounts[0]
    assert a.currency == currency
    assert float(a.amount_original) == pytest.approx(original)
    assert a.amount_type is AmountType.FUNDING_RAISED, \
        "a non-English funding sentence must still classify as funding"


def test_unrecognised_currency_is_rejected_not_assumed_to_be_dollars():
    """The costliest silent failure available: 40m lira read as $40m.

    An explicit currency token we do not recognise must drop the figure, not
    fall back to USD, because the fallback can push a company into the
    qualifying band on money it never raised.
    """
    assert parse_amounts("Raised 5 million zorkbucks in seed funding.") == []


def test_local_currency_beats_the_dollar_default():
    a = parse_amounts("Girişim 40 milyon TL yatırım aldı.")[0]
    assert a.currency == "TRY"
    assert float(a.amount_usd) < 5_000_000, "40m lira is not 40m dollars"


def test_spelled_out_currencies_are_understood():
    assert parse_amounts("raised 2 million dirhams")[0].currency == "AED"
    assert parse_amounts("raised 8 milhões de reais")[0].currency == "BRL"


@pytest.mark.parametrize("text,expected", [
    ("Mexican startup raises 12 millones de pesos.", "MXN"),
    ("Colombian startup raises 12 millones de pesos.", "COP"),
    ("Indian startup raised 30 million rupees.", "INR"),
    ("Pakistani startup raised 30 million rupees.", "PKR"),
    ("Swedish startup raised 25 million kronor.", "SEK"),
    ("Singapore startup raised 4 million dollars.", "SGD"),
])
def test_ambiguous_currency_words_are_resolved_from_context(text, expected):
    """"Pesos" spans a 200x range across countries, so context decides."""
    amounts = parse_amounts(text)
    assert amounts, text
    assert amounts[0].currency == expected


def test_ambiguous_currency_with_no_context_is_dropped():
    assert parse_amounts("A startup raises 12 millones de pesos.") == []


def test_ambiguous_currency_resolves_from_elsewhere_on_the_page():
    """The country is often named once, far from the figure."""
    doc = ("Karachi startup roundup, March 2026. Several deals closed this month. "
           "The company confirmed that Rs 4 crore was raised in the round.")
    amounts = parse_amounts(doc)
    assert amounts and amounts[0].currency == "PKR"


def test_bare_dollars_default_to_usd_but_yield_to_local_context():
    assert parse_amounts("raised $2.5 million in seed funding")[0].currency == "USD"
    assert parse_amounts("Australian startup raised $3 million")[0].currency == "AUD"


def test_local_language_revenue_is_classified_as_revenue():
    a = parse_amounts("Das Unternehmen erzielte einen Umsatz von EUR 2 Millionen.")[0]
    assert a.amount_type is AmountType.REVENUE
