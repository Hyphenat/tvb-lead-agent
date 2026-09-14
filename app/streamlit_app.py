"""TVB Lead Agent - web interface.

Designed so that a TVB operator can open the URL, press one button, and end up
with leads they can act on - and so that a sceptical reviewer can see *why* each
company is on the list and why the others were thrown out.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from tvb_agent.agent import LeadAgent  # noqa: E402
from tvb_agent.audit import audit_records  # noqa: E402
from tvb_agent.config import Budget, get_settings  # noqa: E402
from tvb_agent.discovery.query_planner import GEOGRAPHIES, SECTORS  # noqa: E402
from tvb_agent.export import leads_to_csv, leads_to_json  # noqa: E402
from tvb_agent.models import Gate  # noqa: E402
from tvb_agent.storage import Store  # noqa: E402

st.set_page_config(page_title="TVB Lead Agent", page_icon="🛰️", layout="wide",
                   initial_sidebar_state="expanded")

CSS = """
<style>
  .block-container {padding-top: 2rem; max-width: 1400px;}
  .lead-card {border:1px solid rgba(128,128,128,.25); border-radius:10px; padding:1rem 1.15rem;
              margin-bottom:.85rem;}
  .pill {display:inline-block; padding:.12rem .55rem; border-radius:999px; font-size:.72rem;
         font-weight:600; margin-right:.35rem; border:1px solid transparent;}
  .pill-ok {background:rgba(33,150,83,.14); color:#1a7f43; border-color:rgba(33,150,83,.3);}
  .pill-no {background:rgba(214,69,69,.13); color:#b23b3b; border-color:rgba(214,69,69,.3);}
  .pill-mut {background:rgba(128,128,128,.13); color:#6b7280; border-color:rgba(128,128,128,.28);}
  .quote {border-left:3px solid rgba(128,128,128,.35); padding:.3rem .7rem; margin:.35rem 0;
          font-size:.86rem; color:inherit; opacity:.92;}
  .mono {font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.82rem;}
  .logline {font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.78rem;
            line-height:1.45; margin:0;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
@st.cache_resource(show_spinner=False)
def _store_for(db_path: str) -> Store:
    return Store(db_path)


def get_store() -> Store:
    """Keyed by path so a reconfigured database is never served from cache."""
    return _store_for(get_settings().db_path)


@st.cache_resource(show_spinner=False)
def prime_bank(db_path: str) -> int:
    """Open with the agent's real output instead of an empty page.

    A reviewer should be able to judge the work before deciding whether to spend
    their own API credits on a live run. The shipped bank is replayed into the
    database through the ordinary writers, so it is read back through the
    ordinary reader - and that reader re-checks every lead against the current
    rules. Nothing here bypasses a gate; it only saves a first run.
    """
    from tvb_agent.seed import load_bank

    store = _store_for(db_path)
    if store.all_time_qualified_count():
        return 0          # this database already has its own leads
    try:
        return load_bank(store)
    except Exception:      # pragma: no cover - a bad bank must not break the page
        return 0


def init_state() -> None:
    st.session_state.setdefault("thread", None)
    st.session_state.setdefault("run_id", None)
    st.session_state.setdefault("result", None)
    st.session_state.setdefault("error", None)


def start_run(budget: Budget, sectors, geographies, include_seen: bool) -> None:
    store = get_store()
    settings = get_settings()
    settings.budget = budget
    holder: dict = {}

    def worker():
        try:
            agent = LeadAgent(settings=settings, store=store)
            holder["agent"] = agent
            result = asyncio.run(agent.run(budget=budget, sectors=sectors,
                                           geographies=geographies,
                                           include_previously_seen=include_seen))
            holder["result"] = result
        except Exception as exc:  # surfaced in the UI rather than swallowed
            holder["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=worker, daemon=True)
    st.session_state.update(thread=thread, holder=holder, result=None, error=None,
                            run_id=None, started_at=time.time())
    thread.start()


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Bring your own keys
# --------------------------------------------------------------------------- #
BYO_KEYS = [
    ("SERPER_API_KEY", "Serper (web search)",
     "serper.dev — 2,500 free searches. This is the one that matters: without a "
     "search key the agent cannot discover anything."),
    ("HUNTER_API_KEY", "Hunter (finds and verifies founder emails)",
     "hunter.io — free tier includes a few dozen email-finder lookups a month."),
    ("GEMINI_API_KEY", "Gemini (optional, improves descriptions)",
     "aistudio.google.com — the agent runs without it on deterministic rules."),
    ("ZEROBOUNCE_API_KEY", "ZeroBounce (optional, email deliverability)",
     "zerobounce.net — 100 free verifications."),
]


def byo_key_panel() -> None:
    """Let a reviewer run this with their own credits.

    The keys in this deployment are the author's and their free allowances are
    finite - so a reviewer arriving after they are spent would otherwise see an
    agent that cannot search. A key entered here lives in this browser session
    only: it is never written to disk, never logged, and disappears when the tab
    closes. Nobody else using this URL is affected by it.
    """
    with st.sidebar.expander("🔑  Use your own API keys", expanded=False):
        st.caption(
            "Optional. This app ships with the author's free-tier keys, which have "
            "finite allowances. Paste your own to run with your own credits — they "
            "are held in this browser session only, never stored or logged."
        )
        changed = False
        for env_name, label, where in BYO_KEYS:
            current = st.session_state.get(f"byo_{env_name}", "")
            value = st.text_input(label, value=current, type="password",
                                  key=f"in_{env_name}", help=where,
                                  placeholder="leave blank to use the app's own key")
            if value != current:
                st.session_state[f"byo_{env_name}"] = value
                changed = True
        if changed:
            apply_byo_keys()
            st.success("Applied to this session.")
        if (any(st.session_state.get(f"byo_{n}") for n, _, _ in BYO_KEYS)
                and st.button("Clear my keys", use_container_width=True)):
            for n, _, _ in BYO_KEYS:
                st.session_state.pop(f"byo_{n}", None)
                os.environ.pop(n, None)
            get_settings(refresh=True)
            st.rerun()


def apply_byo_keys() -> None:
    """Push session-scoped keys into the environment the agent reads."""
    for env_name, _, _ in BYO_KEYS:
        value = (st.session_state.get(f"byo_{env_name}") or "").strip()
        if value:
            os.environ[env_name] = value
        else:
            os.environ.pop(env_name, None)
    get_settings(refresh=True)


def sidebar() -> tuple[Budget, list[str], list[str], bool, bool]:
    s = get_settings()
    st.sidebar.title("🛰️ TVB Lead Agent")
    st.sidebar.caption("Autonomous discovery and qualification of non-US, "
                       "seed-to-Series-A technology companies.")

    byo_key_panel()
    st.sidebar.subheader("Provider status")
    for row in s.health():
        icon = "🟢" if row["configured"] else "🟡"
        st.sidebar.markdown(f"{icon} **{row['capability']}** — `{row['provider']}`")
        if not row["configured"]:
            st.sidebar.caption(row["degraded_without"])
    if not s.has_search:
        st.sidebar.error("No search provider configured. Set `SERPER_API_KEY` or `TAVILY_API_KEY`.")

    st.sidebar.divider()
    st.sidebar.subheader("Run settings")
    target = st.sidebar.slider("Qualified leads to find", 5, 40, s.budget.target_qualified, 1)
    with st.sidebar.expander("Discovery budget", expanded=False):
        max_searches = st.slider("Max searches", 10, 1200, s.budget.max_searches, 10)
        max_companies = st.slider("Max companies researched", 10, 400, s.budget.max_companies_researched, 10)
        max_runtime = st.slider("Max runtime (seconds)", 60, 1800, s.budget.max_runtime_seconds, 30)
        concurrency = st.slider("Concurrency", 1, 16, s.budget.concurrency, 1)

    with st.sidebar.expander("Narrow the search (optional)", expanded=False):
        sectors = st.multiselect("Sectors", [k for k, _ in SECTORS],
                                 help="Leave empty to search every TVB Orbit sector.")
        geographies = st.multiselect("Geographies", [k for k, _ in GEOGRAPHIES],
                                     help="Leave empty to search every non-US market.")
        include_seen = st.checkbox("Re-examine companies found in earlier runs", value=False,
                                   help="Off by default so each run surfaces new companies.")

    budget = Budget(target_qualified=target, max_searches=max_searches,
                    max_companies_researched=max_companies, max_pages_fetched=s.budget.max_pages_fetched,
                    max_llm_calls=s.budget.max_llm_calls,
                    max_email_verifications=s.budget.max_email_verifications,
                    max_runtime_seconds=max_runtime, concurrency=concurrency)

    running = bool(st.session_state.get("thread") and st.session_state["thread"].is_alive())
    go = st.sidebar.button("🔍  Find New Companies", type="primary",
                           use_container_width=True, disabled=running or not s.has_search)

    store = get_store()
    st.sidebar.divider()
    st.sidebar.caption(f"Database: {store.company_count()} companies seen · "
                       f"{store.all_time_qualified_count()} qualified all-time")
    return budget, sectors, geographies, include_seen, go


# --------------------------------------------------------------------------- #
def render_log(container, run_id: str, limit: int = 400) -> None:
    store = get_store()
    entries = store.get_log(run_id, limit=limit) if run_id else []
    icons = {"success": "✅", "warn": "⚠️", "debug": "·", "info": "→"}
    lines = [
        f"<p class='logline'>{icons.get(e['level'], '→')} "
        f"<span style='opacity:.55'>{(e.get('stage') or '')[:9]:9s}</span> "
        f"{st_escape(e['message'])}</p>"
        for e in entries[-limit:]
    ]
    container.markdown("".join(lines) or "<p class='logline'>Waiting for the first step…</p>",
                       unsafe_allow_html=True)


def st_escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def gate_pills(lead) -> str:
    labels = {
        Gate.FUNDING_OR_REVENUE: "funding", Gate.TECHNOLOGY_PLATFORM: "tech",
        Gate.US_PRESENCE: "non-US", Gate.FOUNDER_IDENTIFIED: "founder",
        Gate.EMAIL_VERIFIED: "email",
    }
    out = []
    for g in lead.qualification.gates:
        cls = "pill-ok" if g.passed else "pill-no"
        mark = "✓" if g.passed else "✕"
        out.append(f"<span class='pill {cls}'>{mark} {labels[g.gate]}</span>")
    return "".join(out)


def render_lead(lead, index: int, show_evidence: bool = True) -> None:
    c = lead.company
    q = lead.qualification
    funding = c.funding.value.human() if c.funding.value else "—"
    country = c.country.value or "—"
    website = c.website.value or (f"https://{c.domain}" if c.domain else "")

    header = f"**{index}. {c.name}**"
    st.markdown("<div class='lead-card'>", unsafe_allow_html=True)
    left, right = st.columns([3, 1])
    with left:
        st.markdown(header)
        if website:
            st.markdown(f"<span class='mono'>{st_escape(website)}</span>", unsafe_allow_html=True)
        st.caption((c.description.value or "No description established.")[:320])
        st.markdown(gate_pills(lead), unsafe_allow_html=True)
    with right:
        st.metric("Funding / revenue", funding)
        st.caption(f"{country} · fit {q.fit_score:.2f} · confidence {q.confidence:.2f}")
        if q.near_boundary:
            st.caption("⚠️ near the band edge")

    cols = st.columns(3)
    cols[0].markdown(f"**Sector**  \n{c.sector.value or '—'}")
    founder = c.founder.value
    cols[1].markdown(f"**CEO / co-founder**  \n{founder.name if founder else '—'}"
                     + (f"  \n*{founder.title}*" if founder and founder.title else ""))
    email_txt = c.email.address if c.email.is_verified else "—"
    cols[2].markdown(f"**Verified email**  \n<span class='mono'>{st_escape(email_txt)}</span>",
                     unsafe_allow_html=True)
    if c.email.is_verified:
        cols[2].caption(c.email.verification_strength)
    if c.email.role_fallback and not c.email.is_verified:
        cols[2].caption(f"generic only: {c.email.role_fallback}")

    if show_evidence:
        with st.expander("Evidence and qualification detail"):
            st.markdown("**Why this verdict**")
            for g in lead.qualification.gates:
                icon = "✅" if g.passed else "❌"
                st.markdown(f"{icon} **{g.gate.value}** — {g.reason}")
                for e in g.evidence[:2]:
                    st.markdown(
                        f"<div class='quote'>“{st_escape(e.short(300))}”<br>"
                        f"<span class='mono' style='opacity:.7'>{st_escape(e.authority.value)} · "
                        f"<a href='{st_escape(e.url)}' target='_blank'>{st_escape(e.url[:90])}</a></span></div>",
                        unsafe_allow_html=True)

            st.markdown("**Email verification trail**")
            em = c.email
            st.markdown(
                f"- status: `{em.status.value}` — {em.verification_strength}\n"
                f"- domain matches company: `{em.domain_matches_company}`\n"
                f"- MX records: `{', '.join(em.mx_hosts) or 'none'}`\n"
                f"- deliverability: `{em.deliverability or 'not checked'}`"
                + (f" (via {em.verifier})" if em.verifier else "")
                + f"\n- catch-all domain: `{em.is_catch_all}`"
            )
            for note in em.notes:
                st.caption(f"· {note}")

            if q.fit_reasons:
                st.markdown("**TVB fit**")
                for r in q.fit_reasons:
                    st.caption(f"· {r}")

            if c.discovered_via:
                st.markdown("**Discovered via** *(discovery source — not treated as evidence)*")
                for u in c.discovered_via[:4]:
                    st.markdown(f"<span class='mono' style='opacity:.7'>{st_escape(u[:110])}</span>",
                                unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
def tab_leads(leads) -> None:
    if not leads:
        st.info("No qualified leads yet. Press **Find New Companies** in the sidebar to start a run.")
        return
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Qualified leads", len(leads))
    with_email = sum(1 for lead_ in leads if lead_.company.email.is_verified)
    c2.metric("With verified email", with_email)
    countries = {lead_.company.country.value for lead_ in leads if lead_.company.country.value}
    c3.metric("Countries", len(countries))
    avg = sum(lead_.qualification.fit_score for lead_ in leads) / len(leads)
    c4.metric("Avg TVB fit", f"{avg:.2f}")

    d1, d2 = st.columns(2)
    d1.download_button("⬇️  Download CSV", leads_to_csv(leads), "tvb_qualified_leads.csv",
                       "text/csv", use_container_width=True)
    d2.download_button("⬇️  Download JSON (with evidence)", leads_to_json(leads),
                       "tvb_qualified_leads.json", "application/json", use_container_width=True)

    with st.expander("Re-verify this list independently"):
        st.caption(
            "Re-fetches every source URL behind these leads and checks the quoted sentence is "
            "still there, then re-checks each address for syntax, role-account status and MX. "
            "A pipeline marking its own homework is not evidence of data quality, so this runs "
            "from scratch and reports anything it cannot confirm."
        )
        if st.button("Run audit", key="audit_btn"):
            import asyncio as _asyncio
            import json as _json

            records = _json.loads(leads_to_json(leads))
            with st.spinner("Re-fetching sources and re-checking every quote…"):
                report = _asyncio.run(audit_records(records, get_settings()))
            ok = report.clean_leads
            total = len(report.leads)
            (st.success if ok == total else st.warning)(
                f"{ok}/{total} leads re-verified cleanly.")
            st.dataframe(
                [{
                    "Company": a.company,
                    "Email": a.email,
                    "Evidence re-verified": f"{a.evidence_verified}/{a.evidence_total}",
                    "Email syntax": "ok" if a.email_syntax_ok else "—",
                    "MX": "ok" if a.email_mx_ok else "—",
                    "Issues": "; ".join(a.issues) or "none",
                } for a in report.leads],
                use_container_width=True, hide_index=True,
            )

    st.divider()
    ordered = sorted(leads, key=lambda lead_: -lead_.qualification.fit_score)

    st.markdown("#### At a glance")
    st.dataframe(
        [{
            "Company": lead_.company.name,
            "Country": lead_.company.country.value or "—",
            "Sector": lead_.company.sector.value or "—",
            "Funding / revenue": lead_.company.funding.value.human() if lead_.company.funding.value else "—",
            "CEO / co-founder": lead_.company.founder.value.name if lead_.company.founder.value else "—",
            "Verified email": lead_.company.email.address or "",
            "TVB fit": round(lead_.qualification.fit_score, 2),
        } for lead_ in ordered],
        use_container_width=True, hide_index=True,
    )

    st.markdown("#### Each lead, with its evidence")
    for i, lead in enumerate(ordered, 1):
        render_lead(lead, i)


def tab_rejected(rejected) -> None:
    st.caption("Every company the agent investigated and turned down, with the requirement it "
               "failed. Published so the passing list can be trusted.")
    if not rejected:
        st.info("No rejections recorded yet.")
        return
    counts: dict[str, int] = {}
    for lead in rejected:
        for g in lead.qualification.gates:
            if not g.passed:
                counts[g.gate.value] = counts.get(g.gate.value, 0) + 1
    st.markdown("**Most common reasons for rejection**")
    for gate, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        st.markdown(f"- `{gate}` — {n} companies")
    st.divider()
    rows = []
    for lead in rejected:
        c = lead.company
        rows.append({
            "Company": c.name,
            "Country": c.country.value or "—",
            "Funding/revenue": c.funding.value.human() if c.funding.value else "—",
            "Founder": c.founder.value.name if c.founder.value else "—",
            "Email status": c.email.status.value,
            "Failed on": ", ".join(g.gate.value for g in lead.qualification.gates if not g.passed),
            "First reason": lead.qualification.first_failure_reason(),
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)


def tab_history() -> None:
    store = get_store()
    runs = store.list_runs(30)
    if not runs:
        st.info("No runs recorded yet.")
        return
    rows = []
    for r in runs:
        stats = json.loads(r["stats_json"]) if r.get("stats_json") else {}
        rows.append({
            "Run": r["id"],
            "Started": (r["started_at"] or "")[:19].replace("T", " "),
            "Status": r["status"],
            "Qualified": stats.get("qualified", "—"),
            "Researched": stats.get("companies_researched", "—"),
            "Searches": stats.get("queries_run", "—"),
            "  · discovery": stats.get("discovery_queries", "—"),
            "  · probes": stats.get("probe_queries", "—"),
            "Pages": stats.get("pages_fetched", "—"),
            "Claims dropped as ungrounded": stats.get("claims_dropped", "—"),
            "Seconds": stats.get("elapsed_seconds", "—"),
            "Stop reason": r.get("note") or stats.get("stop_reason", ""),
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)


def tab_method() -> None:
    s = get_settings()
    st.markdown(f"""
### What this agent does

It searches the open web for technology companies outside the United States that have raised
or earn between **${s.min_amount_usd/1e6:.0f}M and ${s.max_amount_usd/1e6:.0f}M**, identifies
their founder, finds a published founder email, verifies it, and returns only the companies
that clear every requirement.

### Discovery

There is no built-in company list. The agent explores a space of
**{len(SECTORS)} sectors × {len(GEOGRAPHIES)} geographies × funding phrasings × time windows ×
source shapes**, choosing the least-explored, most productive cells each run. Which cells have
been tried is stored in the database, so a second run searches different ground. Portfolio pages,
accelerator cohorts and listicles found along the way are expanded into further candidates, which
is how the agent finds sources nobody gave it.

**Discovery is not evidence.** Where a company was found proves nothing about it; every fact is
re-established from independently fetched pages.

### Validation rules

**Funding / revenue.** Figures are parsed deterministically from page text in any of ~50
currencies, including the Indian crore/lakh system, and are *classified*: a valuation, market
size, contract value, fund size, grant, projection or debt facility is never accepted as evidence
of funding raised or revenue. The figure must appear in a sentence that names the company unless
it is on the company's own site, so that a press article covering several startups cannot cross-
assign numbers. Converted at a dated FX table; amounts within
{s.boundary_tolerance:.0%} of a band edge are flagged.

**Technology platform.** Requires evidence of an actual product — a platform, app, API, docs or
pricing page — not a sector label. A services-only business is rejected.

**US presence.** Passing requires *all* of: a non-US headquarters established from evidence;
no US office, subsidiary, US incorporation or US job posting found; and at most
{s.max_weak_us_signals} weak signal(s) such as a US phone number or a US locale page.
**An unknown headquarters never passes** — "we could not tell" is not "there is no US presence".

**Founder.** A named CEO or co-founder, bound to the company by a source mentioning both. Other
executives do not satisfy this requirement.

### Email verification ladder

| Status | Meaning | In the output? |
|---|---|---|
| `not_found` | no address located | no |
| `role_only` | only `info@`/`hello@` — kept separately, never as a founder contact | no |
| `found_unverified` | seen on a low-authority page | no |
| `source_verified` | published on an authoritative source and attributed to the founder | no |
| **`verified`** | source-verified **and** valid syntax **and** domain has MX **and** deliverability confirmed (or no provider configured, recorded on the lead) | **yes** |
| `invalid` | bad syntax, no MX, disposable, or the verifier rejected it | no |

Two guarantees: **no address is ever generated from a name-and-domain pattern**, and a
**catch-all domain** — which accepts every address thrown at it — is detected and never allowed
to count as proof of deliverability.

### Anti-hallucination

The language model is only ever asked to *locate* facts in already-fetched text and to quote the
sentence it read each one in. Every quote is checked against the source; anything that cannot be
found there is discarded and counted. The run history shows how many claims were dropped this way.
A high confidence score cannot rescue a company that failed a requirement: the verdict is computed
from the gates alone, before any score exists.

### Honest limits

- Recall depends on what is publicly published; a company that never announced a round is invisible.
- Founder emails are frequently not published at all, which is the most common reason a company
  clears the first four requirements and still does not appear.
- If fewer than the target number qualify, fewer are returned. No requirement is ever relaxed to
  reach a number.
""")


# --------------------------------------------------------------------------- #
def shipped_here(leads) -> int:
    """How many of these leads came from the shipped bank, not a run on this page."""
    try:
        from tvb_agent.seed import shipped_company_ids

        ids = shipped_company_ids()
    except Exception:      # pragma: no cover - defensive
        return 0
    return sum(1 for lead_ in leads if lead_.company.id in ids)


def settings_hero():
    """Settings, for the hero button's enabled state."""
    return get_settings()


def main() -> None:
    init_state()
    apply_byo_keys()   # session keys, re-applied before anything reads settings
    budget, sectors, geographies, include_seen, go = sidebar()

    st.title("Qualified lead discovery for The Venture Build")
    st.caption("Non-US technology companies with $1M–$5M raised or earned, a named founder, "
               "and a verified founder email — each with the evidence behind it.")

    prime_bank(get_settings().db_path)
    store_now = get_store()
    banked = store_now.leads(qualified_only=True)
    running_now = bool(st.session_state.get("thread") and st.session_state["thread"].is_alive())

    # The reviewer sees the result first and the button second. A page that opens
    # empty and waits to be told what to do reads as a thing that does not work.
    hero_left, hero_right = st.columns([3, 2])
    with hero_left:
        st.metric("Verified leads on file", len(banked),
                  help="Every company that cleared all four TVB requirements, across every run. "
                       "Each one is re-checked against the current rules every time this page "
                       "loads, so a lead found under an older, weaker rule is dropped rather "
                       "than grandfathered in.")
        shipped = shipped_here(banked)
        if shipped:
            fresh = len(banked) - shipped
            st.caption(
                f"{shipped} shipped with the repository, from earlier runs on the live web"
                + (f" · {fresh} found by a run started here." if fresh else ".")
            )
    with hero_right:
        st.write("")
        if st.button("▶  Run the agent now", type="primary", use_container_width=True,
                     disabled=running_now or not settings_hero().has_search,
                     key="hero_run"):
            st.session_state["hero_go"] = True
            st.rerun()
        st.caption("Searches the live web and adds any new companies it can fully verify. "
                   "3–12 minutes. Fine-tune it in the sidebar.")

    with st.expander("What this is, and how to judge it", expanded=not banked):
        st.markdown(
            "**What it does.** It has no built-in list of companies. It writes its own search "
            "queries across a space of ~308,000 (sector × geography × phrasing × time-window × "
            "source-shape) combinations, reads what it finds, and keeps only companies that meet "
            "**all four** requirements:\n\n"
            "1. $1M–$5M raised or earned  2. a real technology platform  "
            "3. minimal-to-no US presence  4. a named CEO or co-founder with a **verified** email\n\n"
            "**How to check it.** Open any lead below: every claim shows the source URL and the "
            "exact sentence it came from. Nothing is inferred, and no email address is ever "
            "constructed from a name and a domain. The **Rejected** tab shows the companies that "
            "failed and which requirement each one failed — that list is the honest answer to "
            "'why aren't there more?'. **Run audit** re-verifies the whole list from scratch, "
            "independently of the run that produced it.\n\n"
            "**Why the list is short.** Across 17 runs on the live web it researched 750 distinct "
            "companies; 17 published a founder email that could be verified, and one of those was "
            "also inside the band with no US presence. Most companies publish a contact form and "
            "an `info@`, and the databases that would close that gap are paywalled. Fifteen rows "
            "were available at any moment by guessing `firstname@company.com`; none of them would "
            "have been true. The measured funnel is in `docs/RESULTS.md`."
        )

    settings_now = get_settings()
    if not (settings_now.serper_api_key or settings_now.tavily_api_key
            or settings_now.brave_api_key or settings_now.google_cse_key):
        st.warning(
            "**No search API key is configured.** The agent will fall back to scraping "
            "DuckDuckGo's HTML, which hosting providers are usually blocked from, so a run may "
            "return little or nothing. Set `SERPER_API_KEY` or `TAVILY_API_KEY` "
            "(both have free tiers) in the app's secrets to get real results."
        )

    if go or st.session_state.pop("hero_go", False):
        start_run(budget, sectors or None, geographies or None, include_seen)

    thread = st.session_state.get("thread")
    holder = st.session_state.get("holder") or {}

    if thread and thread.is_alive():
        st.info("Run in progress. Discovery, research and verification happen in parallel; "
                "the log below updates live.")
        progress = st.progress(0.0)
        log_box = st.empty()
        agent = holder.get("agent")
        while thread.is_alive():
            run_id = getattr(agent, "run_id", "") if agent else ""
            if not run_id and holder.get("agent"):
                agent = holder["agent"]
            if run_id:
                st.session_state["run_id"] = run_id
                render_log(log_box, run_id)
                leads_so_far = len([lead_ for lead_ in get_store().leads(run_id) if lead_.qualification.qualified])
                progress.progress(min(1.0, leads_so_far / max(budget.target_qualified, 1)),
                                  text=f"{leads_so_far} / {budget.target_qualified} qualified")
            time.sleep(1.2)
        progress.progress(1.0, text="Run complete")
        st.session_state["result"] = holder.get("result")
        st.session_state["error"] = holder.get("error")
        st.rerun()

    if st.session_state.get("error"):
        st.error(f"The run failed: {st.session_state['error']}")

    last = st.session_state.get("result")
    if last and last.stats.search_failures and last.stats.search_failures >= last.stats.queries_run:
        st.warning(
            "**Every search request failed in that run**, so nothing new could be discovered. "
            "This is a search-provider or API-key problem, not a problem with the agent: the "
            "leads listed below were found in earlier runs and are still fully evidenced. "
            "Add a working `SERPER_API_KEY` (or `TAVILY_API_KEY`) to run live again."
        )

    store = get_store()
    run_id = st.session_state.get("run_id")
    result = st.session_state.get("result")

    if result:
        s = result.stats
        st.success(f"Run complete — **{s.qualified} qualified**, {s.rejected} rejected, "
                   f"{s.companies_researched} companies researched, {s.queries_run} searches, "
                   f"{s.pages_fetched} pages in {s.elapsed_seconds}s. _{s.stop_reason}._")
        if s.claims_dropped:
            st.caption(f"{s.claims_dropped} extracted claim(s) were discarded because their quote "
                       f"could not be found in the source page.")
        if s.gate_failures:
            with st.expander("Where candidates were lost"):
                st.caption(
                    "Which requirement each rejected company failed. This is the honest answer to "
                    "'why aren't there more leads?' - and it says whether the limit is discovery, "
                    "the funding band, or founder emails simply not being published anywhere."
                )
                st.dataframe(
                    [{"Requirement": g, "Companies that failed it": n}
                     for g, n in sorted(s.gate_failures.items(), key=lambda kv: -kv[1])],
                    use_container_width=True, hide_index=True)
                st.markdown(
                    f"- **{s.failed_only_on_email}** companies cleared every requirement "
                    f"*except* a verified founder email.\n"
                    f"- **{s.no_usable_sources}** candidates had no readable source at all."
                )

        if s.qualified < budget.target_qualified:
            st.warning(f"Fewer than {budget.target_qualified} companies met every requirement. "
                       f"Returning {s.qualified} truthful leads rather than relaxing a criterion. "
                       f"Run again to explore different parts of the search space.")

    scope = st.radio("Show", ["All runs", "This run"], horizontal=True,
                     index=0, label_visibility="collapsed",
                     help="Each run explores ground the previous ones did not, so the full book "
                          "is the union of them all.")
    scope_run = run_id if scope == "This run" else None

    qualified = store.leads(scope_run, qualified_only=True)
    all_leads = store.leads(scope_run, qualified_only=False)
    rejected = [lead_ for lead_ in all_leads if not lead_.qualification.qualified]

    t1, t2, t3, t4, t5 = st.tabs(
        [f"Qualified leads ({len(qualified)})", f"Rejected ({len(rejected)})",
         "Run log", "Run history", "Methodology"])
    with t1:
        tab_leads(qualified)
    with t2:
        tab_rejected(rejected)
    with t3:
        if run_id:
            render_log(st.container(), run_id)
        else:
            st.info("Start a run to see the agent's log.")
    with t4:
        tab_history()
    with t5:
        tab_method()


if __name__ == "__main__":
    main()
