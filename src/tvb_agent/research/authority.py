"""How much a given source is allowed to prove.

Discovery and evidence are separate concerns: a company found via a listicle is
still investigated from scratch, and the listicle itself carries little weight.
"""

from __future__ import annotations

from ..models import SourceAuthority
from ..providers.fetcher import host_of

TIER1_PRESS_HOSTS = {
    "techcrunch.com", "venturebeat.com", "forbes.com", "bloomberg.com", "reuters.com",
    "ft.com", "wsj.com", "cnbc.com", "businessinsider.com", "theguardian.com", "bbc.com",
    "economictimes.indiatimes.com", "livemint.com", "business-standard.com", "thehindu.com",
    "yourstory.com", "inc42.com", "entrackr.com", "moneycontrol.com", "vccircle.com",
    "tech.eu", "sifted.eu", "eu-startups.com", "siliconcanals.com", "techcabal.com",
    "disrupt-africa.com", "disruptafrica.com", "wamda.com", "techinasia.com", "e27.co",
    "dealstreetasia.com",
    "startupdaily.net", "betakit.com", "maddyness.com", "gruenderszene.de", "frenchweb.fr",
    "startupticker.ch", "uktech.news", "silicon.co.uk", "arabnews.com", "zawya.com",
    "thenationalnews.com", "businessdailyafrica.com", "itweb.co.za", "contxto.com",
    "labsnews.com", "startupdaily.com.au", "theregister.com", "zdnet.com", "wired.com",
    # structured funding trackers, verified to carry amount, HQ and founder
    "thesaasnews.com", "startuptalky.com", "beststartup.in", "startuprise.co.uk",
    "arabianbusiness.com", "finsmes.com", "techfundingnews.com", "nairametrics.com", "techpoint.africa", "technext24.com", "benjamindada.com",
    "weetracker.com", "techzim.co.zw", "itweb.africa", "startupsmagazine.co.uk",
    # Regional startup press confirmed live to carry the amount, the round and
    # the country in the headline. Run 7 rejected real in-band companies for
    # "no verifiable funding" because their coverage sat on hosts like these and
    # scored zero.
    "technode.global", "technode.com", "vir.com.vn", "digitalnewsasia.com",
    "dailysocial.id", "techinafrica.com", "launchbaseafrica.com", "khusoko.com",
    "ffnews.com", "fintech.global", "menabytes.com", "magnitt.com",
    "calcalistech.com", "geektime.co.il", "globes.co.il", "ctech.co.il",
    "kr-asia.com", "36kr.com", "nikkei.com", "thejakartapost.com",
    "bangkokpost.com", "vietnamnews.vn", "theedgemalaysia.com", "philstar.com",
    "dawn.com", "profit.pakistantoday.com.pk", "thedailystar.net",
    "bdnews24.com", "webrazzi.com", "startupsmagazine.eu", "trendingtopics.eu",
    "netokracija.com", "itkeymedia.com", "themayor.eu", "novobrief.com",
    "businesspost.ie", "irishtechnews.ie", "siliconrepublic.com",
    "startupsmagazine.com", "tech.co", "techerati.com", "uktn.co.uk",
    "eustartups.com", "startupbrett.com", "swissinfo.ch",
    "latamlist.com", "startupi.com.br", "neofeed.com.br", "bloomberglinea.com",
    "techpoint.ng", "condiaonline.com", "bigtechthisweek.com",
    "techafricanews.com", "innovation-village.com", "techloy.com",
    "thefintechtimes.com", "finextra.com", "altfi.com",
    # official wires: company-issued releases
    "prnewswire.com", "businesswire.com", "globenewswire.com", "einpresswire.com",
}

OFFICIAL_REGISTRY_HOSTS = {
    "find-and-update.company-information.service.gov.uk", "companieshouse.gov.uk",
    "mca.gov.in", "opencorporates.com", "handelsregister.de", "kvk.nl",
    "societe.com", "infogreffe.fr", "bizfile.gov.sg", "acra.gov.sg",
    "sec.gov", "asic.gov.au", "brreg.no", "bolagsverket.se", "virk.dk",
}

AGGREGATOR_HOSTS = {
    "crunchbase.com", "pitchbook.com", "tracxn.com", "dealroom.co", "cbinsights.com",
    "owler.com", "zoominfo.com", "growjo.com", "latka.com", "startupranking.com",
    "f6s.com", "angel.co", "wellfound.com", "magnitt.com", "startuplanes.com",
    "eu-startups.com", "seedtable.com", "failory.com", "startupblink.com",
}

SOCIAL_HOSTS = {
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "youtube.com", "medium.com", "substack.com", "github.com", "producthunt.com",
    "crunchbase.com/person", "about.me", "wikipedia.org",
}


def classify_authority(url: str, company_domain: str | None = None) -> SourceAuthority:
    host = host_of(url)
    if not host:
        return SourceAuthority.UNKNOWN

    if company_domain:
        cd = company_domain.lower()
        cd = cd[4:] if cd.startswith("www.") else cd
        if host == cd or host.endswith("." + cd) or cd.endswith("." + host):
            return SourceAuthority.COMPANY_OWNED

    def match(collection: set[str]) -> bool:
        return host in collection or any(host.endswith("." + h) for h in collection)

    if match(OFFICIAL_REGISTRY_HOSTS):
        return SourceAuthority.OFFICIAL_REGISTRY
    if match(TIER1_PRESS_HOSTS):
        return SourceAuthority.TIER1_PRESS
    if match(AGGREGATOR_HOSTS):
        return SourceAuthority.AGGREGATOR
    if match(SOCIAL_HOSTS):
        return SourceAuthority.SOCIAL_PROFILE
    if _looks_like_trade_press(url, host):
        # No curated list can name every regional outlet that reports a seed
        # round, and run 7 discarded real funding evidence from a dozen of them
        # simply because the host was not recognised. An unrecognised outlet is
        # admitted at the *floor* - the same standing as a startup database, no
        # more - and every other safeguard still applies: the sentence has to
        # name the company, the quote has to be verbatim, and the figure has to
        # be parsed out of the page rather than summarised.
        return SourceAuthority.AGGREGATOR
    return SourceAuthority.UNKNOWN


# Words that appear in the name of a publication and almost nowhere else.
_PRESS_MARKERS = (
    "news", "tech", "startup", "startups", "times", "herald", "tribune", "post",
    "daily", "weekly", "wire", "press", "media", "journal", "gazette", "report",
    "insider", "magazine", "bizz", "business", "ventures", "venturebeat",
    "digital", "crunch", "founders", "entrepreneur", "inno", "disrupt",
)


def _looks_like_trade_press(url: str, host: str) -> bool:
    """Is this an unrecognised outlet reporting a story, or just some website?

    Two independent signals are required: the *name* has to read like a
    publication, and the URL has to be an article rather than a home page or a
    marketing route. Either one alone matches far too much.
    """
    from urllib.parse import urlparse

    labels = host.lower().split(".")
    base = labels[-3] if len(labels) >= 3 and len(labels[-2]) <= 3 else (labels[-2] if len(labels) >= 2 else host)
    named_like_press = any(m in base for m in _PRESS_MARKERS)
    if not named_like_press:
        return False
    path = urlparse(url).path.strip("/")
    if not path:
        return False            # a home page proves nothing
    segments = path.split("/")
    slug = segments[-1]
    # An article has a dated path, or a long hyphenated headline slug.
    dated = any(seg.isdigit() and len(seg) == 4 for seg in segments)
    headline_slug = slug.count("-") >= 3
    return dated or headline_slug
