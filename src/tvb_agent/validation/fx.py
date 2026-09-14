"""Currency normalisation.

A static, *dated* rate table is used by default so that every run is
deterministic and testable, and so the system needs no extra API key.  The rate
and its date are stored on every :class:`MoneyAmount`, so a reviewer can see
exactly how a foreign figure was converted.  ``FX_RATE_DATE`` should be
refreshed periodically; ``BOUNDARY_TOLERANCE`` in the config flags any company
whose amount lands near the band edge, where FX drift could change the verdict.
"""

from __future__ import annotations

from decimal import Decimal

FX_RATE_DATE = "2026-09-01"

# USD per 1 unit of currency.
FX_USD_PER_UNIT: dict[str, Decimal] = {
    "USD": Decimal("1.0"),
    "EUR": Decimal("1.08"),
    "GBP": Decimal("1.27"),
    "CHF": Decimal("1.12"),
    "CAD": Decimal("0.73"),
    "AUD": Decimal("0.66"),
    "NZD": Decimal("0.61"),
    "SGD": Decimal("0.74"),
    "HKD": Decimal("0.128"),
    "JPY": Decimal("0.0066"),
    "CNY": Decimal("0.138"),
    "KRW": Decimal("0.00073"),
    "TWD": Decimal("0.031"),
    "INR": Decimal("0.0120"),
    "PKR": Decimal("0.0036"),
    "BDT": Decimal("0.0085"),
    "LKR": Decimal("0.0033"),
    "NPR": Decimal("0.0075"),
    "AED": Decimal("0.2723"),
    "SAR": Decimal("0.2666"),
    "QAR": Decimal("0.2747"),
    "KWD": Decimal("3.25"),
    "BHD": Decimal("2.65"),
    "OMR": Decimal("2.60"),
    "ILS": Decimal("0.27"),
    "TRY": Decimal("0.030"),
    "EGP": Decimal("0.021"),
    "ZAR": Decimal("0.054"),
    "NGN": Decimal("0.00065"),
    "KES": Decimal("0.0077"),
    "GHS": Decimal("0.065"),
    "MAD": Decimal("0.10"),
    "TND": Decimal("0.32"),
    "BRL": Decimal("0.19"),
    "MXN": Decimal("0.055"),
    "ARS": Decimal("0.0011"),
    "CLP": Decimal("0.0011"),
    "COP": Decimal("0.00025"),
    "PEN": Decimal("0.27"),
    "UYU": Decimal("0.025"),
    "SEK": Decimal("0.095"),
    "NOK": Decimal("0.093"),
    "DKK": Decimal("0.145"),
    "PLN": Decimal("0.25"),
    "CZK": Decimal("0.043"),
    "HUF": Decimal("0.0028"),
    "RON": Decimal("0.22"),
    "BGN": Decimal("0.55"),
    "UAH": Decimal("0.024"),
    "RUB": Decimal("0.011"),
    "IDR": Decimal("0.000062"),
    "MYR": Decimal("0.22"),
    "THB": Decimal("0.028"),
    "PHP": Decimal("0.0175"),
    "VND": Decimal("0.000039"),
}

# Symbols and words -> ISO code.  Ambiguous symbols ($, kr) are resolved with
# document context by the parser; the bare default is the most common reading.
SYMBOL_TO_CURRENCY: dict[str, str] = {
    "$": "USD", "us$": "USD", "usd": "USD", "u.s.$": "USD",
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "£": "GBP", "gbp": "GBP", "pound": "GBP", "pounds": "GBP", "sterling": "GBP",
    "₹": "INR", "inr": "INR", "rs": "INR", "rs.": "INR", "rupees": "INR", "rupee": "INR",
    "₨": "PKR", "pkr": "PKR",
    "aed": "AED", "dh": "AED", "dhs": "AED", "dirham": "AED", "dirhams": "AED",
    "sar": "SAR", "qar": "QAR", "kwd": "KWD", "bhd": "BHD", "omr": "OMR",
    "s$": "SGD", "sgd": "SGD", "sg$": "SGD",
    "a$": "AUD", "aud": "AUD", "au$": "AUD",
    "c$": "CAD", "cad": "CAD", "ca$": "CAD",
    "nz$": "NZD", "nzd": "NZD",
    "hk$": "HKD", "hkd": "HKD",
    "r$": "BRL", "brl": "BRL", "reais": "BRL",
    "mxn": "MXN", "mx$": "MXN",
    "¥": "JPY", "jpy": "JPY", "yen": "JPY",
    "cny": "CNY", "rmb": "CNY", "yuan": "CNY",
    "krw": "KRW", "₩": "KRW",
    "₪": "ILS", "ils": "ILS", "nis": "ILS", "shekel": "ILS", "shekels": "ILS",
    "₺": "TRY", "try": "TRY", "lira": "TRY",
    "₦": "NGN", "ngn": "NGN", "naira": "NGN",
    "ksh": "KES", "kes": "KES", "shilling": "KES", "shillings": "KES",
    "zar": "ZAR", "rand": "ZAR",
    "egp": "EGP", "ghs": "GHS", "mad": "MAD", "tnd": "TND",
    "sek": "SEK", "nok": "NOK", "dkk": "DKK",
    "pln": "PLN", "zł": "PLN", "zloty": "PLN",
    "czk": "CZK", "huf": "HUF", "ron": "RON", "bgn": "BGN",
    "uah": "UAH", "₴": "UAH", "rub": "RUB", "₽": "RUB",
    "idr": "IDR", "rp": "IDR", "rupiah": "IDR",
    "myr": "MYR", "rm": "MYR", "ringgit": "MYR",
    "thb": "THB", "฿": "THB", "baht": "THB",
    "php": "PHP", "₱": "PHP", "peso": "PHP", "pesos": "PHP",
    "vnd": "VND", "₫": "VND", "dong": "VND",
    "chf": "CHF", "twd": "TWD", "clp": "CLP", "cop": "COP", "pen": "PEN", "ars": "ARS",
    "lkr": "LKR", "bdt": "BDT", "npr": "NPR", "uyu": "UYU", "tl": "TRY",
    # Spelled-out names, which local coverage uses more often than ISO codes.
    "dollars": "USD", "dollar": "USD",
    "real": "BRL",
    "złotych": "PLN", "zlotych": "PLN", "złoty": "PLN",
    "kronor": "SEK", "kroner": "NOK", "kronen": "DKK",
    "francs": "CHF", "franken": "CHF",
    "riyals": "SAR", "riyal": "SAR",
    "soles": "PEN",
    "liras": "TRY",
    "won": "KRW", "taka": "BDT",
    "forint": "HUF", "koruna": "CZK", "hryvnia": "UAH", "rubles": "RUB", "rouble": "RUB",
    "lei": "RON",
}

# Currency words several countries share. "3 million pesos" could be Mexican
# (~$165k) or Colombian (~$750) - a 200x spread - so these are resolved from the
# surrounding country context, and dropped when there is none, rather than
# silently taking whichever meaning happens to be listed first.
AMBIGUOUS_CURRENCY_TOKENS: set[str] = {
    "peso", "pesos", "rupee", "rupees", "rs", "rs.", "krona", "kronor", "krone", "kroner",
    "kronen", "franc", "francs", "franken", "shilling", "shillings", "riyal", "riyals",
    "dinar", "dinars", "pound", "pounds", "lira", "liras", "real",
}

# "$" and "dollars" stay USD by default: international funding coverage writes
# US dollars far more often than not, and a local reading is still preferred
# whenever the text names a country that uses its own dollar.
DOLLAR_TOKENS: set[str] = {"$", "dollar", "dollars"}


# Country / TLD hints used to disambiguate a bare "$" or a plain number.
# Coverage says "a Mexican startup" at least as often as "a startup in Mexico",
# so demonyms resolve a currency just like country names do.
DEMONYM_CURRENCY: dict[str, str] = {
    "indian": "INR", "pakistani": "PKR", "bangladeshi": "BDT", "sri lankan": "LKR", "nepali": "NPR",
    "emirati": "AED", "saudi arabian": "SAR", "qatari": "QAR", "kuwaiti": "KWD",
    "bahraini": "BHD", "omani": "OMR", "israeli": "ILS", "turkish": "TRY",
    "singaporean": "SGD", "australian": "AUD", "new zealander": "NZD", "canadian": "CAD",
    "japanese": "JPY", "chinese": "CNY", "korean": "KRW", "taiwanese": "TWD",
    "brazilian": "BRL", "mexican": "MXN", "chilean": "CLP", "colombian": "COP",
    "peruvian": "PEN", "argentine": "ARS", "argentinian": "ARS", "uruguayan": "UYU",
    "nigerian": "NGN", "kenyan": "KES", "south african": "ZAR", "ghanaian": "GHS",
    "egyptian": "EGP", "moroccan": "MAD", "tunisian": "TND",
    "swedish": "SEK", "norwegian": "NOK", "danish": "DKK", "polish": "PLN",
    "czech": "CZK", "hungarian": "HUF", "romanian": "RON", "bulgarian": "BGN",
    "ukrainian": "UAH", "russian": "RUB", "swiss": "CHF",
    "indonesian": "IDR", "malaysian": "MYR", "thai": "THB", "filipino": "PHP",
    "philippine": "PHP", "vietnamese": "VND",
    "british": "GBP", "english": "GBP", "scottish": "GBP", "welsh": "GBP",
    # eurozone
    "french": "EUR", "german": "EUR", "dutch": "EUR", "spanish": "EUR", "italian": "EUR",
    "portuguese": "EUR", "irish": "EUR", "belgian": "EUR", "austrian": "EUR",
    "finnish": "EUR", "greek": "EUR", "estonian": "EUR", "latvian": "EUR",
    "lithuanian": "EUR", "slovak": "EUR", "slovenian": "EUR", "croatian": "EUR",
}

COUNTRY_DEFAULT_CURRENCY: dict[str, str] = {
    "india": "INR", "pakistan": "PKR", "bangladesh": "BDT", "sri lanka": "LKR", "nepal": "NPR",
    "uae": "AED", "united arab emirates": "AED", "dubai": "AED", "abu dhabi": "AED",
    "saudi": "SAR", "qatar": "QAR", "kuwait": "KWD", "bahrain": "BHD", "oman": "OMR",
    "singapore": "SGD", "australia": "AUD", "new zealand": "NZD", "canada": "CAD",
    "hong kong": "HKD", "japan": "JPY", "china": "CNY", "south korea": "KRW", "taiwan": "TWD",
    "brazil": "BRL", "mexico": "MXN", "chile": "CLP", "colombia": "COP", "peru": "PEN", "argentina": "ARS",
    "nigeria": "NGN", "kenya": "KES", "south africa": "ZAR", "ghana": "GHS", "egypt": "EGP",
    "morocco": "MAD", "tunisia": "TND", "israel": "ILS", "turkey": "TRY",
    "sweden": "SEK", "norway": "NOK", "denmark": "DKK", "poland": "PLN", "czech": "CZK",
    "hungary": "HUF", "romania": "RON", "bulgaria": "BGN", "ukraine": "UAH",
    "indonesia": "IDR", "malaysia": "MYR", "thailand": "THB", "philippines": "PHP", "vietnam": "VND",
    "switzerland": "CHF", "uruguay": "UYU",
    # Cities, because a funding story names the city far more often than the
    # country - and for the shared-name currencies the city is what settles it.
    "bengaluru": "INR", "bangalore": "INR", "mumbai": "INR", "new delhi": "INR",
    "gurugram": "INR", "gurgaon": "INR", "noida": "INR", "hyderabad": "INR",
    "pune": "INR", "chennai": "INR", "kolkata": "INR", "ahmedabad": "INR",
    "karachi": "PKR", "lahore": "PKR", "islamabad": "PKR",
    "dhaka": "BDT", "colombo": "LKR", "kathmandu": "NPR",
    "stockholm": "SEK", "gothenburg": "SEK", "oslo": "NOK", "copenhagen": "DKK",
    "mexico city": "MXN", "guadalajara": "MXN", "monterrey": "MXN",
    "bogota": "COP", "bogotá": "COP", "medellin": "COP", "medellín": "COP",
    "lima": "PEN", "santiago": "CLP", "buenos aires": "ARS", "montevideo": "UYU",
    "sao paulo": "BRL", "são paulo": "BRL", "rio de janeiro": "BRL",
    "lagos": "NGN", "abuja": "NGN", "nairobi": "KES", "accra": "GHS",
    "cape town": "ZAR", "johannesburg": "ZAR", "cairo": "EGP", "casablanca": "MAD",
    "istanbul": "TRY", "ankara": "TRY", "tel aviv": "ILS",
    "jakarta": "IDR", "kuala lumpur": "MYR", "bangkok": "THB", "manila": "PHP",
    "ho chi minh city": "VND", "hanoi": "VND", "zurich": "CHF", "geneva": "CHF",
    "warsaw": "PLN", "krakow": "PLN", "kraków": "PLN", "prague": "CZK", "budapest": "HUF",
}

# Multipliers, including the Indian numbering system which trips up naive parsers.
MULTIPLIERS: dict[str, Decimal] = {
    "k": Decimal("1e3"), "thousand": Decimal("1e3"),
    "m": Decimal("1e6"), "mn": Decimal("1e6"), "mm": Decimal("1e6"), "mio": Decimal("1e6"),
    # "million" in the languages this agent actually reads. Non-English funding
    # coverage is the point of the exercise, so these are spelled out rather
    # than left to a lucky prefix match.
    "million": Decimal("1e6"), "millions": Decimal("1e6"),
    "millionen": Decimal("1e6"), "millionene": Decimal("1e6"),
    "millón": Decimal("1e6"), "millones": Decimal("1e6"),
    "milhão": Decimal("1e6"), "milhões": Decimal("1e6"),
    "milione": Decimal("1e6"), "milioni": Decimal("1e6"),
    "miljoen": Decimal("1e6"), "miljoenen": Decimal("1e6"),
    "miljon": Decimal("1e6"), "miljoner": Decimal("1e6"), "millioner": Decimal("1e6"),
    "miljoona": Decimal("1e6"), "miljoonaa": Decimal("1e6"),
    "milion": Decimal("1e6"), "miliony": Decimal("1e6"), "milionów": Decimal("1e6"),
    "milyon": Decimal("1e6"), "juta": Decimal("1e6"), "triệu": Decimal("1e6"),
    "مليون": Decimal("1e6"),
    "bn": Decimal("1e9"), "b": Decimal("1e9"), "billion": Decimal("1e9"), "billions": Decimal("1e9"),
    "milliarde": Decimal("1e9"), "milliarden": Decimal("1e9"), "miliardi": Decimal("1e9"),
    "mil millones": Decimal("1e9"), "milyar": Decimal("1e9"), "miliar": Decimal("1e9"),
    "lakh": Decimal("1e5"), "lakhs": Decimal("1e5"), "lac": Decimal("1e5"), "lacs": Decimal("1e5"),
    "crore": Decimal("1e7"), "crores": Decimal("1e7"), "cr": Decimal("1e7"),
}


class UnknownCurrency(Exception):
    pass


def to_usd(amount: Decimal, currency: str) -> tuple[Decimal, Decimal]:
    """Return ``(usd_amount, rate_used)``; raises for an unsupported currency."""
    code = currency.upper()
    rate = FX_USD_PER_UNIT.get(code)
    if rate is None:
        raise UnknownCurrency(code)
    return (amount * rate, rate)


def guess_currency_from_context(text: str) -> str | None:
    """Infer a currency from a country or nationality named nearby."""
    import re as _re

    low = text.lower()
    # Longest names first so "saudi arabian" is not shadowed by "saudi".
    for table in (COUNTRY_DEFAULT_CURRENCY, DEMONYM_CURRENCY):
        for key in sorted(table, key=len, reverse=True):
            if _re.search(rf"\b{_re.escape(key)}\b", low):
                return table[key]
    return None
