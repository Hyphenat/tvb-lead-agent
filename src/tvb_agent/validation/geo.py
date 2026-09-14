"""Country identification and US-signal vocabulary."""

from __future__ import annotations

import re

US_NAMES = {
    "united states", "united states of america", "usa", "u.s.a.", "u.s.", "us", "america",
}

US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming", "district of columbia",
}

US_CITIES = {
    "new york", "san francisco", "los angeles", "chicago", "boston", "seattle", "austin",
    "denver", "miami", "atlanta", "dallas", "houston", "san diego", "philadelphia", "phoenix",
    "san jose", "palo alto", "mountain view", "menlo park", "santa monica", "brooklyn",
    "washington dc", "silicon valley", "bay area", "nyc", "sf",
}

# ccTLD -> country. Only unambiguous mappings; .co and .io are omitted on purpose.
TLD_COUNTRY: dict[str, str] = {
    "in": "India", "uk": "United Kingdom", "fr": "France", "de": "Germany", "es": "Spain",
    "it": "Italy", "nl": "Netherlands", "be": "Belgium", "ch": "Switzerland", "at": "Austria",
    "se": "Sweden", "no": "Norway", "dk": "Denmark", "fi": "Finland", "ie": "Ireland",
    "pt": "Portugal", "pl": "Poland", "cz": "Czech Republic", "sk": "Slovakia", "hu": "Hungary",
    "ro": "Romania", "bg": "Bulgaria", "gr": "Greece", "tr": "Turkey", "ua": "Ukraine",
    "ee": "Estonia", "lv": "Latvia", "lt": "Lithuania", "si": "Slovenia", "hr": "Croatia",
    "rs": "Serbia", "is": "Iceland", "lu": "Luxembourg", "mt": "Malta", "cy": "Cyprus",
    "ca": "Canada", "mx": "Mexico", "br": "Brazil", "ar": "Argentina", "cl": "Chile",
    "pe": "Peru", "uy": "Uruguay", "ec": "Ecuador",
    "au": "Australia", "nz": "New Zealand", "sg": "Singapore", "my": "Malaysia",
    "id": "Indonesia", "th": "Thailand", "vn": "Vietnam", "ph": "Philippines",
    "hk": "Hong Kong", "tw": "Taiwan", "jp": "Japan", "kr": "South Korea", "cn": "China",
    "ae": "United Arab Emirates", "sa": "Saudi Arabia", "qa": "Qatar", "kw": "Kuwait",
    "bh": "Bahrain", "om": "Oman", "il": "Israel", "jo": "Jordan", "lb": "Lebanon",
    "eg": "Egypt", "ma": "Morocco", "tn": "Tunisia", "ng": "Nigeria", "ke": "Kenya",
    "za": "South Africa", "gh": "Ghana", "tz": "Tanzania", "ug": "Uganda", "rw": "Rwanda",
    "pk": "Pakistan", "bd": "Bangladesh", "lk": "Sri Lanka", "np": "Nepal",
}

CITY_COUNTRY: dict[str, str] = {
    "bengaluru": "India", "bangalore": "India", "mumbai": "India", "delhi": "India",
    "new delhi": "India", "gurugram": "India", "gurgaon": "India", "noida": "India",
    "hyderabad": "India", "pune": "India", "chennai": "India", "kolkata": "India",
    "ahmedabad": "India", "jaipur": "India", "kochi": "India", "indore": "India",
    "london": "United Kingdom", "manchester": "United Kingdom", "edinburgh": "United Kingdom",
    "bristol": "United Kingdom", "cambridge": "United Kingdom", "oxford": "United Kingdom",
    "leeds": "United Kingdom", "glasgow": "United Kingdom", "birmingham": "United Kingdom",
    "paris": "France", "lyon": "France", "toulouse": "France", "marseille": "France",
    "bordeaux": "France", "nantes": "France", "lille": "France",
    "berlin": "Germany", "munich": "Germany", "münchen": "Germany", "hamburg": "Germany",
    "cologne": "Germany", "frankfurt": "Germany", "stuttgart": "Germany",
    "amsterdam": "Netherlands", "rotterdam": "Netherlands", "utrecht": "Netherlands",
    "eindhoven": "Netherlands", "the hague": "Netherlands", "delft": "Netherlands",
    "madrid": "Spain", "barcelona": "Spain", "valencia": "Spain", "seville": "Spain",
    "milan": "Italy", "rome": "Italy", "turin": "Italy", "bologna": "Italy",
    "lisbon": "Portugal", "porto": "Portugal",
    "dublin": "Ireland", "cork": "Ireland",
    "zurich": "Switzerland", "geneva": "Switzerland", "lausanne": "Switzerland", "basel": "Switzerland",
    "vienna": "Austria", "graz": "Austria",
    "brussels": "Belgium", "ghent": "Belgium", "antwerp": "Belgium", "leuven": "Belgium",
    "stockholm": "Sweden", "gothenburg": "Sweden", "malmo": "Sweden", "malmö": "Sweden",
    "copenhagen": "Denmark", "aarhus": "Denmark",
    "oslo": "Norway", "bergen": "Norway",
    "helsinki": "Finland", "espoo": "Finland", "tampere": "Finland",
    "warsaw": "Poland", "krakow": "Poland", "kraków": "Poland", "wroclaw": "Poland", "gdansk": "Poland",
    "prague": "Czech Republic", "brno": "Czech Republic",
    "budapest": "Hungary", "bucharest": "Romania", "cluj": "Romania", "sofia": "Bulgaria",
    "tallinn": "Estonia", "riga": "Latvia", "vilnius": "Lithuania", "ljubljana": "Slovenia",
    "zagreb": "Croatia", "belgrade": "Serbia", "athens": "Greece", "thessaloniki": "Greece",
    "istanbul": "Turkey", "ankara": "Turkey", "izmir": "Turkey",
    "kyiv": "Ukraine", "kiev": "Ukraine", "lviv": "Ukraine",
    "dubai": "United Arab Emirates", "abu dhabi": "United Arab Emirates", "sharjah": "United Arab Emirates",
    "riyadh": "Saudi Arabia", "jeddah": "Saudi Arabia", "doha": "Qatar", "kuwait city": "Kuwait",
    "manama": "Bahrain", "muscat": "Oman", "amman": "Jordan", "beirut": "Lebanon",
    "tel aviv": "Israel", "jerusalem": "Israel", "haifa": "Israel", "herzliya": "Israel",
    "cairo": "Egypt", "alexandria": "Egypt", "casablanca": "Morocco", "rabat": "Morocco",
    "tunis": "Tunisia", "lagos": "Nigeria", "abuja": "Nigeria", "nairobi": "Kenya",
    "cape town": "South Africa", "johannesburg": "South Africa", "pretoria": "South Africa",
    "durban": "South Africa", "accra": "Ghana", "kampala": "Uganda", "kigali": "Rwanda",
    "dar es salaam": "Tanzania",
    "karachi": "Pakistan", "lahore": "Pakistan", "islamabad": "Pakistan",
    "dhaka": "Bangladesh", "colombo": "Sri Lanka", "kathmandu": "Nepal",
    "singapore": "Singapore", "kuala lumpur": "Malaysia", "penang": "Malaysia",
    "jakarta": "Indonesia", "bandung": "Indonesia", "surabaya": "Indonesia",
    "bangkok": "Thailand", "chiang mai": "Thailand",
    "hanoi": "Vietnam", "ho chi minh city": "Vietnam", "saigon": "Vietnam", "da nang": "Vietnam",
    "manila": "Philippines", "cebu": "Philippines",
    "hong kong": "Hong Kong", "taipei": "Taiwan", "tokyo": "Japan", "osaka": "Japan",
    "seoul": "South Korea", "busan": "South Korea",
    "shanghai": "China", "beijing": "China", "shenzhen": "China", "hangzhou": "China",
    "sydney": "Australia", "melbourne": "Australia", "brisbane": "Australia", "perth": "Australia",
    "adelaide": "Australia", "canberra": "Australia",
    "auckland": "New Zealand", "wellington": "New Zealand", "christchurch": "New Zealand",
    "toronto": "Canada", "vancouver": "Canada", "montreal": "Canada", "ottawa": "Canada",
    "calgary": "Canada", "waterloo": "Canada",
    "sao paulo": "Brazil", "são paulo": "Brazil", "rio de janeiro": "Brazil",
    "belo horizonte": "Brazil", "florianopolis": "Brazil", "curitiba": "Brazil",
    "mexico city": "Mexico", "guadalajara": "Mexico", "monterrey": "Mexico",
    "bogota": "Colombia", "bogotá": "Colombia", "medellin": "Colombia", "medellín": "Colombia",
    "santiago": "Chile", "buenos aires": "Argentina", "lima": "Peru", "montevideo": "Uruguay",
    "quito": "Ecuador", "san jose costa rica": "Costa Rica", "panama city": "Panama",
}

# Countries whose names are unambiguous enough to match in free text.
COUNTRY_NAMES = sorted(set(TLD_COUNTRY.values()) | set(CITY_COUNTRY.values()))


def is_us_country(name: str | None) -> bool:
    return bool(name) and name.strip().lower() in US_NAMES


def country_from_tld(domain: str | None) -> str | None:
    if not domain:
        return None
    parts = domain.lower().strip().split(".")
    if len(parts) < 2:
        return None
    # Handle both example.in and example.co.in
    for tld in (parts[-1], ".".join(parts[-2:])):
        c = TLD_COUNTRY.get(tld.split(".")[-1])
        if c:
            return c
    return None


# "us" is the commonest word on the web that also spells a country. Scanning for
# it plainly made "contact us", "join us" and "about us" - on every site on earth -
# read as a United States mention, and the US-presence gate is the one that
# decides whether a non-US company is eligible at all. These sets separate the
# pronoun from the country by the company it keeps.
_US_PRONOUN_BEFORE = {
    "contact", "about", "join", "with", "email", "mail", "follow", "reach", "tell",
    "ask", "help", "let", "for", "to", "of", "near", "like", "give", "show", "call",
    "text", "meet", "trust", "choose", "hire", "support", "thank", "visit", "message",
    "write", "and", "why", "at", "by", "from", "between", "among", "all", "made",
}
_US_PRONOUN_AFTER = {
    "today", "now", "on", "at", "via", "here", "if", "a", "an", "and", "know",
    "directly", "anytime", "your", "you", "build", "grow", "help", "so", "what",
    "how", "why", "or", "for", "to", "with", "about", "improve", "understand",
}
# "US" as the country, in the shapes it actually appears in: an abbreviation with
# stops, or the bare token next to something it can only be modifying.
_US_ABBREV = re.compile(r"(?<![A-Za-z.])U\.S\.?A?\.?(?![A-Za-z])")
_US_QUALIFIED = re.compile(
    r"\b(?:the\s+us\b|us[- ](?:based|market|markets|office|offices|customers|clients|"
    r"headquarters|hq|subsidiary|entity|entities|incorporated|incorporation|company|"
    r"corporation|dollars?|team|operations|expansion|launch|residents?|users?|state|"
    r"states|federal|citizens?)\b)")
_AMERICA = re.compile(r"(?<!latin )(?<!south )(?<!north )(?<!central )\bamerica\b")
# A US state whose name is also a sovereign country. It only counts as a US
# signal in company with another one.
_AMBIGUOUS_STATES = {"georgia"}


def _bare_us_is_the_country(low: str) -> bool:
    """Is a lowercase, unpunctuated "us" the country rather than the pronoun?"""
    for m in re.finditer(r"\bus\b", low):
        before = low[max(0, m.start() - 24):m.start()].split()
        after = low[m.end():m.end() + 24].split()
        prev = before[-1].strip(".,:;!?\u2019'\"") if before else ""
        nxt = after[0].strip(".,:;!?\u2019'\"") if after else ""
        if prev in _US_PRONOUN_BEFORE or nxt in _US_PRONOUN_AFTER:
            continue          # "contact us", "us today" - the pronoun
        if prev in ("the", "in", "across", "throughout") or nxt in (
                "market", "based", "office", "offices", "customers", "subsidiary"):
            return True
    return False


def mentions_the_united_states(text: str) -> bool:
    """Does this text name the United States - as a country, not as a pronoun?"""
    raw = text or ""
    low = raw.lower()
    if not low:
        return False
    for name in ("united states of america", "united states", "u.s.a.", "usa"):
        if re.search(rf"\b{re.escape(name)}\b", low):
            return True
    if _US_ABBREV.search(raw) or _US_QUALIFIED.search(low) or _AMERICA.search(low):
        return True
    return _bare_us_is_the_country(low)


def _us_place_in(text: str) -> bool:
    """Does this phrase name the United States, or a US state or city?

    CITY_COUNTRY and TLD_COUNTRY contain no US entries, so every other lookup in
    this module is structurally blind to the United States: "Austin, Texas" and
    "San Francisco, California" resolved to nothing, and "based in London and New
    York" resolved to the United Kingdom - a New York company passing the
    US-presence gate with a rationale asserting it had no US footprint.
    """
    low = (text or "").lower()
    if mentions_the_united_states(text or ""):
        return True
    if any(re.search(rf"\b{re.escape(c)}\b", low) for c in US_CITIES):
        return True
    plain = {st for st in US_STATES if st not in _AMBIGUOUS_STATES}
    if any(re.search(rf"\b{re.escape(st)}\b", low) for st in plain):
        return True
    # Georgia is a country as well as a state: it needs a second US signal.
    return any(
        re.search(rf"\b{re.escape(st)}\b", low) and (
            re.search(r"\b(?:usa|u\.s\.a?\.?|united states|atlanta|savannah)\b", low))
        for st in _AMBIGUOUS_STATES
    )


def country_from_city(text: str | None) -> tuple[str | None, str | None]:
    """Return ``(country, matched_city)`` for the first known city named."""
    # The US is checked first: a phrase naming both ("London and New York") must
    # not resolve to the other country.
    if _us_place_in(text or ""):
        return "United States", None
    if not text:
        return None, None
    low = text.lower()
    for city in sorted(CITY_COUNTRY, key=len, reverse=True):
        if re.search(rf"\b{re.escape(city)}\b", low):
            return CITY_COUNTRY[city], city
    return None, None


def country_from_text(text: str | None) -> str | None:
    if not text:
        return None
    # The US first, for the same reason: it is absent from COUNTRY_NAMES, so
    # every other branch here is blind to it.
    if _us_place_in(text):
        return "United States"
    low = text.lower()
    for name in sorted(COUNTRY_NAMES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name.lower())}\b", low):
            return name
    if any(re.search(rf"\b{re.escape(u)}\b", low) for u in ("united states", "usa", "u.s.")):
        return "United States"
    return None


def normalise_country(raw: str | None) -> str | None:
    if not raw:
        return None
    r = raw.strip().strip(".,")
    if r.lower() in US_NAMES:
        return "United States"
    for name in COUNTRY_NAMES:
        if r.lower() == name.lower():
            return name
    # A US state or city given where a country was expected still means the US.
    if r.lower() in US_STATES or r.lower() in US_CITIES:
        return "United States"
    c, _ = country_from_city(r)
    if c:
        return c
    # Anything else is not a country. A real run produced "Non-US headquarters
    # (North)" - a sentence fragment treated as a country, which is exactly how
    # an unverifiable company slips past the US-presence gate.
    return None


def mentions_us_location(text: str) -> bool:
    low = (text or "").lower()
    if any(re.search(rf"\b{re.escape(n)}\b", low) for n in ("united states", "usa", "u.s.", "u.s.a")):
        return True
    return any(re.search(rf"\b{re.escape(c)}\b", low) for c in US_CITIES)

# Press writes "a Kenyan AI startup" and "the Jakarta-based company" far more
# often than "headquartered in Kenya". Run 7 rejected real non-US companies for
# having no establishable country while the country sat in the first four words
# of the headline that found them.
DEMONYM_COUNTRY: dict[str, str] = {
    "indian": "India", "pakistani": "Pakistan", "bangladeshi": "Bangladesh",
    "sri lankan": "Sri Lanka", "nepali": "Nepal", "emirati": "United Arab Emirates",
    "saudi": "Saudi Arabia", "saudi arabian": "Saudi Arabia", "qatari": "Qatar",
    "kuwaiti": "Kuwait", "bahraini": "Bahrain", "omani": "Oman", "jordanian": "Jordan",
    "lebanese": "Lebanon", "israeli": "Israel", "turkish": "Turkey",
    "singaporean": "Singapore", "australian": "Australia", "canadian": "Canada",
    "japanese": "Japan", "chinese": "China", "korean": "South Korea",
    "south korean": "South Korea", "taiwanese": "Taiwan",
    "brazilian": "Brazil", "mexican": "Mexico", "chilean": "Chile",
    "colombian": "Colombia", "peruvian": "Peru", "argentine": "Argentina",
    "argentinian": "Argentina", "uruguayan": "Uruguay",
    "nigerian": "Nigeria", "kenyan": "Kenya", "south african": "South Africa",
    "ghanaian": "Ghana", "egyptian": "Egypt", "moroccan": "Morocco",
    "tunisian": "Tunisia", "ethiopian": "Ethiopia", "ugandan": "Uganda",
    "rwandan": "Rwanda", "tanzanian": "Tanzania", "senegalese": "Senegal",
    "swedish": "Sweden", "norwegian": "Norway", "danish": "Denmark",
    "finnish": "Finland", "polish": "Poland", "czech": "Czech Republic",
    "hungarian": "Hungary", "romanian": "Romania", "bulgarian": "Bulgaria",
    "ukrainian": "Ukraine", "estonian": "Estonia", "latvian": "Latvia",
    "lithuanian": "Lithuania", "slovak": "Slovakia", "slovenian": "Slovenia",
    "croatian": "Croatia", "serbian": "Serbia", "greek": "Greece",
    "swiss": "Switzerland", "austrian": "Austria", "belgian": "Belgium",
    "dutch": "Netherlands", "french": "France", "german": "Germany",
    "spanish": "Spain", "italian": "Italy", "portuguese": "Portugal",
    "irish": "Ireland", "british": "United Kingdom", "scottish": "United Kingdom",
    "welsh": "United Kingdom", "indonesian": "Indonesia", "malaysian": "Malaysia",
    "thai": "Thailand", "filipino": "Philippines", "philippine": "Philippines",
    "vietnamese": "Vietnam", "cambodian": "Cambodia", "burmese": "Myanmar",
    "new zealand": "New Zealand", "american": "United States",
}


# Words that turn a demonym into a claim about a *company* rather than a stray
# mention of a nationality. "Little Talk in Slow French" is a podcast title on a
# hosting platform's home page; "the French fintech startup" locates a business.
# Without this anchor the agent read the first as a headquarters and shipped a
# US-owned company as French.
_ORG_NOUN = (r"(?:start-?ups?|scale-?ups?|compan(?:y|ies)|firms?|businesses|business|ventures?|"
             r"platforms?|providers?|vendors?|makers?|developers?|studios?|agenc(?:y|ies)|"
             r"groups?|teams?|founders?|co-?founders?|entrepreneurs?|fintechs?|proptech|"
             r"insurtech|healthtech|edtech|adtech|deeptech|marketplaces?|unicorns?|saas|"
             r"software|technology|tech|banks?|lenders?|retailers?|operators?|"
             r"manufacturers?|brands?|enterprises?|corporations?|subsidiar(?:y|ies)|"
             r"maker|outfit|venture-backed)")


def demonym_describes_a_company(text: str | None, demonym: str) -> bool:
    """Does this demonym modify a company, or is it just a word on the page?

    Accepts "Kenyan AI startup", "the French fintech", "Estonian-based" - the
    demonym attached to an organisation - and rejects a nationality that merely
    occurs in a title, a language name or a list of content.
    """
    low = (text or "").lower()
    d = re.escape(demonym)
    # "Estonian-based", "French-headquartered"
    if re.search(rf"\b{d}[- ](?:based|headquartered|owned|founded|registered)\b", low):
        return True
    # "the French startup", "Kenyan AI-driven lending startup" - up to three
    # words of qualifier between the demonym and the thing it describes.
    return bool(
        re.search(rf"\b{d}\b(?:[ -][a-z0-9&.\u2019'-]{{1,18}}){{0,3}}[ -]{_ORG_NOUN}\b", low))


def country_from_demonym(text: str | None, *, must_describe_a_company: bool = False) -> str | None:
    """"a Kenyan fintech startup" -> Kenya.

    With ``must_describe_a_company`` the demonym must actually modify a company;
    a nationality appearing anywhere in the text is not enough.
    """
    # The US is checked first: a phrase naming both ("London and New York")
    # must not resolve to the other country.
    if _us_place_in(text or ""):
        return "United States"
    low = (text or "").lower()
    if not low:
        return None
    # Longest first, so "south korean" is not read as "korean".
    for demonym in sorted(DEMONYM_COUNTRY, key=len, reverse=True):
        if re.search(rf"\b{re.escape(demonym)}\b", low):
            if must_describe_a_company and not demonym_describes_a_company(low, demonym):
                continue
            return DEMONYM_COUNTRY[demonym]
    return None
