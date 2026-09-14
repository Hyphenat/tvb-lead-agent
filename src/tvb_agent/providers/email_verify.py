"""Email verification.

Verification here is deliberately *multi-signal* rather than "an API said OK".
An address becomes VERIFIED only when all of the following hold:

1. it is syntactically valid and is not a role account (info@, hello@, ...);
2. its domain is not disposable and resolves to real MX hosts;
3. it was published on an **authoritative source** that attributes it to the
   named founder - this is the ``SOURCE_VERIFIED`` step performed upstream in
   ``validation.email``;
4. a deliverability check either passes, or is unavailable and the fact is
   recorded on the lead.

Two guards matter more than the rest:

* **Nothing is ever pattern-generated.**  There is no ``first@domain`` builder
  anywhere in this codebase, because a plausible guess that passes an SMTP check
  is indistinguishable from a real address until it bounces on TVB's side.
* **Catch-all domains are detected and downgraded.**  A catch-all accepts every
  local part, so treating its "deliverable" answer as proof would manufacture
  false positives at scale.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import httpx

from ..config import Settings
from .base import NotConfigured, ParseFailed, QuotaExhausted, RateLimited, Timeout, with_retries

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}$")

# Obfuscated forms sites use to dodge scrapers: "name [at] co.com", "name (at) co
# dot com", "name at co dot com".  The trailing TLD is validated against a real
# list because otherwise a phrase like "CEO at jane.doe" parses as an address.
_EMAIL_CORE = (
    r"(?P<local>[A-Za-z0-9._%+\-]{1,64})"
    r"\s*(?P<sep>@|\[\s*at\s*\]|\(\s*at\s*\)|&#64;|&#x40;|\{at\}|\s+at\s+)\s*"
    r"(?P<domain>(?:[A-Za-z0-9\-]{1,63}(?:\s*(?:\.|\[\s*dot\s*\]|\(\s*dot\s*\)|\s+dot\s+)\s*)){1,4}[A-Za-z]{2,63})"
    r"(?![A-Za-z0-9\-])"
)

# Wrapped in a zero-width lookahead so the scanner advances one character at a
# time.  Without this, a near-miss like "CEO at jane.doe" consumes the span and
# hides the real address that follows it on the same line.
EMAIL_IN_TEXT_RE = re.compile(r"(?<![A-Za-z0-9._%+\-])(?=" + _EMAIL_CORE + r")", re.IGNORECASE)

# Not exhaustive - broad enough to accept real company domains while rejecting
# accidental matches like "jane.doe" or "version 2.0".
VALID_TLDS = {
    "com", "net", "org", "io", "co", "ai", "app", "dev", "tech", "xyz", "me", "info", "biz",
    "cloud", "digital", "studio", "agency", "solutions", "systems", "software", "online",
    "site", "space", "world", "life", "live", "media", "news", "group", "team", "work",
    "email", "global", "ventures", "capital", "fund", "finance", "health", "care", "eco",
    "edu", "gov", "ac", "int", "mil", "inc", "llc", "ltd", "gmbh", "company", "store", "shop",
    "in", "uk", "de", "fr", "es", "it", "nl", "be", "ch", "at", "se", "no", "dk", "fi", "ie",
    "pt", "pl", "cz", "sk", "hu", "ro", "bg", "gr", "tr", "ru", "ua", "by", "lt", "lv", "ee",
    "ca", "mx", "br", "ar", "cl", "co.uk", "com.au", "au", "nz", "sg", "my", "id", "th", "vn",
    "ph", "hk", "tw", "jp", "kr", "cn", "ae", "sa", "qa", "kw", "bh", "om", "il", "eg", "ma",
    "ng", "ke", "za", "gh", "tz", "ug", "rw", "pk", "bd", "lk", "np", "ir", "iq", "jo", "lb",
    "is", "lu", "mt", "cy", "si", "hr", "rs", "ba", "mk", "al", "md", "ge", "am", "az", "kz",
    "uz", "mn", "kh", "la", "mm", "bn", "fj", "pg", "pe", "uy", "py", "bo", "ec", "ve", "cr",
    "pa", "gt", "hn", "sv", "ni", "do", "cu", "jm", "tt", "bb", "bs", "eu", "asia", "africa",
}


def _has_valid_tld(domain: str) -> bool:
    parts = domain.lower().split(".")
    if len(parts) < 2:
        return False
    return parts[-1] in VALID_TLDS or ".".join(parts[-2:]) in VALID_TLDS


def _trim_to_valid_domain(domain: str) -> str | None:
    """Cut a greedily-matched domain back to its longest valid form.

    ``info@acmetech.io. More text`` matches as the domain "acmetech.io.more"
    because the pattern treats the sentence-ending period as another label
    separator.  Trimming label by label recovers "acmetech.io".
    """
    parts = [p for p in domain.lower().split(".") if p]
    for k in range(len(parts), 1, -1):
        candidate = ".".join(parts[:k])
        if _has_valid_tld(candidate):
            return candidate
    return None


ROLE_LOCALPARTS = {
    "info", "hello", "contact", "support", "sales", "admin", "team", "help", "office",
    "enquiries", "enquiry", "inquiry", "inquiries", "press", "media", "marketing",
    "careers", "jobs", "hr", "recruitment", "billing", "accounts", "finance", "legal",
    "privacy", "security", "abuse", "postmaster", "webmaster", "noreply", "no-reply",
    "donotreply", "newsletter", "partnerships", "partner", "bd", "business", "general",
    "mail", "email", "service", "customercare", "care", "feedback", "invest", "investors",
    "management", "board", "directors", "exec", "executive",
    # the same desks in the languages whose imprint pages this agent targets
    "kontakt", "kontakta", "contacto", "contatti", "contato", "contacta",
    "presse", "impressum", "bonjour", "hola", "ciao", "salut", "hallo",
    "info-de", "anfrage", "buchhaltung", "vertrieb",
    # the English variants the single-component check could not see
    "contactus", "getintouch", "sayhello", "hi", "hey", "welcome", "reception",
    "hellothere", "letstalk", "talktous", "newbusiness", "new-business",
    "pr", "comms", "communications", "ops", "operations", "it", "dev", "devs",
    "staff", "people", "talent", "hiring", "recruiting", "apply", "jobs2",
    "ir", "events", "community", "orders", "order", "accounting", "accounts2",
    "all", "everyone", "everybody", "group", "company", "main", "reach",
}

DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org",
    "throwawaymail.com", "yopmail.com", "trashmail.com", "sharklasers.com", "getnada.com",
    "maildrop.cc", "dispostable.com", "fakeinbox.com", "mailnesia.com", "spamgourmet.com",
    "mohmal.com", "emailondeck.com", "tempr.email", "discard.email", "mintemail.com",
}

# A founder on a free consumer mailbox is not a company-verified contact.
FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com", "mail.com",
    "protonmail.com", "proton.me", "gmx.com", "gmx.de", "yandex.com", "yandex.ru",
    "zoho.com", "rediffmail.com", "qq.com", "163.com", "126.com", "naver.com",
}


def is_valid_syntax(address: str) -> bool:
    a = (address or "").strip()
    return bool(a) and len(a) <= 254 and bool(EMAIL_RE.match(a))


def local_part(address: str) -> str:
    return (address or "").split("@")[0].lower()


def domain_part(address: str) -> str:
    return (address or "").split("@")[-1].lower()


def is_role_account(address: str) -> bool:
    """Is this a shared mailbox rather than one person's address?

    Offices get their own mailbox - tallinn@, london@, apac@ - and run 10 put
    exactly such an address forward as a founder contact. A city is a desk, not
    a person.
    """
    whole = local_part(address)
    # Every component, not only the first: "the.team@", "new.business@" and
    # "our.sales@" all escaped a check that looked at the first piece alone.
    parts = [p for p in re.split(r"[+._\-]", whole) if p]
    # ...and with trailing digits stripped, because "info2@" and "team2@" are the
    # same desk as "info@" and "team@".
    stripped = {re.sub(r"\d+$", "", p) for p in parts} | {re.sub(r"\d+$", "", whole)}
    if whole in ROLE_LOCALPARTS or any(p in ROLE_LOCALPARTS for p in parts):
        return True
    if any(p in ROLE_LOCALPARTS for p in stripped if p):
        return True
    from ..validation.geo import CITY_COUNTRY, COUNTRY_NAMES, US_CITIES

    places = {c.replace(" ", "") for c in CITY_COUNTRY}
    places |= {c.lower().replace(" ", "") for c in COUNTRY_NAMES}
    places |= {c.replace(" ", "") for c in US_CITIES}
    return whole in places or any(p in places for p in parts)



# Addresses that are the *named* officer's desk rather than a shared company
# inbox. The brief asks for "name and email of the CEO or Co-founder", and
# ceo@acme.com is exactly that when we have independently established who the
# CEO is: it reaches that person. info@ and sales@ do not, which is the whole
# distinction. These are accepted only alongside a founder identified from our
# own evidence, and the lead records that the address is role-addressed rather
# than a personal mailbox.
FOUNDER_OFFICE_LOCALPARTS = {
    "ceo", "founder", "founders", "cofounder", "co-founder", "founder1",
    "md", "managingdirector", "gf", "geschaeftsfuehrung", "geschäftsführung",
    "direccion", "direction", "gerencia", "amministratore",
}


def is_founder_office_account(address: str) -> bool:
    """Is this the named officer's desk (ceo@) rather than a shared inbox (info@)?"""
    whole = local_part(address).lower()
    parts = {p for p in re.split(r"[+._\-]", whole) if p}
    return whole in FOUNDER_OFFICE_LOCALPARTS or bool(parts & FOUNDER_OFFICE_LOCALPARTS)


def is_disposable(address: str) -> bool:
    return domain_part(address) in DISPOSABLE_DOMAINS


def is_freemail(address: str) -> bool:
    return domain_part(address) in FREEMAIL_DOMAINS


# Words that end an ordinary sentence before "at <domain>". Treating any of
# these as a mailbox name invents an address that exists nowhere.
_PROSE_WORDS = frozenset({
    "more", "us", "me", "here", "this", "it", "them", "him", "her", "you",
    "founder", "ceo", "team", "story", "work", "available", "reach", "look", "now", "today", "online", "live", "free", "back", "out", "in",
    "on", "up", "down", "again", "soon", "later", "everything", "anything",
    "something", "all", "both", "one", "two", "office", "offices", "home",
    "shop", "store", "site", "website", "page", "blog", "news", "based",
})

def find_emails_in_text(text: str) -> list[str]:
    """Find addresses in page text, including common anti-scraper obfuscations.

    The bare ``name at domain`` spelling is only accepted when the domain is
    *also* obfuscated (``co dot uk``).  Without that restriction, ordinary prose
    such as "see our docs at example.com" parses as an address - and inventing a
    contact is the one failure this project cannot afford.
    """
    out: list[str] = []
    for m in EMAIL_IN_TEXT_RE.finditer(text or ""):
        sep = (m.group("sep") or "").strip().lower()
        raw_domain = m.group("domain")
        obfuscated_dot = bool(re.search(r"\[\s*dot\s*\]|\(\s*dot\s*\)|\s+dot\s+", raw_domain, re.I))
        # "at" in prose is not obfuscation. "Learn more at acme dot com" was
        # producing more@acme.com, and "our founder at acme dot com" produced
        # founder@acme.com - an address that appears nowhere, manufactured from
        # an ordinary English sentence and then shipped as verified. A spelled-out
        # "at" only counts when the local part is itself written as an address
        # would be, not as the last word of a sentence.
        local = m.group("local").strip(".")
        if sep == "at":
            if not obfuscated_dot:
                continue
            # The local part has to look like a mailbox name, not like the last
            # word of a sentence.
            if local.lower() in _PROSE_WORDS:
                continue
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._%+-]{1,63}", local):
                continue
        domain = re.sub(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\s+dot\s+)\s*", ".", raw_domain, flags=re.I)
        domain = re.sub(r"\s+", "", domain).strip(".").lower()
        domain = _trim_to_valid_domain(domain)
        if not domain:
            continue
        addr = f"{local}@{domain}".lower()
        if is_valid_syntax(addr) and addr not in out:
            out.append(addr)
    return out


@dataclass
class DeliverabilityResult:
    status: str            # deliverable | undeliverable | risky | unknown
    provider: str
    is_catch_all: bool | None = None
    detail: str = ""


# --------------------------------------------------------------------------- #
# DNS / MX
# --------------------------------------------------------------------------- #
class MXChecker:
    def __init__(self):
        self._cache: dict[str, list[str]] = {}

    async def mx_hosts(self, domain: str) -> list[str]:
        d = (domain or "").lower()
        if d in self._cache:
            return self._cache[d]
        hosts = await asyncio.get_running_loop().run_in_executor(None, self._lookup, d)
        self._cache[d] = hosts
        return hosts

    @staticmethod
    def _lookup(domain: str) -> list[str]:
        try:
            import dns.resolver  # local import keeps the module importable without dnspython

            resolver = dns.resolver.Resolver()
            resolver.lifetime = 6.0
            resolver.timeout = 6.0
            try:
                answers = resolver.resolve(domain, "MX")
                return sorted(str(r.exchange).rstrip(".").lower() for r in answers)
            except Exception:
                # Some domains accept mail on the A record with no MX.
                try:
                    resolver.resolve(domain, "A")
                    return [domain]
                except Exception:
                    return []
        except Exception:
            return []


# --------------------------------------------------------------------------- #
# Deliverability providers
# --------------------------------------------------------------------------- #
class DeliverabilityProvider:
    name = "base"

    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client = client
        self.settings = settings

    async def check(self, address: str) -> DeliverabilityResult:  # pragma: no cover
        raise NotImplementedError


class ZeroBounceProvider(DeliverabilityProvider):
    name = "zerobounce"

    async def check(self, address: str) -> DeliverabilityResult:
        key = self.settings.zerobounce_api_key
        if not key:
            raise NotConfigured("zerobounce")

        async def _go():
            r = await self.client.get("https://api.zerobounce.net/v2/validate",
                                      params={"api_key": key, "email": address})
            if r.status_code == 429:
                raise RateLimited("zerobounce")
            if r.status_code in (401, 403):
                raise QuotaExhausted("zerobounce auth")
            if r.status_code >= 400:
                raise ParseFailed(f"zerobounce HTTP {r.status_code}")
            d = r.json()
            status = (d.get("status") or "").lower()
            sub = (d.get("sub_status") or "").lower()
            # ZeroBounce answers HTTP 200 with sub_status "exceeded_api_credits"
            # when the balance runs out. Reading that as an ordinary "unknown"
            # leaves the provider enabled and every later address stuck one rung
            # below verified - which is how a run with five credits left ends
            # with zero qualified leads.
            if sub in ("exceeded_api_credits", "insufficient_credits") or \
                    "credit" in (d.get("error") or "").lower():
                raise QuotaExhausted("zerobounce credits exhausted")
            mapped = {"valid": "deliverable", "invalid": "undeliverable",
                      "catch-all": "risky", "spamtrap": "undeliverable",
                      "abuse": "risky", "do_not_mail": "undeliverable"}.get(status, "unknown")
            return DeliverabilityResult(mapped, self.name,
                                        is_catch_all=(status == "catch-all"),
                                        detail=f"{status}/{sub}")

        return await with_retries(_go)


class HunterProvider(DeliverabilityProvider):
    name = "hunter"

    async def check(self, address: str) -> DeliverabilityResult:
        key = self.settings.hunter_api_key
        if not key:
            raise NotConfigured("hunter")

        async def _go():
            r = await self.client.get("https://api.hunter.io/v2/email-verifier",
                                      params={"email": address, "api_key": key})
            if r.status_code == 429:
                raise RateLimited("hunter")
            if r.status_code in (401, 403):
                raise QuotaExhausted("hunter auth")
            if r.status_code >= 400:
                raise ParseFailed(f"hunter HTTP {r.status_code}")
            d = (r.json() or {}).get("data") or {}
            status = (d.get("status") or "").lower()
            mapped = {"valid": "deliverable", "invalid": "undeliverable",
                      "accept_all": "risky", "webmail": "risky",
                      "disposable": "undeliverable", "unknown": "unknown"}.get(status, "unknown")
            return DeliverabilityResult(mapped, self.name,
                                        is_catch_all=bool(d.get("accept_all")),
                                        detail=f"{status} score={d.get('score')}")

        return await with_retries(_go)


class AbstractProvider(DeliverabilityProvider):
    name = "abstract"

    async def check(self, address: str) -> DeliverabilityResult:
        key = self.settings.abstract_api_key
        if not key:
            raise NotConfigured("abstract")

        async def _go():
            r = await self.client.get("https://emailvalidation.abstractapi.com/v1/",
                                      params={"api_key": key, "email": address})
            if r.status_code == 429:
                raise RateLimited("abstract")
            if r.status_code in (401, 403):
                raise QuotaExhausted("abstract auth")
            if r.status_code >= 400:
                raise ParseFailed(f"abstract HTTP {r.status_code}")
            d = r.json() or {}
            deliv = (d.get("deliverability") or "").lower()
            mapped = {"deliverable": "deliverable", "undeliverable": "undeliverable",
                      "risky": "risky"}.get(deliv, "unknown")
            catch_all = d.get("is_catchall_email", {})
            return DeliverabilityResult(mapped, self.name,
                                        is_catch_all=bool(catch_all.get("value")) if isinstance(catch_all, dict) else None,
                                        detail=deliv)

        return await with_retries(_go)


class FixtureDeliverabilityProvider(DeliverabilityProvider):
    name = "fixture"

    def __init__(self, results: dict[str, DeliverabilityResult] | None = None,
                 default: DeliverabilityResult | None = None):
        self.results = results or {}
        self.default = default or DeliverabilityResult("unknown", "fixture")
        self.calls: list[str] = []

    async def check(self, address: str) -> DeliverabilityResult:
        self.calls.append(address)
        return self.results.get(address.lower(), self.default)


class EmailVerificationService:
    """Combines syntax, MX, disposability and deliverability into one verdict."""

    def __init__(self, providers: list[DeliverabilityProvider], mx: MXChecker | None = None,
                 on_event=None, max_checks: int = 10_000):
        self.providers = providers
        self.mx = mx or MXChecker()
        self.on_event = on_event or (lambda *a, **k: None)
        self.checks = 0
        self.max_checks = max_checks
        self._disabled: set[str] = set()

    @classmethod
    def build(cls, client: httpx.AsyncClient, settings: Settings, on_event=None) -> EmailVerificationService:
        provs: list[DeliverabilityProvider] = []
        if settings.zerobounce_api_key:
            provs.append(ZeroBounceProvider(client, settings))
        if settings.hunter_api_key:
            provs.append(HunterProvider(client, settings))
        if settings.abstract_api_key:
            provs.append(AbstractProvider(client, settings))
        return cls(provs, on_event=on_event, max_checks=settings.budget.max_email_verifications)

    @property
    def has_deliverability_provider(self) -> bool:
        return any(p.name not in self._disabled for p in self.providers)

    async def mx_for(self, domain: str) -> list[str]:
        return await self.mx.mx_hosts(domain)

    async def deliverability(self, address: str) -> DeliverabilityResult:
        # These three situations had the same provider name ("none"), and the
        # email ladder treats "none" as the documented no-provider fallback that
        # promotes an address to verified. So company #81 in a run, and every
        # address during a five-minute rate-limit, were promoted on no evidence -
        # identical companies getting different verdicts by position in the run.
        if not self.providers:
            return DeliverabilityResult("unknown", "none",
                                        detail="no deliverability provider configured")
        if self.checks >= self.max_checks:
            return DeliverabilityResult("unknown", "budget-spent",
                                        detail="verification budget reached this run")
        for p in self.providers:
            if p.name in self._disabled:
                continue
            try:
                res = await p.check(address)
                self.checks += 1
                return res
            except NotConfigured:
                self._disabled.add(p.name)
            except QuotaExhausted as e:
                self.on_event(f"Email verifier '{p.name}' out of quota ({e}) - falling back.", "warn")
                self._disabled.add(p.name)
            except (RateLimited, Timeout, ParseFailed) as e:
                self.on_event(f"Email verifier '{p.name}' failed: {e}", "debug")
            except Exception as e:  # pragma: no cover
                self.on_event(f"Email verifier '{p.name}' unexpected {type(e).__name__}", "debug")
        return DeliverabilityResult("unknown", "errored",
                                    detail="every deliverability provider failed on this address")
