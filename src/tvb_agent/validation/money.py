"""Deterministic monetary-figure parser.

Two jobs, both of which an LLM does badly and a regex does reliably:

1. **Parse** an amount written in any of the world's common notations - including
   ``$2.5 million``, ``EUR 3,2 Mio``, ``₹25 crore``, ``Rs 40 lakh``, ``S$4m``.
2. **Classify** what the figure *measures*.  The single most damaging failure
   mode for this project is reading "valued at $50 million" or "the market is
   worth $4 billion" as though it were money the company raised, so
   classification runs on the words surrounding the figure and defaults to
   ``UNKNOWN`` (which fails the gate) rather than guessing.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from ..models import AmountType, MoneyAmount
from .fx import (
    AMBIGUOUS_CURRENCY_TOKENS,
    DOLLAR_TOKENS,
    FX_RATE_DATE,
    MULTIPLIERS,
    SYMBOL_TO_CURRENCY,
    UnknownCurrency,
    guess_currency_from_context,
    to_usd,
)

# --------------------------------------------------------------------------- #
# Classification vocabulary. Order of evaluation is deliberate: the *rejecting*
# categories are tested first, so an ambiguous sentence never lands on
# FUNDING_RAISED by accident.
# --------------------------------------------------------------------------- #
_VALUATION = (
    "valuation", "valued at", "post-money", "pre-money", "post money", "pre money",
    "market cap", "market capitalisation", "market capitalization", "worth an estimated",
    "enterprise value", "unicorn status", "at a valuation",
)
_TAM = (
    "addressable market", "market size", "market is worth", "market worth", "tam",
    "market opportunity", "industry is worth", "market is expected to reach",
    "market is projected", "sector is worth", "global market",
)
_ACV = ("contract value", "acv", "average deal", "deal size", "per contract", "annual contract",
        "contract with", "supply contract", "won a contract", "signed a contract",
        "contract to supply", "framework agreement")
_AUM = ("assets under management", "aum", "assets under advisory")
# A bare "fund" is far too eager: it fires on "led by Norfund", "Sequoia Fund"
# and "seed funding", and a real run silently reclassified a genuine $27.5M
# round as a VC vehicle because an investor's name happened to contain it. Only
# phrasings that actually describe raising a *fund* count.
_FUND_SIZE = (
    "maiden fund", "debut fund", "venture fund", "new fund", "first fund",
    "corpus", "investment vehicle", "limited partners", " lps ", "投资基金",
    "fund i", "fund ii", "fund iii", "close of its fund", "closes its fund",
    "closed its fund", "launches a fund", "launched a fund", "raises a fund",
    "raised a fund", "fund to invest", "fund to back", "fund targeting",
)
_GRANT = ("grant", "subsidy", "prize", "non-dilutive", "innovate uk award", "award from", "scheme")
_PROJECTION = (
    "projected", "projects", "expects to reach", "expected to reach", "aims to reach",
    "targeting", "forecast", "on track to", "hopes to", "by 2027", "by 2028", "by 2030",
    "plans to hit", "guidance",
)
_DEBT = ("debt financing", "venture debt", "credit facility", "loan", "debt round", "credit line")
_ARR = ("arr", "annual recurring revenue", "recurring revenue", "run rate", "run-rate")
_REVENUE = (
    "revenue", "revenues", "turnover", "topline", "top line", "sales of", "in sales",
    "gross merchandise", "billings",
    # international
    "chiffre d'affaires", "facturación", "facturacion", "ingresos de", "umsatz", "jahresumsatz",
    "faturamento", "receita de", "fatturato", "omzet", "omsättning", "przychody", "ciro",
    "pendapatan", "doanh thu",
)
# Non-English funding language. Without this, an amount in a Spanish, German or
# Indonesian article parses correctly but classifies as UNKNOWN - and an unknown
# type fails the gate, so the agent would quietly ignore exactly the non-US
# coverage it exists to read.
_FUNDING_INTL = (
    # de
    "finanzierungsrunde", "sammelte", "eingesammelt", "erhielt", "seed-runde", "investition von",
    # fr
    "levée de fonds", "levee de fonds", "lève", "leve", "a levé", "a leve", "tour de table",
    "financement de",
    # es
    "ronda de financiación", "ronda de financiacion", "ha recaudado", "recauda", "levantó",
    "levanto", "capta", "ha captado", "ronda semilla",
    # pt
    "levantou", "captou", "rodada de", "aporte de", "rodada seed",
    # it
    "ha raccolto", "raccolto", "round di finanziamento", "aumento di capitale",
    # nl
    "haalde op", "opgehaald", "investeringsronde",
    # nordics
    "tog in", "hentet", "reiste", "rahoituskierros", "finansieringsrunde",
    # pl / cee
    "pozyskała", "pozyskala", "pozyskał", "runda finansowania",
    # tr
    "yatırım aldı", "yatirim aldi", "yatırım turu",
    # id / ms
    "mengumpulkan", "pendanaan", "meraih pendanaan", "kutipan dana",
    # vi
    "huy động", "gọi vốn",
    # ar
    "جولة تمويل", "تمويل بقيمة",
)

_FUNDING = (
    "raised", "raises", "raising", "secured", "secures", "closed", "closes", "bagged", "bags",
    "nets", "netted", "funding round", "seed round", "pre-seed", "series a", "series b",
    "investment round", "led by", "co-led by", "participation from", "backed by",
    "financing round", "capital injection", "infusion", "levée de fonds", "ronda de",
    "funding of", "in funding", "investment of", "round of funding", "oversubscribed",
)
_CUMULATIVE = (
    "total funding", "total raised", "raised a total", "brings its total", "brings total",
    "to date", "cumulative", "overall funding", "since inception", "all-time",
)

# The figure itself: optional symbol, number, optional multiplier, optional code.
# Grouping separator may be "," (1,234,567), "." (1.234.567), a space or NBSP.
_NUM = r"\d{1,3}(?:[,.\s\u00a0\u202f]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
_SYMS = r"US\$|A\$|C\$|S\$|NZ\$|HK\$|R\$|MX\$|SG\$|AU\$|CA\$|\$|€|£|₹|₨|¥|₩|₪|₺|₦|₴|₽|₱|₫|฿|zł"
_CODES = (
    r"USD|EUR|GBP|INR|PKR|AED|SAR|QAR|KWD|BHD|OMR|SGD|AUD|CAD|NZD|HKD|JPY|CNY|RMB|KRW|TWD|"
    r"ILS|NIS|TRY|TL|EGP|ZAR|NGN|KES|GHS|MAD|TND|BRL|MXN|ARS|CLP|COP|PEN|UYU|SEK|NOK|DKK|PLN|"
    r"CZK|HUF|RON|BGN|UAH|RUB|IDR|MYR|THB|PHP|VND|CHF|LKR|BDT|NPR|Rs\.?|Rp|RM|Ksh|Dh|Dhs|"
    # spelled out, as local coverage usually writes it
    r"euros|euro|pounds|pound|sterling|dollars|dollar|reais|real|z\u0142otych|zlotych|z\u0142oty|"
    r"kronor|kroner|kronen|francs|franken|dirhams|dirham|riyals|riyal|shekels|shekel|"
    r"rupees|rupee|rupiah|ringgit|baht|pesos|peso|soles|liras|lira|naira|shillings|shilling|"
    r"yen|yuan|won|dong|taka|kyat|lei|forint|koruna|hryvnia|rubles|rouble"
)
_MULT = (
    # Ordered longest-first: regex alternation is greedy in order, so a short
    # variant listed early ("milion") would swallow the stem of a longer one
    # ("milionow") and strip the currency code that follows it.
    r"millionene|millionen|milionow|milion\u00f3w|milhoes|milh\u00f5es|miljoenen|miljoona[a]?|"
    r"millioner|miljoner|milliarden|milliarde|mil millones|millones|milliard|miliardi|"
    r"millions|million|mill\u00f3n|milhao|milh\u00e3o|milioni|milione|miljoen|miljon|"
    r"billions|billion|thousand|milyar|milyon|miliar|miliony|juta|"
    r"crores|crore|lakhs|lakh|lacs|lac|mio|mn|mm|bn|cr|k|m|b"
)

# Word boundaries on the currency codes. Without them "The company won $3 million"
# parsed as 3,000,000 KRW - about $2,190 - and the round vanished; "Try our plan"
# and "to open 2 million accounts" did the same through TRY and PEN.
_PATTERN = re.compile(
    rf"(?:\b(?P<pre_code>{_CODES})\b)?\s*(?P<sym>{_SYMS})?\s*"
    rf"(?P<num>{_NUM})\s*"
    rf"(?P<mult>{_MULT})?"
    rf"(?:\s+(?:de|di|d\u2019|d'|of|em|w|van)\b)?\s*"
    rf"(?:\b(?P<post_code>{_CODES})\b)?",
    re.IGNORECASE,
)

CONTEXT_CHARS = 160


def _to_decimal(raw: str) -> Decimal | None:
    """Handle both 1,234.5 (anglo) and 1.234,5 / 1 234,5 (euro) notations."""
    s = raw.strip().replace(" ", " ").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        frac = s.split(",")[-1]
        s = s.replace(",", ".") if len(frac) in (1, 2) and s.count(",") == 1 else s.replace(",", "")
    elif s.count(".") > 1 or (s.count(".") == 1 and len(s.split(".")[-1]) == 3):
        # "1.500.000" and "1.500" - dots as thousands separators, which is how
        # German, Dutch, Italian, Spanish and Portuguese coverage writes figures.
        # Decimal() choked on these and the amount was silently dropped, in
        # exactly the non-US markets this agent exists to read.
        s = s.replace(".", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def classify_amount(context: str) -> AmountType:
    """Decide what a figure measures from the words around it."""
    c = f" {context.lower()} "

    def has(words) -> bool:
        """Whole-phrase match.

        Plain substring matching read "carrier" as ARR, "aumento di capitale" as
        assets under management, and "scheme" inside unrelated prose as a grant -
        so a customer contract and a litigation settlement both arrived at the
        funding gate as qualifying revenue.
        """
        return any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", c) for w in words)

    # Rejecting categories first - an ambiguous sentence must not become funding.
    if has(_VALUATION):
        return AmountType.VALUATION
    if has(_TAM):
        return AmountType.TAM
    if has(_AUM):
        return AmountType.AUM
    if has(_ACV):
        return AmountType.ACV
    # "raises $5M fund" is a VC vehicle, never an operating company we want.
    if has(_FUND_SIZE) and not re.search(r"\bfunding\b|\bfunded\b", c):
        return AmountType.FUND_SIZE
    if has(_PROJECTION):
        return AmountType.PROJECTION
    if has(_DEBT):
        return AmountType.DEBT
    if has(_GRANT):
        return AmountType.GRANT
    # Accepting categories.
    if has(_ARR):
        return AmountType.ARR
    if has(_REVENUE):
        return AmountType.REVENUE
    if has(_FUNDING) or has(_FUNDING_INTL):
        return AmountType.FUNDING_RAISED
    return AmountType.UNKNOWN


def _resolve_currency(sym: str | None, pre: str | None, post: str | None, context: str,
                      document: str = "") -> str | None:
    """Map the matched currency token to an ISO code, or give up.

    Critically, an explicit token we do not recognise must NOT fall back to USD.
    "40 milyon TL" defaulting to dollars turns 40m lira into $40m - a 34x
    overstatement that would push a company into the qualifying band on a figure
    it never raised. Unrecognised means unknown, and unknown is dropped.
    """
    # An explicit symbol outranks a spelled-out word, because several currency
    # words are also ordinary English: "The company won $3 million" read "won" as
    # the Korean won and turned a $3M round into $2,190. A symbol is unambiguous;
    # a word next to one is a coincidence.
    for token in (sym, pre, post) if sym else (pre, post, sym):
        if not token:
            continue
        tok = token.strip().lower()
        code = SYMBOL_TO_CURRENCY.get(tok)

        if tok in AMBIGUOUS_CURRENCY_TOKENS:
            # Look at the sentence first, then the whole page: an article about
            # an Indian startup may only name the country in its opening line,
            # and dropping the figure over that would discard real coverage.
            return guess_currency_from_context(context) or guess_currency_from_context(document)

        if tok in DOLLAR_TOKENS:
            local = guess_currency_from_context(context) or guess_currency_from_context(document)
            return local if local in {"SGD", "AUD", "CAD", "NZD", "HKD", "TWD"} else "USD"

        if code:
            return code
        # A token was written but is not one we know: refuse rather than assume.
        return guess_currency_from_context(context) or guess_currency_from_context(document)
    return guess_currency_from_context(context) or guess_currency_from_context(document)


def parse_amounts(text: str, *, require_currency: bool = True) -> list[MoneyAmount]:
    """Extract every monetary figure in ``text`` with its type and USD value."""
    out: list[MoneyAmount] = []
    if not text:
        return out
    seen: set[tuple] = set()

    for m in _PATTERN.finditer(text):
        num_raw = m.group("num")
        if not num_raw:
            continue
        sym, pre, post, mult = m.group("sym"), m.group("pre_code"), m.group("post_code"), m.group("mult")
        if require_currency and not (sym or pre or post):
            continue

        value = _to_decimal(num_raw)
        if value is None or value <= 0:
            continue
        if mult:
            value *= MULTIPLIERS.get(mult.strip().lower(), Decimal(1))
        elif value < 1000:
            # "3" with no multiplier and no context is noise, not three dollars.
            continue

        lo = max(0, m.start() - CONTEXT_CHARS)
        context = text[lo : m.end() + CONTEXT_CHARS]
        currency = _resolve_currency(sym, pre, post, context, text[:20_000])
        if currency is None:
            continue
        try:
            usd, rate = to_usd(value, currency)
        except UnknownCurrency:
            continue

        key = (currency, str(value), m.start() // 40)
        if key in seen:
            continue
        seen.add(key)

        out.append(
            MoneyAmount(
                raw=m.group(0).strip(),
                amount_original=value,
                currency=currency,
                amount_usd=usd,
                fx_rate=rate,
                fx_date=FX_RATE_DATE,
                amount_type=classify_amount(context),
                is_cumulative=any(w in context.lower() for w in _CUMULATIVE),
                context=re.sub(r"\s+", " ", context).strip(),
                start=m.start(),
                end=m.end(),
            )
        )
    return out


def best_qualifying_amount(
    amounts: list[MoneyAmount], lo: float, hi: float
) -> MoneyAmount | None:
    """Pick the figure that best represents what the company raised or earns.

    Preference order: a cumulative "total raised" statement beats a single round
    (it is the fuller picture and avoids double-counting extensions), and an
    in-band figure beats an out-of-band one so that a company is not rejected
    because an early smaller round was mentioned alongside the current one.
    """
    usable = [a for a in amounts if a.qualifies_as_criterion_input]
    if not usable:
        return None
    # The band is a statement about the SIZE of the company, so the largest
    # attributable figure is the one that decides it. Preferring an in-band
    # figure over a larger one meant "raised $2.5M seed ... has now closed a $45M
    # Series B, $52M total" qualified on the $2.5M and shipped as a lead, with no
    # trace of the other two numbers anywhere in the export. That is how three
    # false positives reached a delivered list.
    biggest = max(usable, key=lambda a: float(a.amount_usd))
    if float(biggest.amount_usd) > hi:
        return biggest          # out of band, and the gate must say so
    in_band = [a for a in usable if lo <= float(a.amount_usd) <= hi]
    pool = in_band or usable
    return sorted(
        pool,
        key=lambda a: (
            0 if a.is_cumulative else 1,
            0 if a.amount_type is AmountType.FUNDING_RAISED else 1,
            -float(a.amount_usd),
        ),
    )[0]
