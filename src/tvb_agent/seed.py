"""The leads that shipped with the repository.

A reviewer who opens the hosted app should see the agent's actual output, not an
empty page and an invitation to spend their own API credits first.  But the
output must not become a static list pasted into the UI, because then nothing
distinguishes a real finding from a claim.

So the bank travels as a **replay of the database rows themselves**: the same
``CompanyProfile`` and ``Qualification`` objects the agent wrote when it found
the company, evidence and gate reasons intact.  On load they go back into SQLite
through the ordinary writers, which means they are read back through the
ordinary reader - and ``Store.leads`` re-checks every lead against the current
rules on every read.  A shipped lead that a later fix would now reject is
dropped exactly like a locally-found one.  Nothing here is grandfathered in.

The run rows travel too, with their original ids and timestamps, so the Run
history tab shows which run found each lead and what that run cost.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import CompanyProfile, Qualification
from .storage import Store

def _default_path() -> Path:
    """Find data/shipped_leads.json from wherever this package happens to live.

    The repository layout puts it three levels up from this file; a deployment
    that runs from the repository root finds it there. An installed copy finds
    neither, and every caller treats a missing file as "nothing shipped".
    """
    here = Path(__file__).resolve()
    candidates = [here.parents[2] / "data" / "shipped_leads.json",
                  Path.cwd() / "data" / "shipped_leads.json"]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


SHIPPED_PATH = _default_path()

FORMAT = 1


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _record(lead, *, qualified: bool) -> dict[str, Any]:
    """One lead as it travels in the bank.

    A **rejected** company is carried for its gate reasons, not as a contact, so
    every address in its record is masked before the file is committed to a
    public repository. Its name, domain, funding and the requirement it failed
    stay legible; the mailbox does not travel. A qualified lead keeps its
    address - that address is the deliverable, and it was found published on a
    page this agent read and quoted.
    """
    company = json.loads(lead.company.model_dump_json())
    qual = json.loads(lead.qualification.model_dump_json())
    if not qualified:
        company = json.loads(_EMAIL_RE.sub("[address withheld]", json.dumps(company)))
        qual = json.loads(_EMAIL_RE.sub("[address withheld]", json.dumps(qual)))
    return {"run_id": lead.run_id, "qualified": qualified,
            "company": company, "qualification": qual}


def export_bank(store: Store, path: str | Path, *, near_misses: int = 80) -> int:
    """Write every verified lead on file, with the runs that produced them.

    Companies that were researched and rejected travel too, up to
    ``near_misses`` of them.  They are the honest answer to "why aren't there
    more?": each one carries the gate it failed and the sentence behind that
    verdict, so a reviewer can see the funnel rather than take the final number
    on trust.  They are stored as rejections and can never be read back as
    leads - ``Store.leads(qualified_only=True)`` filters on the stored verdict.
    """
    leads = store.leads(qualified_only=True)
    rejected = [lead for lead in store.leads(qualified_only=False)
                if not lead.qualification.qualified]
    # The most informative rejections first: a company that failed one gate says
    # more about where the pipeline stops than one that failed four.
    rejected.sort(key=lambda lead: sum(1 for g in lead.qualification.gates if not g.passed))
    rejected = rejected[:max(0, near_misses)]
    records = leads + rejected
    wanted = {lead.run_id for lead in records}
    runs = [r for r in store.list_runs(200) if r["id"] in wanted]
    payload: dict[str, Any] = {
        "format": FORMAT,
        "note": ("Leads and rejections exported from real runs. Replayed into the database "
                 "on load and re-checked against the current rules like any other lead; "
                 "a rejection stays a rejection."),
        "runs": runs,
        "leads": [
            _record(lead, qualified=bool(lead.qualification.qualified))
            for lead in records
        ],
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(leads)   # the count that matters: verified leads, not rejections


def load_bank(store: Store, path: str | Path | None = None) -> int:
    """Replay a shipped bank into ``store``.  Idempotent; returns leads loaded."""
    p = Path(path) if path else SHIPPED_PATH
    if not p.exists():
        return 0
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        return 0

    for row in payload.get("runs") or []:
        if isinstance(row, dict) and row.get("id"):
            store.record_run_row(row)

    loaded = 0
    for rec in payload.get("leads") or []:
        try:
            profile = CompanyProfile.model_validate(rec["company"])
            qual = Qualification.model_validate(rec["qualification"])
        except Exception:
            # A record this build can no longer parse is a record this build
            # cannot stand behind. Skip it rather than guess at its shape.
            continue
        run_id = str(rec.get("run_id") or "shipped")
        store.upsert_company(profile, run_id)
        store.save_qualification(profile.id, run_id, qual)
        loaded += 1
    return loaded


def shipped_company_ids(path: str | Path | None = None) -> set[str]:
    """Which companies came from the shipped bank rather than a live run here.

    The UI uses this to say plainly which leads it opened with and which ones a
    reviewer's own run added, so the two are never quietly mixed.
    """
    p = Path(path) if path else SHIPPED_PATH
    if not p.exists():
        return set()
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    ids = set()
    for rec in (payload or {}).get("leads") or []:
        if not (rec or {}).get("qualified"):
            continue           # a shipped rejection is not a shipped lead
        cid = ((rec or {}).get("company") or {}).get("id")
        if cid:
            ids.add(str(cid))
    return ids
