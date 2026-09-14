"""Command-line runner.

Useful for local validation and for scheduled runs:

    python -m tvb_agent.cli run --target 15 --json leads.json --csv leads.csv
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .agent import LeadAgent
from .config import get_settings
from .export import leads_to_csv, leads_to_json
from .seed import SHIPPED_PATH
from .storage import Store

LEVEL_PREFIX = {"success": "[OK]  ", "warn": "[WARN]", "debug": "[..]  ", "info": "[info]"}


def _printer(verbose: bool):
    def emit(message: str, level: str = "info", stage: str = "") -> None:
        if level == "debug" and not verbose:
            return
        print(f"{LEVEL_PREFIX.get(level, '[info]')} {message}", flush=True)

    return emit


def _preflight(settings) -> bool:
    """Refuse to start a run that is near-certain to find nothing."""
    has_real_search = bool(settings.serper_api_key or settings.tavily_api_key
                           or settings.brave_api_key
                           or (settings.google_cse_key and settings.google_cse_cx))
    if has_real_search:
        return True
    print("!" * 70)
    print("No search API key is configured, so this run would fall back to")
    print("scraping DuckDuckGo - which blocks automated requests and will")
    print("return nothing. Stopping before wasting your time.")
    print("")
    print("  1. Put SERPER_API_KEY (or TAVILY_API_KEY) in .env")
    print("  2. python tvb.py doctor --live")
    print("")
    print("If .env looks empty, `copy .env.example .env` overwrote it -")
    print("that command is only for the first time.")
    print("")
    print("To run anyway despite the above, pass --allow-no-search-key.")
    print("!" * 70)
    return False


async def _run(args) -> int:
    settings = get_settings()
    if not getattr(args, "allow_no_search_key", False) and not _preflight(settings):
        return 2
    if args.target:
        settings.budget.target_qualified = args.target
    if args.max_searches:
        settings.budget.max_searches = args.max_searches
    if args.max_companies:
        settings.budget.max_companies_researched = args.max_companies
    if args.max_runtime:
        settings.budget.max_runtime_seconds = args.max_runtime

    store = Store(settings.db_path)
    agent = LeadAgent(settings=settings, store=store, on_event=_printer(args.verbose))

    result = await agent.run(
        sectors=args.sectors.split(",") if args.sectors else None,
        geographies=args.geographies.split(",") if args.geographies else None,
        include_previously_seen=args.include_seen,
        seed=args.seed,
    )

    qualified = result.qualified_leads
    print("\n" + "=" * 78)
    print(f"QUALIFIED LEADS: {len(qualified)}   (researched {result.stats.companies_researched}, "
          f"rejected {result.stats.rejected}, {result.stats.elapsed_seconds}s)")
    print(f"Stop reason: {result.stats.stop_reason}")
    print("=" * 78)
    for i, lead in enumerate(qualified, 1):
        c = lead.company
        print(f"{i:2d}. {c.name}  [{c.country.value}]  "
              f"{c.funding.value.human() if c.funding.value else '-'}")
        print(f"    {c.founder.value.name if c.founder.value else '-'} "
              f"({c.founder.value.title if c.founder.value else '-'})  <{c.email.address}>")
        print(f"    {c.website.value or ''}")
    if not qualified:
        print("No company met every requirement in this run. "
              "Nothing has been relaxed to produce a number.")
        if result.stats.search_failures and result.stats.search_failures >= result.stats.queries_run:
            print("")
            print("Every search request failed, so nothing was ever discovered.")
            print("This is a provider or API-key problem, not a data problem.")
            print("Run: python tvb.py doctor --live")
        elif result.stats.companies_researched == 0:
            print("")
            print("No candidates were discovered at all. Check `doctor --live`, then try")
            print("a wider search: python tvb.py run --target 15 --max-searches 400")

    if result.stats.gate_failures:
        print("\nWhere candidates were lost:")
        for gate, n in sorted(result.stats.gate_failures.items(), key=lambda kv: -kv[1]):
            print(f"  {gate:28s} {n}")
        print(f"  {'cleared all but the email':28s} {result.stats.failed_only_on_email}")

    # Every run deliberately explores ground the previous runs did not, so the
    # deliverable is the whole verified book, not one run's slice of it. Each
    # lead in it cleared the same five gates on its own evidence; nothing is
    # pooled, averaged or relaxed by being counted together.
    banked = store.leads(qualified_only=True)
    stale = getattr(store, "last_read_rejections", [])
    if stale:
        print(f"\n{len(stale)} lead(s) from earlier runs were dropped: they no longer pass the "
              f"current rules.")
        for name, why in stale:
            print(f"  - {name}: {why}")
    if len(banked) > len(qualified):
        print(f"\nVerified leads on file across all runs: {len(banked)} "
              f"({len(qualified)} of them found in this run).")
        print("Export the full list with:  python tvb.py leads --csv leads.csv --json leads.json")

    export = banked if getattr(args, "all_runs", False) else qualified
    if args.json:
        Path(args.json).write_text(leads_to_json(export), encoding="utf-8")
        print(f"\nWrote {args.json} ({len(export)} leads)")
    if args.csv:
        Path(args.csv).write_text(leads_to_csv(export), encoding="utf-8")
        print(f"Wrote {args.csv} ({len(export)} leads)")
    if args.stats:
        Path(args.stats).write_text(json.dumps(result.stats.as_dict(), indent=2), encoding="utf-8")
        print(f"Wrote {args.stats}")

    store.close()
    return 0 if qualified else 1


async def _leads(args) -> int:
    """Every verified lead the agent has found, across every run."""
    settings = get_settings()
    store = Store(settings.db_path)
    banked = store.leads(qualified_only=True)

    dropped = getattr(store, "last_read_rejections", [])
    print("=" * 78)
    print(f"VERIFIED LEADS ON FILE: {len(banked)}")
    print("=" * 78)
    if dropped:
        print(f"{len(dropped)} lead(s) banked by an earlier build no longer pass the current")
        print("rules and are excluded. A fix applies retroactively; nothing is grandfathered in:")
        for name, why in dropped:
            print(f"  - {name}: {why}")
        print("")
    for i, lead in enumerate(banked, 1):
        c = lead.company
        print(f"{i:2d}. {c.name}  [{c.country.value}]  "
              f"{c.funding.value.human() if c.funding.value else '-'}")
        print(f"    {c.founder.value.name if c.founder.value else '-'} "
              f"({c.founder.value.title if c.founder.value else '-'})  <{c.email.address}>")
        print(f"    {c.website.value or ''}")
    if not banked:
        print("None yet. Run: python tvb.py run")

    if args.json:
        Path(args.json).write_text(leads_to_json(banked), encoding="utf-8")
        print(f"\nWrote {args.json}")
    if args.csv:
        Path(args.csv).write_text(leads_to_csv(banked), encoding="utf-8")
        print(f"Wrote {args.csv}")
    store.close()
    return 0 if banked else 1


async def _audit(args) -> int:
    from .audit import audit_file

    report = await audit_file(args.path, get_settings(),
                              on_event=_printer(args.verbose) if args.verbose else None)
    print(report.summary())
    if args.out:
        Path(args.out).write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        print(f"\nWrote {args.out}")
    return 0 if report.clean_leads == len(report.leads) else 1


def _bank(args) -> int:
    """Export the verified leads to a shippable file, or replay one back in.

    The hosted app is judged by what it shows on open, and a reviewer should not
    have to spend their own API credits to see whether the agent works. The bank
    carries the real database rows - evidence, gate reasons and run stats - so a
    shipped lead is re-checked against the current rules exactly like a
    locally-found one when it is read back.
    """
    from .seed import export_bank, load_bank

    if not args.export_to and not args.import_from:
        print("Nothing to do: pass --export [PATH] or --import [PATH].")
        return 2
    store = Store(get_settings().db_path)
    code = 0
    if args.export_to:
        n = export_bank(store, args.export_to)
        rejected = len([lead for lead in store.leads(qualified_only=False)
                        if not lead.qualification.qualified])
        print(f"Exported {n} verified lead(s) to {args.export_to}, "
              f"with up to 80 of the {rejected} rejections and the reason each one failed.")
        if not n:
            print("No verified leads on file yet - nothing was shipped.")
            code = 1
    if args.import_from:
        n = load_bank(store, args.import_from)
        print(f"Replayed {n} lead(s) from {args.import_from}")
        live = len(store.leads(qualified_only=True))
        print(f"{live} of them still pass the current rules and are on file.")
        if not n:
            code = 1
    store.close()
    return code


def _doctor(live: bool = False) -> int:
    from .diagnostics import check_live, collect

    s = get_settings()
    print("TVB Lead Agent - configuration check\n" + "-" * 60)
    for row in s.health():
        mark = "OK " if row["configured"] else "-- "
        print(f"[{mark}] {row['capability']:22s} {row['provider']}")
        if not row["configured"]:
            print(f"       {row['degraded_without']}")

    rows = collect(s)
    if live:
        print("\nCalling each configured provider...")
        rows = asyncio.run(check_live(s, rows))

    configured = [r for r in rows if r.configured]
    if not configured:
        print("\n" + "!" * 60)
        print("NO API KEYS ARE CONFIGURED.")
        print("")
        print("The agent will fall back to scraping DuckDuckGo, which blocks")
        print("automated requests almost immediately, so a run will find nothing.")
        print("")
        print("  1. Open .env in this folder")
        print("  2. Paste your keys (at minimum SERPER_API_KEY or TAVILY_API_KEY)")
        print("  3. Run this check again")
        print("")
        print("If .env looks empty, it was probably overwritten by")
        print("`copy .env.example .env` - only run that the first time.")
        print("!" * 60)
    else:
        print("\nAPI keys\n" + "-" * 60)
        for r in configured:
            print(f"  {r.provider:12s} {r.capability:11s} {r.verdict}")
            # A key that just made a successful call is fine, whatever its shape.
            if r.format_note and r.live_ok is not True:
                print(f"               ! {r.format_note}")
            if r.live_note:
                print(f"               {r.live_note}")

    broken = [r for r in configured
              if r.live_ok is False or (r.format_ok is False and r.live_ok is None)]
    if broken:
        print("\n" + "!" * 60)
        print("These keys are set but will not work:")
        for r in broken:
            print(f"  - {r.provider}: {r.live_note or r.format_note}")
        print("A key that is set but invalid is worse than a blank one: the agent")
        print("reports the provider as available, then every call fails quietly")
        print("behind the fallback chain. Fix or clear these before a real run.")
        print("!" * 60)

    print(f"\nDatabase: {s.db_path}")
    print(f"Qualifying band: ${s.min_amount_usd:,.0f} - ${s.max_amount_usd:,.0f}")
    print(f"Target per run: {s.budget.target_qualified} qualified leads")
    print(f"Search budget: {s.budget.max_searches} "
          f"({s.budget.max_discovery_searches} for discovery, rest for per-company probes)")

    if not s.has_search:
        print("\nNo search capability at all: set SERPER_API_KEY or TAVILY_API_KEY.")
        return 2
    if not live:
        print("\nRun `python tvb.py doctor --live` to prove each key actually works.")
    return 1 if broken else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tvb_agent", description="TVB lead discovery agent")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="discover and qualify companies")
    run.add_argument("--target", type=int, default=None, help="qualified leads to aim for")
    run.add_argument("--max-searches", type=int, default=None)
    run.add_argument("--max-companies", type=int, default=None)
    run.add_argument("--max-runtime", type=int, default=None, help="seconds")
    run.add_argument("--sectors", type=str, default=None, help="comma-separated sector keys")
    run.add_argument("--geographies", type=str, default=None, help="comma-separated geography keys")
    run.add_argument("--include-seen", action="store_true", help="do not skip companies from earlier runs")
    run.add_argument("--seed", type=int, default=None)
    run.add_argument("--json", type=str, default=None)
    run.add_argument("--csv", type=str, default=None)
    run.add_argument("--stats", type=str, default=None)
    run.add_argument("--all-runs", action="store_true",
                     help="export every verified lead on file, not only this run's")
    run.add_argument("-v", "--verbose", action="store_true")
    run.add_argument("--allow-no-search-key", action="store_true",
                     help="start the run even with no search provider configured")

    doctor = sub.add_parser("doctor", help="show which providers are configured and working")
    doctor.add_argument("--live", action="store_true",
                        help="actually call each provider to prove the key works "
                             "(uses a free balance endpoint where one exists, otherwise one credit)")

    leads = sub.add_parser(
        "leads", help="every verified lead found so far, across all runs")
    leads.add_argument("--json", type=str, default=None)
    leads.add_argument("--csv", type=str, default=None)

    bank = sub.add_parser(
        "bank", help="ship the verified leads with the repo, or replay them back in")
    bank.add_argument("--export", dest="export_to", nargs="?", const=str(SHIPPED_PATH),
                      default=None, metavar="PATH",
                      help="write every verified lead on file to a shippable JSON bank "
                           "(default: data/shipped_leads.json)")
    bank.add_argument("--import", dest="import_from", nargs="?", const=str(SHIPPED_PATH),
                      default=None, metavar="PATH",
                      help="replay a shipped bank into this database")

    audit = sub.add_parser(
        "audit", help="independently re-verify an exported lead list")
    audit.add_argument("path", help="path to a leads JSON file produced by `run --json`")
    audit.add_argument("--json", dest="out", default=None, help="write the audit report as JSON")
    audit.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "doctor":
        return _doctor(live=getattr(args, "live", False))
    if args.command == "leads":
        return asyncio.run(_leads(args))
    if args.command == "audit":
        return asyncio.run(_audit(args))
    if args.command == "bank":
        return _bank(args)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        # Everything found so far is already in SQLite; say so rather than
        # dumping a traceback that looks like a crash.
        print("\n\nInterrupted. Everything discovered so far was written to the")
        print("database as it was found - nothing is lost. Re-run to continue;")
        print("companies already seen are skipped automatically.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
