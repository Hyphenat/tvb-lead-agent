"""TVB fit score - advisory ranking, never a qualification criterion.

The brief's four requirements are a filter; they do not say which of two
qualifying companies TVB should call first.  This score encodes the preferences
stated in the TVB overview: the Orbit sectors, the Hub geographies, the
seed-to-Series-A stage, and a product with signs of real traction.
"""

from __future__ import annotations

from ..models import CompanyProfile

ORBIT_SECTORS = {
    "healthcare": 1.0, "health": 1.0, "healthtech": 1.0, "digital health": 1.0, "medtech": 0.9,
    "education": 1.0, "edtech": 1.0, "workforce": 0.9, "learning": 0.9,
    "ai": 1.0, "artificial intelligence": 1.0, "machine learning": 0.95, "automation": 0.9,
    "cybersecurity": 1.0, "security": 0.9, "compliance": 0.85,
    "digital twin": 1.0, "simulation": 0.9, "iot": 0.85,
    "fintech": 1.0, "payments": 1.0, "embedded finance": 1.0, "lending": 0.9, "insurtech": 0.85,
    "travel": 1.0, "traveltech": 1.0, "mobility": 0.8,
    "saas": 0.95, "b2b": 0.9, "b2b2c": 0.9, "enterprise software": 0.9, "marketplace": 0.8,
}

HUB_COUNTRIES = {
    "United Kingdom": 1.0, "France": 1.0, "India": 1.0, "United Arab Emirates": 1.0,
    "Singapore": 0.9, "Pakistan": 0.85,
    "Germany": 0.8, "Netherlands": 0.8, "Spain": 0.75, "Ireland": 0.75, "Switzerland": 0.75,
    "Sweden": 0.75, "Denmark": 0.7, "Norway": 0.7, "Finland": 0.7, "Poland": 0.7,
    "Brazil": 0.8, "Mexico": 0.8, "Colombia": 0.75, "Chile": 0.7, "Argentina": 0.7,
    "Nigeria": 0.8, "Kenya": 0.8, "South Africa": 0.75, "Egypt": 0.7,
    "Indonesia": 0.75, "Vietnam": 0.75, "Malaysia": 0.7, "Philippines": 0.7, "Thailand": 0.7,
    "Bangladesh": 0.7, "Sri Lanka": 0.65, "Turkey": 0.7, "Israel": 0.7,
    "Australia": 0.6, "New Zealand": 0.55, "Canada": 0.55, "Japan": 0.5, "South Korea": 0.5,
}


def tvb_fit_score(company: CompanyProfile) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.0

    sector_blob = " ".join(filter(None, [
        (company.sector.value or ""), (company.description.value or "")
    ])).lower()
    sector_hit = 0.0
    matched: str | None = None
    for key, weight in ORBIT_SECTORS.items():
        if key in sector_blob and weight > sector_hit:
            sector_hit, matched = weight, key
    if matched:
        reasons.append(f"Sector '{matched}' aligns with a TVB Orbit.")
    score += 0.40 * sector_hit

    country = company.country.value
    geo = HUB_COUNTRIES.get(country or "", 0.45 if country else 0.0)
    if country and geo >= 0.85:
        reasons.append(f"{country} is a TVB Hub geography.")
    elif country:
        reasons.append(f"{country} is outside the US and reachable through TVB's network.")
    score += 0.30 * geo

    if company.funding.value:
        usd = float(company.funding.value.amount_usd)
        # Mid-band companies are the clearest "traction but needs scale" profile.
        stage = 1.0 if 1.5e6 <= usd <= 4.0e6 else 0.7
        reasons.append(f"{company.funding.value.human()} sits in TVB's seed-to-Series-A sweet spot."
                       if stage == 1.0 else "Funding is inside the band but near an edge.")
        score += 0.20 * stage

    if company.email.is_verified:
        score += 0.05
    if company.founder.value and company.founder.value.is_founder_or_ceo:
        score += 0.05
        reasons.append("Direct founder-level contact available.")

    return round(min(score, 1.0), 3), reasons
