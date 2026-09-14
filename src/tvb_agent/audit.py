"""Independent re-verification of an exported lead list.

Runs *after* a lead list exists and checks it from scratch: is each evidence URL
still reachable, does the quoted sentence still appear on the page, and does the
email domain still resolve?  A claim that cannot be re-verified is reported, not
quietly kept.

This exists because a pipeline marking its own homework is not evidence of data
quality.  Anyone can run this against the exported JSON and see for themselves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import Settings, get_settings
from .providers.email_verify import MXChecker, domain_part, is_role_account, is_valid_syntax
from .providers.fetcher import Fetcher
from .research.grounding import verify_quote
from .storage import Store


@dataclass
class EvidenceCheck:
    url: str
    reachable: bool
    quote_found: bool
    mode: str = ""
    note: str = ""


@dataclass
class LeadAudit:
    company: str
    email: str
    evidence: list[EvidenceCheck] = field(default_factory=list)
    email_syntax_ok: bool = False
    email_not_role: bool = False
    email_mx_ok: bool = False
    issues: list[str] = field(default_factory=list)

    @property
    def evidence_verified(self) -> int:
        return sum(1 for e in self.evidence if e.quote_found)

    @property
    def evidence_total(self) -> int:
        return len(self.evidence)

    @property
    def clean(self) -> bool:
        return not self.issues and self.evidence_verified > 0


@dataclass
class AuditReport:
    leads: list[LeadAudit] = field(default_factory=list)

    @property
    def clean_leads(self) -> int:
        return sum(1 for lead in self.leads if lead.clean)

    def summary(self) -> str:
        total = len(self.leads)
        if not total:
            return "Nothing to audit."
        ev_total = sum(lead_.evidence_total for lead_ in self.leads)
        ev_ok = sum(lead_.evidence_verified for lead_ in self.leads)
        lines = [
            "=" * 72,
            f"AUDIT: {self.clean_leads}/{total} leads re-verified cleanly",
            f"Evidence: {ev_ok}/{ev_total} quotes still found at their source URL",
            "=" * 72,
        ]
        for lead_ in self.leads:
            mark = "OK  " if lead_.clean else "FLAG"
            lines.append(f"[{mark}] {lead_.company}  <{lead_.email}>  "
                         f"evidence {lead_.evidence_verified}/{lead_.evidence_total}")
            for issue in lead_.issues:
                lines.append(f"        ! {issue}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "leads_total": len(self.leads),
            "leads_clean": self.clean_leads,
            "leads": [
                {
                    "company": lead_.company,
                    "email": lead_.email,
                    "email_syntax_ok": lead_.email_syntax_ok,
                    "email_not_role": lead_.email_not_role,
                    "email_mx_ok": lead_.email_mx_ok,
                    "evidence_verified": lead_.evidence_verified,
                    "evidence_total": lead_.evidence_total,
                    "issues": lead_.issues,
                    "evidence": [
                        {"url": e.url, "reachable": e.reachable,
                         "quote_found": e.quote_found, "mode": e.mode, "note": e.note}
                        for e in lead_.evidence
                    ],
                }
                for lead_ in self.leads
            ],
        }


async def audit_records(records: list[dict], settings: Settings | None = None,
                        *, max_evidence_per_lead: int = 6,
                        on_event=None) -> AuditReport:
    settings = settings or get_settings()
    emit = on_event or (lambda *a, **k: None)
    report = AuditReport()
    mx = MXChecker()

    timeout = httpx.Timeout(settings.http_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 headers={"User-Agent": settings.user_agent}) as client:
        fetcher = Fetcher(client, settings, Store(settings.db_path))

        for rec in records:
            name = rec.get("company_name") or "(unnamed)"
            email = rec.get("verified_email") or ""
            audit = LeadAudit(company=name, email=email)
            emit(f"auditing {name}")

            # --- the contact, re-checked from first principles ---
            if not email:
                audit.issues.append("No verified email present on this lead.")
            else:
                audit.email_syntax_ok = is_valid_syntax(email)
                audit.email_not_role = not is_role_account(email)
                hosts = await mx.mx_hosts(domain_part(email))
                audit.email_mx_ok = bool(hosts)
                if not audit.email_syntax_ok:
                    audit.issues.append(f"Email {email} is not syntactically valid.")
                if not audit.email_not_role:
                    audit.issues.append(f"Email {email} is a role account, not a personal contact.")
                if not audit.email_mx_ok:
                    audit.issues.append(f"Domain {domain_part(email)} has no MX record.")

            # --- the evidence, re-fetched and re-grounded ---
            items = rec.get("evidence") or []
            if not items:
                audit.issues.append("Lead carries no evidence records.")
            for item in items[:max_evidence_per_lead]:
                url, quote = item.get("url", ""), item.get("quote", "")
                page = await fetcher.fetch_safe(url)
                if not page.ok:
                    audit.evidence.append(EvidenceCheck(url, False, False,
                                                        note="page could not be fetched"))
                    continue
                res = verify_quote(quote, page.text)
                audit.evidence.append(EvidenceCheck(url, True, bool(res.ok), res.mode, res.detail))

            if audit.evidence and audit.evidence_verified == 0:
                audit.issues.append("None of the quoted evidence could be found at its source.")

            report.leads.append(audit)

    return report


def load_records(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("leads") or []
    return [r for r in data if isinstance(r, dict)]


async def audit_file(path: str | Path, settings: Settings | None = None, on_event=None) -> AuditReport:
    return await audit_records(load_records(path), settings, on_event=on_event)
