"""Contact enrichment: asking a database for an address we could not read.

Everything else in this project reaches ``verified`` by reading a source with
standing and quoting the sentence that proves it.  A contact database is not
that: it returns an address without showing where it came from.  Rather than
dress that up as a page we read, this module treats enrichment as its own kind
of evidence, labelled as such in the trail, and holds it to rules that keep it
from becoming a guess:

* the provider is **asked about a person we already identified ourselves**.  It
  never supplies the founder's name - that claim stays grounded in our own
  evidence, because it is the one most easily corrupted;
* only a provider status of *verified* is accepted.  Apollo also returns
  ``unverified`` and ``catch-all`` addresses, and those are guesses;
* the returned person must be the person we asked about, and the address must
  sit on the company's own domain;
* the address then clears the same technical checks as any other - syntax, not
  a role account, not disposable, real MX records, deliverability.

The provenance recorded on the lead says literally which database answered and
what it claimed, so a reader can always separate "found published on the
company's site" from "supplied by a contact database".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from ..config import Settings
from .base import (
    NotConfigured,
    ParseFailed,
    QuotaExhausted,
    RateLimited,
    Timeout,
    with_retries,
)


@dataclass
class ContactFinding:
    """One address offered by a contact database, with its provenance."""

    address: str
    provider: str
    provider_status: str
    person_name: str = ""
    title: str = ""
    # Pages the provider says it saw this address on. Hunter returns these;
    # they turn a database answer into something the agent can go and read for
    # itself, which is the difference between being told and knowing.
    source_urls: list[str] = field(default_factory=list)
    confidence: int | None = None
    retrieved_at: str = field(default_factory=lambda: datetime.now(UTC).date().isoformat())

    @property
    def provenance(self) -> str:
        base = (f"{self.provider} contact record for {self.person_name or 'this person'}"
                f" - status '{self.provider_status}', retrieved {self.retrieved_at}.")
        if self.source_urls:
            return (f"{base} {self.provider} cites this address as published at "
                    f"{self.source_urls[0]}.")
        return f"{base} Supplied by a contact database, not published on a page we read."


def _same_person(asked: str, returned: str) -> bool:
    """Did the database answer about the person we asked about?

    A wrong match is how somebody else's address ends up on a lead, so a name
    that shares no distinctive token with the one we asked for is rejected.
    """
    from ..validation.email import distinctive_name_tokens

    want = set(distinctive_name_tokens(asked))
    got = set(distinctive_name_tokens(returned or ""))
    if not want:
        return False
    if not got:
        return False                 # unnamed record proves nothing
    return bool(want & got)


class ApolloProvider:
    """Apollo.io People Enrichment (``POST /api/v1/people/match``)."""

    name = "apollo"
    ACCEPTED_STATUSES = ("verified",)

    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client = client
        self.settings = settings

    async def find(self, person: str, domain: str, company: str = "") -> ContactFinding | None:
        key = self.settings.apollo_api_key
        if not key:
            raise NotConfigured("apollo")

        parts = [p for p in (person or "").split() if p]
        if len(parts) < 2 or not domain:
            return None
        first, last = parts[0], parts[-1]

        async def _go() -> ContactFinding | None:
            r = await self.client.post(
                "https://api.apollo.io/api/v1/people/match",
                headers={"x-api-key": key, "Content-Type": "application/json",
                         "Cache-Control": "no-cache", "accept": "application/json"},
                json={"first_name": first, "last_name": last, "domain": domain,
                      "organization_name": company or None,
                      # Personal mailboxes are not what TVB asked for and carry a
                      # different privacy weight; only the work address is sought.
                      "reveal_personal_emails": False},
            )
            if r.status_code in (401, 403):
                raise QuotaExhausted("apollo: key rejected or plan does not allow API access")
            if r.status_code == 429:
                raise RateLimited("apollo")
            if r.status_code == 422:
                return None          # nothing matched; not an error
            if r.status_code >= 400:
                raise ParseFailed(f"apollo HTTP {r.status_code}")

            data = r.json() or {}
            record = data.get("person") or {}
            address = (record.get("email") or "").strip().lower()
            status = (record.get("email_status") or "").strip().lower()
            if not address or "@" not in address:
                return None
            # Apollo returns this literal placeholder when the plan will not
            # release the address. It is not an address and must never be used.
            if address.startswith("email_not_unlocked"):
                raise QuotaExhausted("apollo: plan will not release email addresses via API")
            if status not in self.ACCEPTED_STATUSES:
                return None          # 'unverified' and 'catch-all' are guesses

            returned_name = " ".join(
                p for p in (record.get("first_name"), record.get("last_name")) if p
            ) or (record.get("name") or "")
            if not _same_person(person, returned_name):
                return None

            return ContactFinding(address=address, provider=self.name, provider_status=status,
                                  person_name=returned_name or person,
                                  title=(record.get("title") or "").strip())

        return await with_retries(_go)



class HunterFinderProvider:
    """Hunter.io Email Finder (``GET /v2/email-finder``).

    Hunter is the better of the two providers for this project because it says
    *where* it saw an address. An answer with no cited source is Hunter guessing
    a pattern from the domain, which is exactly the thing this project refuses to
    do itself - so those answers are discarded, and only cited ones are used.
    """

    name = "hunter"

    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client = client
        self.settings = settings

    async def find(self, person: str, domain: str, company: str = "") -> ContactFinding | None:
        key = self.settings.hunter_api_key
        if not key:
            raise NotConfigured("hunter")

        parts = [p for p in (person or "").split() if p]
        if len(parts) < 2 or not domain:
            return None
        first, last = parts[0], parts[-1]

        async def _go() -> ContactFinding | None:
            r = await self.client.get(
                "https://api.hunter.io/v2/email-finder",
                params={"domain": domain, "first_name": first, "last_name": last,
                        "api_key": key},
            )
            if r.status_code in (401, 403):
                raise QuotaExhausted("hunter: key rejected")
            if r.status_code == 429:
                raise RateLimited("hunter")
            if r.status_code == 451:
                raise QuotaExhausted("hunter: monthly search allowance spent")
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                raise ParseFailed(f"hunter HTTP {r.status_code}")

            data = (r.json() or {}).get("data") or {}
            address = (data.get("email") or "").strip().lower()
            if not address or "@" not in address:
                return None

            sources = [str(src.get("uri") or "").strip()
                       for src in (data.get("sources") or []) if src.get("uri")]
            if not sources:
                # No cited page means Hunter inferred the address from the
                # domain's pattern. That is a guess, and this project does not
                # ship guesses - not its own, and not anybody else's.
                return None

            returned = " ".join(p for p in (data.get("first_name"), data.get("last_name")) if p)
            # `returned or person` compared the name to itself whenever Hunter
            # omitted one, so the identity check passed unconditionally - the one
            # guard standing between a pattern-generated address and a lead.
            if not _same_person(person, returned):
                return None

            verification = (data.get("verification") or {}).get("status") or ""
            # Apollo is held to email_status == "verified"; Hunter was held to
            # nothing. A cited source plus a score is the equivalent bar here:
            # Hunter publishes a confidence, and a low one means it inferred.
            score = data.get("score")
            if isinstance(score, int) and score < 70:
                return None
            if verification in ("invalid", "disposable"):
                return None
            return ContactFinding(
                address=address, provider=self.name,
                provider_status=verification or "cited",
                person_name=returned or person,
                title=(data.get("position") or "").strip(),
                source_urls=sources[:3],
                confidence=data.get("score") if isinstance(data.get("score"), int) else None,
            )

        return await with_retries(_go)


class ContactEnrichmentService:
    """The enrichment providers, behind one budgeted call."""

    def __init__(self, providers: list, on_event=None, max_calls: int = 200):
        self.providers = providers
        self.on_event = on_event or (lambda *a, **k: None)
        self.max_calls = max_calls
        self.calls = 0
        self.hits = 0
        self._disabled: set[str] = set()

    @classmethod
    def build(cls, client: httpx.AsyncClient, settings: Settings, on_event=None):
        provs = []
        # Hunter first: it cites the pages it saw an address on, so its answers
        # can be checked rather than taken on trust.
        if settings.hunter_api_key:
            provs.append(HunterFinderProvider(client, settings))
        if settings.apollo_api_key:
            provs.append(ApolloProvider(client, settings))
        return cls(provs, on_event=on_event,
                   max_calls=settings.budget.max_contact_lookups)

    @property
    def configured(self) -> bool:
        return any(p.name not in self._disabled for p in self.providers)

    @property
    def provider_names(self) -> str:
        return ", ".join(p.name for p in self.providers) or "none"

    async def find(self, person: str, domain: str, company: str = "") -> ContactFinding | None:
        if not self.configured or self.calls >= self.max_calls:
            return None
        for p in self.providers:
            if p.name in self._disabled:
                continue
            try:
                self.calls += 1
                found = await p.find(person, domain, company)
                if found:
                    self.hits += 1
                    return found
                continue   # this provider had nothing; ask the next one
            except NotConfigured:
                self._disabled.add(p.name)
            except QuotaExhausted as e:
                # A plan that will not release addresses is a permanent answer,
                # not a transient one: stop asking rather than burning the run.
                self.on_event(f"Contact provider '{p.name}' unavailable ({e}) - "
                              f"continuing without it.", "warn")
                self._disabled.add(p.name)
            except (RateLimited, Timeout, ParseFailed) as e:
                self.on_event(f"Contact provider '{p.name}' failed: {e}", "debug")
            except Exception as e:  # pragma: no cover - defensive
                self.on_event(f"Contact provider '{p.name}' unexpected "
                              f"{type(e).__name__}: {e}", "debug")
        return None
