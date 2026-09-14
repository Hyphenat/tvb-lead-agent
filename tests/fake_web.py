"""A small simulated web used by the integration tests.

Three companies: two should qualify, one must be rejected for having a US office.
The fixtures deliberately include traps - a valuation figure, a market-size
figure, a role email and a fabricated LLM claim - so the tests prove the guards
work rather than merely that the pipeline runs.
"""

ZETA_HOME = """<html><head><title>Zeta Care - Care coordination platform</title></head><body>
<h1>Zeta Care</h1>
<p>Zeta Care is a care coordination platform used by clinics across India. Our software
connects providers, patients and social care teams through a single dashboard and API.</p>
<a href="/about">About</a><a href="/team">Team</a><a href="/contact">Contact</a>
<a href="/careers">Careers</a><a href="/product">Product</a>
</body></html>"""

ZETA_ABOUT = """<html><head><title>About Zeta Care</title></head><body>
<p>Zeta Care is headquartered in Bengaluru, India. Founded in 2021, the company now serves
180 clinics. The global digital health market is worth $400 billion, and we intend to take
a meaningful share of it.</p>
<p>In March 2025 Zeta Care raised $2.4 million in a seed round led by Blume Ventures.</p>
</body></html>"""

ZETA_TEAM = """<html><head><title>Team - Zeta Care</title></head><body>
<p>Priya Raman, Co-founder &amp; CEO, leads the company. She previously built health systems
at a large hospital group.</p>
<p>Arjun Mehta, CTO, leads engineering.</p>
</body></html>"""

ZETA_CONTACT = """<html><head><title>Contact Zeta Care</title></head><body>
<p>For partnership enquiries contact Priya Raman at priya.raman [at] zetacare.in.</p>
<p>General enquiries: info@zetacare.in</p>
<p>Registered office: 4th Floor, Koramangala, Bengaluru 560034, India.</p>
</body></html>"""

ZETA_CAREERS = """<html><head><title>Careers - Zeta Care</title></head><body>
<p>We are hiring in Bengaluru and Pune. Open roles: Backend Engineer (Bengaluru),
Clinical Success Manager (Pune).</p></body></html>"""

ZETA_PRODUCT = """<html><head><title>Product - Zeta Care</title></head><body>
<p>The Zeta Care platform offers a referral engine, an API and an analytics dashboard.</p>
</body></html>"""

NOVA_HOME = """<html><head><title>Nova Pay - embedded payments</title></head><body>
<h1>Nova Pay</h1>
<p>Nova Pay is an embedded finance platform for marketplaces in Nigeria. Our API lets
platforms issue wallets and settle payouts.</p>
<a href="/about">About</a><a href="/team">Team</a><a href="/contact">Contact</a>
</body></html>"""

NOVA_ABOUT = """<html><head><title>About Nova Pay</title></head><body>
<p>Nova Pay is based in Lagos, Nigeria. The company raised $3.1 million in a pre-Series A
round in 2025. Nova Pay was valued at $28 million post-money after the round.</p>
</body></html>"""

NOVA_TEAM = """<html><head><title>Team</title></head><body>
<p>Chidi Okonkwo is the Founder and CEO of Nova Pay.</p>
<p>Contact Chidi Okonkwo directly at chidi.okonkwo@novapay.ng for partnership discussions.</p>
</body></html>"""

NOVA_CONTACT = """<html><head><title>Contact</title></head><body>
<p>Head office: 12 Admiralty Way, Lekki, Lagos, Nigeria. Email hello@novapay.ng.</p>
</body></html>"""

# --- the one that must be rejected: real US office ---
ORBIT_HOME = """<html><head><title>Orbit Sim - digital twin platform</title></head><body>
<h1>Orbit Sim</h1><p>Orbit Sim builds a digital twin simulation platform for factories.</p>
<a href="/about">About</a><a href="/team">Team</a><a href="/contact">Contact</a>
</body></html>"""

ORBIT_ABOUT = """<html><head><title>About Orbit Sim</title></head><body>
<p>Orbit Sim is headquartered in Warsaw, Poland and raised $2.2 million in seed funding in 2025.
We opened a New York office in 2024 to serve North American manufacturers.</p>
</body></html>"""

ORBIT_TEAM = """<html><head><title>Team</title></head><body>
<p>Marek Nowak, Co-founder and CEO, founded the company in 2020.
Reach him at marek.nowak@orbitsim.pl.</p></body></html>"""

ORBIT_CONTACT = """<html><head><title>Contact</title></head><body>
<p>Warsaw office: Prosta 51. New York office: 350 Fifth Avenue, New York, NY 10118.</p>
</body></html>"""

NEWS_ARTICLE = """<html><head><title>Indian healthtech Zeta Care raises $2.4M seed</title></head><body>
<p>Bengaluru-based Zeta Care has raised $2.4 million in a seed round led by Blume Ventures,
the company said on Tuesday. Zeta Care operates a care coordination platform for clinics.</p>
<p>Separately, Lagos-based Nova Pay raised $3.1 million in a pre-Series A round.</p>
</body></html>"""

PORTFOLIO_PAGE = """<html><head><title>Our Portfolio - Example Ventures</title></head><body>
<h1>Our Portfolio</h1><p>Companies we back across our cohort:</p>
<ul>
<li><a href="https://zetacare.in/">Zeta Care</a></li>
<li><a href="https://novapay.ng/">Nova Pay</a></li>
<li><a href="https://orbitsim.pl/">Orbit Sim</a></li>
<li><a href="https://heliosgrid.de/">Helios Grid</a></li>
<li><a href="https://techcrunch.com/tag/x">Press coverage</a></li>
</ul></body></html>"""

# --- founder email published only on an UNLINKED imprint page --------------
# Common in Germany/Austria/Switzerland, where an Impressum is legally required
# but is often not in the navigation. Exercises the founder-email hunt.
HELIOS_HOME = """<html><head><title>Helios Grid - energy management platform</title>
<meta name="description" content="Helios Grid is an energy management platform for industrial sites in Germany.">
</head><body><h1>Helios Grid</h1>
<p>Helios Grid is an energy management software platform used by industrial sites across Germany.</p>
<a href="/about">About</a><a href="/team">Team</a></body></html>"""

HELIOS_ABOUT = """<html><head><title>About Helios Grid</title></head><body>
<p>Helios Grid is headquartered in Munich, Germany. The company raised EUR 2,6 Millionen
in a seed round in 2025. Our platform and API serve 40 industrial sites.</p></body></html>"""

HELIOS_TEAM = """<html><head><title>Team</title></head><body>
<p>Lena Brandt, Co-founder and CEO, leads Helios Grid.</p>
<p>No contact details are listed on this page.</p></body></html>"""

# Not linked from anywhere on the site - only reachable by convention.
HELIOS_IMPRINT = """<html><head><title>Impressum - Helios Grid</title></head><body>
<p>Helios Grid GmbH, Maximilianstrasse 12, 80539 Munich, Germany.</p>
<p>Vertretungsberechtigte Geschaeftsfuehrerin: Lena Brandt.</p>
<p>Kontakt: lena.brandt [at] heliosgrid.de</p></body></html>"""

# A company that exists only as a funding headline: its coverage is on a host
# whose full page cannot be retrieved, and its own site is at a country-code
# domain that no search result points to. Everything the gates need is either in
# the excerpt or behind a domain the agent has to work out for itself.
FLOWT_HOME = """<html><head><title>Flowt - workflow automation</title></head><body>
<h1>Flowt</h1>
<p>Flowt is a workflow automation platform used by businesses across East Africa.
Our platform connects the tools a team already uses through a single API.</p>
<a href="/about">About</a><a href="/contact">Contact</a>
</body></html>"""

FLOWT_CONTACT = """<html><head><title>Contact Flowt</title></head><body>
<p>Flowt is based in Nairobi, Kenya.</p>
<p>Reach our founder Alice Wanjiru at alice.wanjiru@flowt.co.ke.</p>
</body></html>"""


PAGES: dict[str, str] = {
    "https://heliosgrid.de": HELIOS_HOME,
    "https://heliosgrid.de/": HELIOS_HOME,
    "https://heliosgrid.de/about": HELIOS_ABOUT,
    "https://heliosgrid.de/team": HELIOS_TEAM,
    "https://heliosgrid.de/impressum": HELIOS_IMPRINT,
    "https://zetacare.in": ZETA_HOME,
    "https://zetacare.in/": ZETA_HOME,
    "https://zetacare.in/about": ZETA_ABOUT,
    "https://zetacare.in/team": ZETA_TEAM,
    "https://zetacare.in/contact": ZETA_CONTACT,
    "https://zetacare.in/careers": ZETA_CAREERS,
    "https://zetacare.in/product": ZETA_PRODUCT,
    "https://novapay.ng": NOVA_HOME,
    "https://novapay.ng/": NOVA_HOME,
    "https://novapay.ng/about": NOVA_ABOUT,
    "https://novapay.ng/team": NOVA_TEAM,
    "https://novapay.ng/contact": NOVA_CONTACT,
    "https://orbitsim.pl": ORBIT_HOME,
    "https://orbitsim.pl/": ORBIT_HOME,
    "https://orbitsim.pl/about": ORBIT_ABOUT,
    "https://orbitsim.pl/team": ORBIT_TEAM,
    "https://orbitsim.pl/contact": ORBIT_CONTACT,
    "https://techcrunch.com/2026/zeta-care-seed": NEWS_ARTICLE,
    "https://exampleventures.com/portfolio": PORTFOLIO_PAGE,
    # Note: no flowt.com entry - the probe has to fall through to the ccTLD, and
    # no disruptafrica.com entry - that page is unreachable by design.
    "https://flowt.co.ke": FLOWT_HOME,
    "https://flowt.co.ke/": FLOWT_HOME,
    "https://flowt.co.ke/contact": FLOWT_CONTACT,
}

SERP_RESULTS = [
    {"title": "Indian healthtech Zeta Care raises $2.4M seed",
     "link": "https://techcrunch.com/2026/zeta-care-seed",
     "snippet": "Bengaluru-based Zeta Care has raised $2.4 million in a seed round."},
    {"title": "Our Portfolio - Example Ventures",
     "link": "https://exampleventures.com/portfolio",
     "snippet": "Companies we back across our cohort."},
]

# A second SERP fixture, used only by the test for companies that exist to the
# agent as nothing but a funding headline: the covering page is unreachable and
# the company's own site is at a country-code domain no result points to.
FLOWT_RESULT = {
    "title": "Kenyan AI startup Flowt raises pre-seed funding round",
    "link": "https://disruptafrica.com/2026/08/26/kenyan-ai-startup-flowt-raises-pre-seed/",
    "snippet": "Flowt, a Nairobi-based workflow automation platform, has raised $2 million "
               "in a pre-seed round. Founder Alice Wanjiru said the platform serves 300 businesses.",
}

SERP_RESULTS_WITH_HEADLINE_ONLY_COMPANY = [FLOWT_RESULT, *SERP_RESULTS]
