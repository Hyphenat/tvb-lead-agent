"""The email verification ladder.

Reaching ``VERIFIED`` requires *all* of:

* valid syntax, not disposable, not a role account;
* the address was published on an **authoritative** source (the company's own
  site, an official registry, or a first-tier press release) - aggregators,
  social profiles and search snippets are not enough;
* the address is **attributed to the identified founder**, either because the
  quote names them or because the local part matches their name;
* the domain belongs to the company (or, for a personal mailbox, the company's
  own site published it);
* the domain has working MX records;
* a deliverability provider says "deliverable", or no provider is configured and
  that limitation is recorded on the lead.

Two things deliberately never happen here:

* an address is never *constructed* from a name-and-domain pattern;
* a "deliverable" answer from a **catch-all** domain is never treated as proof,
  because such a domain accepts every local part that is thrown at it.
"""

from __future__ import annotations

import re

from ..models import EmailRecord, EmailStatus, Evidence, Evidenced, Person, SourceAuthority
from ..providers.email_verify import (
    EmailVerificationService,
    domain_part,
    is_disposable,
    is_founder_office_account,
    is_freemail,
    is_role_account,
    is_valid_syntax,
    local_part,
)
from ..research.extractor import Claim, ExtractionResult

AUTHORITATIVE_FLOOR = SourceAuthority.TIER1_PRESS  # rank 4: company-owned, registry, tier-1 press


def _name_tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^A-Za-z]+", (name or "").lower()) if len(t) > 1]


def local_part_matches_name(address: str, name: str) -> bool:
    """Does the published local part correspond to this person?

    This *confirms* an address that was already found in public text.  It is not
    used to build one - there is no code path in this project that turns a name
    plus a domain into an address.
    """
    lp = re.sub(r"[^a-z]", "", local_part(address).lower())
    toks = _name_tokens(name)
    if not lp or not toks:
        return False
    first, last = toks[0], toks[-1]
    if first in lp and last in lp:
        return True
    if lp in (first, last):
        return True
    if len(first) > 0 and lp == f"{first[0]}{last}":
        return True
    if bool(last) and lp == f"{first}{last[0]}":
        return True
    # "l.brandt@", "lena.b@", "brandt@" - the person's own name as a WHOLE
    # component of the local part. Matching it as a bare substring made
    # crawford@ belong to Tom Ford, bernstein@ to Ben Stein and markus@ to Mark
    # Ross: any founder whose name is a common prefix was attributed to a
    # colleague's address on the same domain.
    components = {c for c in re.split(r"[^a-z]+", local_part(address).lower()) if c}
    return any(len(t) >= 4 and t in components for t in (first, last))


# Words that describe a job rather than identify a person. A "name" made of
# these matches almost any page: run 10 attributed tallinn@lift99.co to a
# "founder" called "Ex-Pipedrive Founder" because the words "founder" and
# "Pipedrive" both appeared near the address.
_ROLE_WORDS = frozenset({
    "founder", "founders", "cofounder", "founding", "ceo", "cto", "coo", "cfo",
    "chief", "president", "vice", "director", "head", "officer", "partner",
    "investor", "chairman", "owner", "executive", "manager", "lead", "principal",
    "ex", "former", "serial", "angel", "mentor", "advisor", "adviser",
    "entrepreneur", "alum", "alumni", "team", "member", "guest", "speaker",
})


def distinctive_name_tokens(name: str) -> list[str]:
    """The parts of a name that actually identify the person."""
    return [t for t in _name_tokens(name) if len(t) >= 3 and t not in _ROLE_WORDS]


def quote_attributes_to(quote: str, name: str) -> bool:
    """Does this passage name the person the address is supposed to belong to?"""
    toks = distinctive_name_tokens(name)
    # One token identifies nobody: "Pipedrive" appears on any page about the
    # company. Attribution by quote needs a first and a last name; a single-token
    # name can still be carried by the local part, which is a stronger signal.
    if len(toks) < 2:
        return False
    low = (quote or "").lower()
    return sum(1 for t in toks if t in low) >= 2


# Second-level labels that are part of the public suffix rather than the name.
_PUBLIC_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "in"}


def registrable_root(domain: str) -> str:
    """The meaningful label: acme.io, mail.acme.io and acme.co.uk all -> "acme"."""
    labels = [lbl for lbl in (domain or "").lower().split(".") if lbl]
    if len(labels) < 2:
        return labels[0] if labels else ""
    if len(labels) >= 3 and labels[-2] in _PUBLIC_SECOND_LEVEL:
        return labels[-3]
    return labels[-2]


def domain_matches_company(address: str, company_domain: str | None) -> bool:
    if not company_domain:
        return False
    cd = company_domain.lower()
    cd = cd[4:] if cd.startswith("www.") else cd
    ad = domain_part(address)
    if not ad or not cd:
        return False
    if ad == cd or ad.endswith("." + cd) or cd.endswith("." + ad):
        return True
    a_root, c_root = registrable_root(ad), registrable_root(cd)
    return bool(a_root) and a_root == c_root


async def validate_email(
    result: ExtractionResult,
    founder: Evidenced[Person],
    company_domain: str | None,
    verifier: EmailVerificationService,
) -> EmailRecord:
    record = EmailRecord()

    claims: list[Claim] = [c for c in result.by_field("email") if is_valid_syntax(str(c.value))]
    if not claims:
        record.status = EmailStatus.NOT_FOUND
        record.notes.append("No email address was published on any source we read.")
        return record

    # Role accounts are kept only as a separate, clearly-labelled fallback.
    role_claims = [c for c in claims if is_role_account(str(c.value))]
    personal = [c for c in claims if not is_role_account(str(c.value))]
    if role_claims:
        record.role_fallback = str(role_claims[0].value)

    personal = [c for c in personal if not is_disposable(str(c.value))]
    if not personal:
        record.status = EmailStatus.ROLE_ONLY if role_claims else EmailStatus.NOT_FOUND
        record.notes.append(
            "Only generic company addresses were found; a role account is not accepted as a founder contact."
            if role_claims else "No personal address found."
        )
        return record

    founder_name = founder.value.name if founder.value else ""

    def score(c: Claim) -> tuple:
        addr = str(c.value)
        return (
            1 if quote_attributes_to(c.evidence.quote, founder_name) else 0,
            1 if local_part_matches_name(addr, founder_name) else 0,
            1 if domain_matches_company(addr, company_domain) else 0,
            c.evidence.rank,
        )

    best = max(personal, key=score)
    address = str(best.value).lower()
    record.address = address
    record.owner_name = founder_name or None
    record.evidence = [best.evidence] + [
        c.evidence for c in personal if str(c.value).lower() == address and c.evidence.url != best.evidence.url
    ][:2]
    record.syntax_ok = True
    record.is_role_account = False
    record.is_disposable = False
    record.domain_matches_company = domain_matches_company(address, company_domain)

    office = is_founder_office_account(address)
    attributed = (quote_attributes_to(best.evidence.quote, founder_name)
                  or local_part_matches_name(address, founder_name)
                  # ceo@acme.com IS the CEO's address once we have independently
                  # established who the CEO is. The brief asks for the name and
                  # email of the CEO or a co-founder; this is both. A shared
                  # inbox like info@ is not, and is still refused above.
                  or (office and founder.known))
    authoritative = _rank_ok(best.evidence)

    if not founder.known:
        record.status = EmailStatus.FOUND_UNVERIFIED
        record.notes.append("No founder identified, so this address cannot be attributed to a named person.")
        return record

    if not authoritative:
        record.status = EmailStatus.FOUND_UNVERIFIED
        record.notes.append(
            f"Published only on a {best.evidence.authority.value} source; an authoritative source "
            "(company site, official registry or first-tier press) is required."
        )
        return record

    if not attributed:
        record.status = EmailStatus.FOUND_UNVERIFIED
        record.notes.append(
            f"Address is published on an authoritative source but is not attributed to {founder_name}."
        )
        return record

    if is_freemail(address) and best.evidence.authority is not SourceAuthority.COMPANY_OWNED:
        record.status = EmailStatus.FOUND_UNVERIFIED
        record.notes.append(
            "Personal mailbox on a free provider, not published on the company's own site."
        )
        return record

    if not record.domain_matches_company and not is_freemail(address):
        # A previous employer's address, or a wrong match. A press quote naming
        # "Lena Brandt, previously of Northstar Ventures (lena.brandt@northstar.vc)"
        # shipped that address as her verified contact at the company she founded.
        record.status = EmailStatus.FOUND_UNVERIFIED
        record.notes.append(
            f"Address domain ({domain_part(address)}) is not the company's domain "
            f"({company_domain}), so it is not evidence of a contact at this company.")
        return record

    record.status = EmailStatus.SOURCE_VERIFIED
    if office:
        record.notes.append(
            f"This is the {local_part(address)}@ desk of {founder_name}, not a personal mailbox. "
            f"It is accepted because the officer was identified independently and this address "
            f"reaches them; a shared inbox such as info@ is not accepted.")

    # --- technical checks -------------------------------------------------
    mx = await verifier.mx_for(domain_part(address))
    record.mx_ok = bool(mx)
    record.mx_hosts = mx[:4]
    if not mx:
        record.status = EmailStatus.INVALID
        record.notes.append(f"Domain {domain_part(address)} has no MX record; mail cannot be delivered.")
        return record

    deliv = await verifier.deliverability(address)
    record.deliverability = deliv.status
    record.verifier = deliv.provider
    record.is_catch_all = deliv.is_catch_all
    if deliv.detail:
        record.notes.append(f"Deliverability check ({deliv.provider}): {deliv.detail}")

    if deliv.status == "undeliverable":
        record.status = EmailStatus.INVALID
        record.notes.append("Deliverability provider reports this address is undeliverable.")
        return record

    if deliv.is_catch_all:
        record.notes.append(
            "Domain is catch-all: it accepts any address, so a positive deliverability result "
            "proves nothing. Kept at source-verified rather than verified."
        )
        return record

    if deliv.status == "deliverable":
        record.status = EmailStatus.VERIFIED
        return record

    if deliv.status == "risky":
        record.notes.append("Deliverability provider flagged this address as risky; not promoted to verified.")
        return record

    # No provider answered at all - none configured, the free tier is spent, or
    # every provider errored. Source attribution + MX is then the strongest
    # claim that can honestly be made, and the limitation travels with the lead
    # in writing. A provider that *did* answer "unknown" about this particular
    # address is a different matter: that answer is about the address, and it
    # keeps the lead one rung below verified.
    # Only the genuine "nobody was configured" case earns the fallback. A budget
    # that ran out and a provider that errored are not evidence of anything.
    if deliv.provider == "none" and not verifier.has_deliverability_provider:
        record.status = EmailStatus.VERIFIED
        record.notes.append(
            "Verified by authoritative-source attribution and MX check. No third-party "
            f"deliverability provider answered for this address ({deliv.detail or 'none available'}), "
            "so an SMTP-level confirmation was not performed."
        )
        return record

    record.notes.append("Deliverability result was inconclusive; not promoted to verified.")
    return record


def _rank_ok(evidence: Evidence) -> bool:
    from ..models import AUTHORITY_RANK

    return AUTHORITY_RANK[evidence.authority] >= AUTHORITY_RANK[AUTHORITATIVE_FLOOR]

# --------------------------------------------------------------------------- #
# An address supplied by a contact database
# --------------------------------------------------------------------------- #
async def verify_enriched_address(finding, founder: Evidenced[Person],
                                  company_domain: str | None,
                                  verifier: EmailVerificationService) -> EmailRecord:
    """Turn a contact-database answer into a record, held to the same checks.

    The one thing that is different is the *provenance*: this address was not
    published on a page anybody read, and the evidence says so in those words.
    Everything else - not a role account, not disposable, on the company's own
    domain, real MX records, a deliverability answer - is identical to the
    ladder an address found on a page has to climb.
    """
    record = EmailRecord()
    address = (finding.address or "").strip().lower()
    record.address = address
    record.owner_name = founder.value.name if founder.value else None

    if not is_valid_syntax(address):
        record.status = EmailStatus.INVALID
        record.notes.append(f"{finding.provider} returned a malformed address.")
        return record
    if is_role_account(address):
        record.role_fallback = address
        record.address = None
        record.status = EmailStatus.ROLE_ONLY
        record.notes.append(
            f"{finding.provider} returned a generic company address, which is not a "
            f"founder contact.")
        return record
    if is_disposable(address):
        record.status = EmailStatus.INVALID
        record.notes.append("Address is on a disposable domain.")
        return record
    if not founder.known:
        record.status = EmailStatus.FOUND_UNVERIFIED
        record.notes.append("No founder identified, so this address cannot be attributed.")
        return record

    record.syntax_ok = True
    record.domain_matches_company = domain_matches_company(address, company_domain)
    if not record.domain_matches_company:
        # A database record on a different domain could be an old employer or a
        # wrong match. Without the company's own domain behind it, it is not
        # evidence that this person can be reached here on this company's behalf.
        record.status = EmailStatus.FOUND_UNVERIFIED
        record.notes.append(
            f"{finding.provider} returned an address on {domain_part(address)}, which is not "
            f"the company's domain ({company_domain}).")
        return record

    # And it has to be THIS person's address. The enriched path checked only that
    # the domain matched, so peter.schmidt@acme.com and ceo@acme.com both became
    # Lena Brandt's verified contact.
    founder_name = founder.value.name if founder.value else ""
    if not local_part_matches_name(address, founder_name):
        record.status = EmailStatus.FOUND_UNVERIFIED
        record.notes.append(
            f"{finding.provider} returned {address} for {founder_name}, but the address does "
            f"not carry their name and no page we read attributes it to them.")
        return record

    cited = getattr(finding, "source_urls", None) or []
    record.evidence = [Evidence(
        url=cited[0] if cited else f"https://{finding.provider}.com/",
        quote=finding.provenance,
        authority=SourceAuthority.AGGREGATOR,
        note=f"contact database ({finding.provider}); not published on a page we read",
    )]
    record.status = EmailStatus.SOURCE_VERIFIED

    mx = await verifier.mx_for(domain_part(address))
    record.mx_ok = bool(mx)
    record.mx_hosts = mx[:4]
    if not mx:
        record.status = EmailStatus.INVALID
        record.notes.append(f"Domain {domain_part(address)} has no MX record.")
        return record

    deliv = await verifier.deliverability(address)
    record.deliverability = deliv.status
    record.verifier = deliv.provider
    record.is_catch_all = deliv.is_catch_all
    if deliv.detail:
        record.notes.append(f"Deliverability check ({deliv.provider}): {deliv.detail}")

    if deliv.status == "undeliverable":
        record.status = EmailStatus.INVALID
        record.notes.append("Deliverability provider reports this address is undeliverable.")
        return record
    if deliv.is_catch_all:
        record.notes.append(
            "Domain is catch-all, so a positive deliverability result proves nothing. "
            "Kept at source-verified rather than verified.")
        return record
    # The rungs this ladder was missing. Without them, "risky", "unknown" and
    # "no provider answered" all fell through to VERIFIED - so a database answer
    # nobody checked was indistinguishable from a confirmed mailbox.
    if deliv.status == "risky":
        record.notes.append("Deliverability provider flagged this address as risky; "
                            "not promoted to verified.")
        return record
    if deliv.status != "deliverable":
        record.notes.append(
            f"No deliverability confirmation for an address supplied by {finding.provider} "
            f"({deliv.detail or 'no answer'}). A database claim without a delivery check is "
            f"not verification, so this stays at source-verified.")
        return record

    record.status = EmailStatus.VERIFIED
    record.notes.append(
        f"Supplied by {finding.provider} with status '{finding.provider_status}', then "
        f"independently checked here (domain, MX"
        + (f", {deliv.provider}" if deliv.provider not in (None, "none") else "")
        + "). This address was not published on any page the agent read.")
    return record
