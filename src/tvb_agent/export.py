"""Export helpers shared by the UI and the CLI."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable

from .models import Lead

CSV_COLUMNS = [
    "company_name", "description", "industry_sector", "funding_or_revenue", "funding_type",
    "ceo_or_cofounder", "founder_title", "verified_email", "website", "country",
    "us_presence", "confidence", "tvb_fit_score", "evidence_urls",
]


def leads_to_rows(leads: Iterable[Lead]) -> list[dict]:
    return [lead_.export_row() for lead_ in leads]


def leads_to_csv(leads: Iterable[Lead]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in leads_to_rows(leads):
        writer.writerow(row)
    return buf.getvalue()


def leads_to_json(leads: Iterable[Lead], *, full_evidence: bool = True) -> str:
    """JSON export. With ``full_evidence`` the complete audit trail travels too."""
    out = []
    for lead in leads:
        row = lead.export_row()
        if full_evidence:
            c = lead.company
            row["evidence"] = [
                {"url": e.url, "quote": e.short(400), "authority": e.authority.value,
                 "title": e.title, "note": e.note}
                for e in c.all_evidence()
            ]
            row["gates"] = [
                {"gate": g.gate.value, "passed": g.passed, "reason": g.reason,
                 "evidence_urls": sorted({e.url for e in g.evidence})}
                for g in lead.qualification.gates
            ]
            row["email_detail"] = {
                "status": c.email.status.value,
                "mx_ok": c.email.mx_ok,
                "mx_hosts": c.email.mx_hosts,
                "deliverability": c.email.deliverability,
                "verifier": c.email.verifier,
                "is_catch_all": c.email.is_catch_all,
                "domain_matches_company": c.email.domain_matches_company,
                "role_fallback": c.email.role_fallback,
                "notes": c.email.notes,
            }
            row["discovered_via"] = c.discovered_via
            row["pages_fetched"] = c.pages_fetched
            row["near_boundary"] = lead.qualification.near_boundary
            row["fit_reasons"] = lead.qualification.fit_reasons
        out.append(row)
    return json.dumps(out, indent=2, ensure_ascii=False, default=str)
