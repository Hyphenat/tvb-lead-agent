"""The agent loop.

Discovery -> research -> validation -> qualification, repeated until the target
number of qualified leads is reached or the budget runs out.  Every stage writes
straight to SQLite, so a run interrupted by a hosting timeout still leaves behind
everything it had established.

The loop is genuinely adaptive: each pass consults the frontier for the least
explored, most productive corners of the search space, and every discovered
article or portfolio page can inject new candidates mid-run.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field

import httpx

from .config import Budget, Settings, get_settings
from .discovery.candidate_extractor import (
    Candidate,
    candidates_from_article,
    candidates_from_link_page,
    candidates_from_search,
    is_probable_company_host,
    looks_like_link_hub,
)
from .discovery.frontier import Frontier
from .discovery.query_planner import Cell, QueryPlanner
from .models import (
    AUTHORITY_RANK,
    CompanyProfile,
    Evidence,
    Lead,
    SourceAuthority,
)
from .providers.base import SearchResult
from .providers.contacts import ContactEnrichmentService
from .providers.email_verify import EmailVerificationService
from .providers.fetcher import Fetcher, host_of
from .providers.llm import LLMService
from .providers.search import SearchService
from .qualify.engine import qualify
from .research.authority import classify_authority
from .research.crawler import crawl_company_site
from .research.extractor import GroundedExtractor, Source
from .storage import Store, normalise_domain
from .validation.email import validate_email, verify_enriched_address
from .validation.validators import (
    SourceText,
    domain_is_an_investor,
    looks_like_non_operating_company,
    mentions_company,
    validate_country,
    validate_founder,
    validate_funding,
    validate_technology,
    validate_us_presence,
)


@dataclass
class RunStats:
    queries_run: int = 0
    discovery_queries: int = 0
    probe_queries: int = 0
    candidates_found: int = 0
    candidates_skipped_known: int = 0
    companies_researched: int = 0
    pages_fetched: int = 0
    cache_hits: int = 0
    llm_calls: int = 0
    email_checks: int = 0
    claims_grounded: int = 0
    claims_dropped: int = 0
    qualified: int = 0
    rejected: int = 0
    search_failures: int = 0
    search_error: str = ""
    llm_rate_limit_hits: int = 0
    # Where candidates are actually lost. Without this a disappointing run is
    # just a small number, and there is no way to tell whether the problem is
    # discovery, the funding band, or unpublished founder emails.
    gate_failures: dict[str, int] = field(default_factory=dict)
    failed_only_on_email: int = 0
    emails_from_enrichment: int = 0
    emails_confirmed_on_cited_page: int = 0
    no_usable_sources: int = 0
    skipped_non_operating: int = 0
    # Candidates looked at, including the ones that never became research.
    candidates_examined: int = 0
    # Where the scarcest requirement - a verified founder email - actually broke.
    email_outcomes: dict = field(default_factory=dict)
    # Why single-gate near misses failed, so a run is diagnosable from its own
    # output instead of by guesswork.
    near_miss_reasons: dict = field(default_factory=dict)
    cells_explored: int = 0
    elapsed_seconds: float = 0.0
    stop_reason: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunResult:
    run_id: str
    leads: list[Lead] = field(default_factory=list)
    rejected: list[Lead] = field(default_factory=list)
    stats: RunStats = field(default_factory=RunStats)

    @property
    def qualified_leads(self) -> list[Lead]:
        return [lead_ for lead_ in self.leads if lead_.qualification.qualified]


class LeadAgent:
    def __init__(self, settings: Settings | None = None, store: Store | None = None,
                 on_event: Callable[[str, str, str], None] | None = None):
        self.settings = settings or get_settings()
        self.store = store or Store(self.settings.db_path)
        self._on_event = on_event
        self.run_id = ""

    # ------------------------------------------------------------------ #
    def log(self, message: str, level: str = "info", stage: str = "") -> None:
        if self.run_id:
            with contextlib.suppress(Exception):
                self.store.log(self.run_id, message, level, stage)
        if self._on_event:
            with contextlib.suppress(Exception):
                self._on_event(message, level, stage)

    # ------------------------------------------------------------------ #
    async def run(self, budget: Budget | None = None, *,
                  sectors: Iterable[str] | None = None,
                  geographies: Iterable[str] | None = None,
                  include_previously_seen: bool = False,
                  seed: int | None = None) -> RunResult:
        budget = budget or self.settings.budget
        self.run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        started = time.monotonic()
        stats = RunStats()
        result = RunResult(run_id=self.run_id, stats=stats)

        self.store.start_run(self.run_id, {
            "budget": asdict(budget), "sectors": list(sectors or []),
            "geographies": list(geographies or []), "include_previously_seen": include_previously_seen,
        })

        limits = httpx.Limits(max_connections=budget.concurrency * 3,
                              max_keepalive_connections=budget.concurrency)
        timeout = httpx.Timeout(self.settings.http_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True,
                                     headers={"User-Agent": self.settings.user_agent}) as client:
            search = SearchService.build(client, self.settings, on_event=lambda m, level="info": self.log(m, level, "search"))
            llm = LLMService.build(client, self.settings, on_event=lambda m, level="info": self.log(m, level, "llm"))
            verifier = EmailVerificationService.build(client, self.settings,
                                                      on_event=lambda m, level="info": self.log(m, level, "email"))
            fetcher = Fetcher(client, self.settings, self.store,
                              on_event=lambda m, level="info": self.log(m, level, "fetch"))
            extractor = GroundedExtractor(llm, on_event=lambda m, level="info": self.log(m, level, "extract"))
            contacts = ContactEnrichmentService.build(
                client, self.settings, on_event=lambda m, level="info": self.log(m, level, "contacts"))

            self.log(f"Run {self.run_id} starting. Search: {search.active_provider}; "
                     f"extraction: {llm.active_provider}; email: {self.settings.email_verifier_name()}; "
                     f"contacts: {contacts.provider_names}.", "info", "start")

            planner = QueryPlanner(cell_stats=self.store.cell_stats(),
                                   seen_queries=self.store.seen_queries(),
                                   seed=seed, sectors=sectors, geographies=geographies)
            frontier = Frontier(
                known_keys=set() if include_previously_seen else self.store.known_company_keys(),
                min_usd=self.settings.min_amount_usd, max_usd=self.settings.max_amount_usd)
            if frontier.known_keys:
                self.log(f"{len(frontier.known_keys)} companies already known from previous runs "
                         f"will be skipped so this run surfaces new ones.", "info", "start")

            def out_of_time() -> bool:
                return (time.monotonic() - started) > budget.max_runtime_seconds

            wave = 0
            while len(result.qualified_leads) < budget.target_qualified:
                if out_of_time():
                    stats.stop_reason = "time budget reached"
                    break
                if stats.companies_researched >= budget.max_companies_researched:
                    stats.stop_reason = "research budget reached"
                    break

                # Discovery running out is not a reason to stop while candidates
                # are still queued: run 10 finished at 85 companies with 65
                # research slots unused and a full frontier, because the *other*
                # half of the budget was spent. Stop finding, keep qualifying.
                discovery_spent = stats.discovery_queries >= budget.max_discovery_searches
                if discovery_spent and frontier.pending == 0:
                    stats.stop_reason = "discovery search allowance reached"
                    break

                wave += 1
                found = 0
                if not discovery_spent:
                    found = await self._discover_wave(
                        wave, planner, frontier, search, fetcher, stats, budget)
                if found == 0 and frontier.pending == 0:
                    # Distinguish "we looked and found nothing" from "we could not
                    # look at all". Reporting a dead search provider as exhausted
                    # discovery sends you tuning the search strategies when the
                    # actual problem is a missing or rejected API key.
                    if search.all_searches_failed:
                        stats.stop_reason = (
                            f"every search failed - the search provider "
                            f"('{search.active_provider}') is not returning results: "
                            f"{search.last_error}")
                        self.log("STOPPING: every search request failed. This is a provider or "
                                 "API-key problem, not a discovery problem. Run "
                                 "`python tvb.py doctor --live` to check your keys.", "warn", "discover")
                    else:
                        stats.stop_reason = ("discovery exhausted: no new candidates from the "
                                             "strategies tried")
                        self.log("No new candidates from this wave and nothing pending - stopping.",
                                 "warn", "discover")
                    break

                await self._research_wave(
                    frontier, search, fetcher, extractor, verifier, result, stats, budget,
                    out_of_time, contacts)

            if not stats.stop_reason:
                stats.stop_reason = f"target of {budget.target_qualified} qualified leads reached"

            stats.queries_run = search.calls
            stats.search_failures = search.failures
            stats.search_error = search.last_error
            stats.probe_queries = max(0, search.calls - stats.discovery_queries)
            stats.pages_fetched = fetcher.pages_fetched
            stats.cache_hits = fetcher.cache_hits
            stats.llm_calls = llm.calls
            stats.llm_rate_limit_hits = llm.rate_limit_hits
            stats.email_checks = verifier.checks
            stats.claims_grounded = extractor.grounded_ok
            stats.claims_dropped = extractor.grounded_dropped

        stats.elapsed_seconds = round(time.monotonic() - started, 1)
        stats.qualified = len(result.qualified_leads)
        stats.rejected = len(result.rejected)
        self.store.finish_run(self.run_id, "completed", stats.as_dict(), stats.stop_reason)
        self.log(f"Run finished: {stats.qualified} qualified, {stats.rejected} rejected, "
                 f"{stats.companies_researched} researched in {stats.elapsed_seconds}s "
                 f"({stats.stop_reason}).", "info", "done")
        if stats.gate_failures:
            breakdown = ", ".join(f"{g}={n}" for g, n in
                                  sorted(stats.gate_failures.items(), key=lambda kv: -kv[1]))
            self.log(f"Where candidates were lost: {breakdown}. "
                     f"{stats.failed_only_on_email} cleared every requirement except a verified "
                     f"email. {stats.no_usable_sources} had no readable source at all, and "
                     f"{stats.skipped_non_operating} were funds, publications or agencies "
                     f"skipped before any budget was spent on them.",
                     "info", "done")
        if frontier.ruled_out:
            top = sorted(frontier.ruled_out_reasons.items(), key=lambda kv: -kv[1])[:3]
            self.log(f"{frontier.ruled_out} candidates were ruled out by their own headline "
                     f"before any budget was spent: "
                     + "; ".join(f"{n}x {why}" for why, n in top), "info", "done")
        if stats.email_outcomes:
            order = ["verified", "source_verified", "found_unverified", "role_only",
                     "not_found", "invalid"]
            parts = [f"{stats.email_outcomes[k]} {k.replace('_', ' ')}"
                     for k in order if stats.email_outcomes.get(k)]
            if stats.emails_confirmed_on_cited_page:
                parts.append(f"{stats.emails_confirmed_on_cited_page} were confirmed by reading "
                             f"the page a provider cited")
            if stats.emails_from_enrichment:
                parts.append(f"{stats.emails_from_enrichment} of the verified addresses came "
                             f"from a contact database rather than a page we read")
            self.log("Founder email, the scarcest requirement: " + ", ".join(parts)
                     + f". {stats.candidates_examined} candidates were examined to produce "
                       f"{stats.companies_researched} actual pieces of research.",
                     "info", "done")
        if stats.near_miss_reasons:
            top = sorted(stats.near_miss_reasons.items(), key=lambda kv: -kv[1])[:6]
            self.log("Closest misses, and the single requirement each one failed: "
                     + "; ".join(f"{n}x {why}" for why, n in top), "info", "done")
        return result

    # --------------------------------------------------------- discovery --
    async def _discover_wave(self, wave: int, planner: QueryPlanner, frontier: Frontier,
                             search: SearchService, fetcher: Fetcher,
                             stats: RunStats, budget: Budget) -> int:
        # Discovery and research share one search budget. Run 9 spent the whole
        # discovery allowance building a 357-candidate frontier while only 64
        # candidates were ever researched, then stopped for want of searches. A
        # deep frontier means the next credit is worth more to research.
        if frontier.pending >= 40:
            self.log(f"Skipping discovery this wave: {frontier.pending} candidates are already "
                     f"waiting, and the search budget is worth more to research.", "debug", "discover")
            return 0
        cells = planner.propose_cells(4)
        stats.cells_explored += len(cells)
        new_total = 0

        for cell in cells:
            if stats.discovery_queries >= budget.max_discovery_searches:
                break
            queries = planner.render_queries(cell, per_cell=2)
            for q in queries:
                if stats.discovery_queries >= budget.max_discovery_searches:
                    break
                results = await search.search(q, limit=10)
                stats.discovery_queries += 1
                planner.seen_queries.add(q)
                self.store.record_query(self.run_id, q, search.active_provider, cell.key, len(results))
                self.log(f"[wave {wave}] {cell.label()} :: {q} -> {len(results)} results", "info", "discover")

                cands = candidates_from_search(results, cell.key)
                added = frontier.add_many(cands)
                new_total += added

                added += await self._expand_link_hubs(results, fetcher, frontier, cell, budget, stats)
                self.store.bump_cell(cell.key, cell.parts(), self.run_id, candidates=added)

        stats.candidates_found += new_total
        stats.candidates_skipped_known = frontier.skipped_known
        self.log(f"[wave {wave}] {new_total} new candidates ({frontier.pending} pending, "
                 f"{frontier.skipped_known} already known from earlier runs).", "info", "discover")
        return new_total

    async def _expand_link_hubs(self, results: list[SearchResult], fetcher: Fetcher,
                                frontier: Frontier, cell: Cell, budget: Budget, stats: RunStats) -> int:
        """Portfolio pages, cohort pages and listicles yield many companies at once."""
        added = 0
        # A VC's own site is a perfectly good hub, so do not exclude pages that
        # merely look like a company domain - judge by what the page contains.
        hubs = [r for r in results if looks_like_link_hub(r.url, r.title, r.snippet)][:2]
        for r in hubs:
            if fetcher.pages_fetched >= budget.max_pages_fetched:
                break
            page, html = await fetcher.fetch_safe(r.url, want_html=True)
            if not page.ok:
                continue
            from .providers.fetcher import extract_links

            link_cands = candidates_from_link_page(extract_links(html, page.url), page.url, cell.key)
            article_cands = candidates_from_article(page.text, page.url, cell.key)
            n = frontier.add_many(link_cands) + frontier.add_many(article_cands)
            if n:
                self.log(f"Expanded {len(link_cands) + len(article_cands)} companies from {host_of(page.url)} "
                         f"({n} new).", "info", "discover")
            added += n
        return added

    # ---------------------------------------------------------- research --
    async def _research_wave(self, frontier: Frontier, search: SearchService, fetcher: Fetcher,
                             extractor: GroundedExtractor, verifier: EmailVerificationService,
                             result: RunResult, stats: RunStats, budget: Budget,
                             out_of_time: Callable[[], bool],
                             contacts: ContactEnrichmentService | None = None) -> None:
        while frontier.pending and len(result.qualified_leads) < budget.target_qualified:
            if out_of_time() or stats.companies_researched >= budget.max_companies_researched:
                return
            # Candidates that yield nothing cost a little work each, so the
            # examine count is bounded too - otherwise a bad wave of link-hub
            # labels could spin for the whole runtime.
            if stats.candidates_examined >= budget.max_companies_researched * 3:
                stats.stop_reason = "candidate budget reached"
                return
            if search.budget_exhausted and frontier.pending == 0:
                return
            batch = frontier.pop_batch(budget.concurrency)
            if not batch:
                return
            tasks = [self._research_one(c, search, fetcher, extractor, verifier,
                                        budget, stats, contacts) for c in batch]
            for coro in asyncio.as_completed(tasks):
                try:
                    lead = await coro
                except (NameError, AttributeError, TypeError, ImportError, SyntaxError):
                    # These are bugs in this codebase, not bad data. Swallowing
                    # them makes a broken build look like a disappointing run:
                    # two NameErrors once ate 60% of a run's companies while the
                    # log said only "Research error".
                    raise
                except Exception as e:
                    self.log(f"Research error on one company: {type(e).__name__}: {e}",
                             "warn", "research")
                    continue
                if lead is None:
                    continue
                if lead.qualification.qualified:
                    result.leads.append(lead)
                    c = lead.company
                    self.log(f"QUALIFIED: {c.name} ({c.country.value}) - "
                             f"{c.funding.value.human() if c.funding.value else '?'} - {c.email.address} "
                             f"[{len(result.qualified_leads)}/{budget.target_qualified}]", "success", "qualify")
                else:
                    result.rejected.append(lead)
                    failed = [g.gate.value for g in lead.qualification.gates if not g.passed]
                    for gate in failed:
                        stats.gate_failures[gate] = stats.gate_failures.get(gate, 0) + 1
                    if failed == ["email_verified"]:
                        stats.failed_only_on_email += 1
                    if len(failed) == 1:
                        why = lead.qualification.first_failure_reason() or failed[0]
                        key = f"{failed[0]}: {why[:90]}"
                        stats.near_miss_reasons[key] = stats.near_miss_reasons.get(key, 0) + 1
                    self.log(f"rejected: {lead.company.name} - {lead.qualification.first_failure_reason()}",
                             "debug", "qualify")

    async def _research_one(self, candidate: Candidate, search: SearchService, fetcher: Fetcher,
                            extractor: GroundedExtractor, verifier: EmailVerificationService,
                            budget: Budget, stats: RunStats,
                            contacts: ContactEnrichmentService | None = None) -> Lead | None:
        """Research one company in two passes.

        Pass 1 reads the company's own website, which usually settles what the
        product is, where it is based and who runs it.  Pass 2 spends search
        credits **only on the gates still unresolved** - most often funding,
        which companies rarely state on their own site.  Probing blindly for
        every company would exhaust a free search tier long before the run
        reached its target.
        """
        stats.candidates_examined += 1
        name = candidate.name
        domain = normalise_domain(candidate.domain)

        # Does the result that found this company say anything usable about it?
        # Computed before the site lookup because it decides whether the lookup
        # is worth a search credit at all.
        excerpt = self._discovery_excerpt(candidate, domain)

        if not domain:
            # Try the obvious addresses first. A search costs a credit from a
            # budget the research probes need; fetching acme.io costs a page
            # from a budget with room to spare, and it is accepted only if the
            # page that comes back actually talks about this company.
            domain = await self._probe_obvious_domains(name, candidate, fetcher, budget)
        if not domain and self._worth_a_website_search(candidate, excerpt):
            # A company with no website and nothing readable about it is dead on
            # arrival, so this lookup is what decides whether it becomes research
            # at all. Run 14 gated it on the excerpt passing its own attribution
            # check, which almost nothing does: across 263 candidates the search
            # ran twice and the probe fired three times, and 180 candidates went
            # straight to "no readable source". The gate is now about whether the
            # candidate is a plausible company, not about whether we already have
            # quotable evidence for it.
            domain = await self._find_official_domain(name, search, candidate)
        if domain and excerpt is not None and host_of(excerpt.url) == domain:
            excerpt = None

        profile = CompanyProfile.new(name, domain)
        profile.discovered_via = list(candidate.discovered_via)
        profile.discovery_cell = candidate.cell_key
        profile.first_seen_run = profile.last_seen_run = self.run_id

        sources: list[Source] = []
        if domain and fetcher.pages_fetched < budget.max_pages_fetched:
            site = await crawl_company_site(fetcher, domain)
            for page in site.all_pages():
                sources.append(Source(url=page.url, text=page.text, title=page.title,
                                      authority=classify_authority(page.url, domain)))
            profile.pages_fetched = site.urls()

        # The headline that discovered this company is evidence in its own right:
        # "Banyu raises US$1.25 million seed to scale Indonesia's seaweed
        # industry" carries the figure, the country and often the founder. Run 7
        # threw every one of these away and then rejected the company for having
        # no verifiable funding.
        if excerpt is not None:
            sources.append(excerpt)

        if not sources:
            # Nothing to read and nothing to go on. This candidate never became
            # a piece of research, so it does not spend a research slot either -
            # run 8 gave 89 of its 120 slots to candidates in exactly this state.
            stats.no_usable_sources += 1
            return None

        stats.companies_researched += 1

        # Bail out before spending search credits and email verifications on an
        # organisation that is not an operating company at all.
        own_texts = [SourceText(url=s.url, text=s.text, authority=s.authority, title=s.title)
                     for s in sources]
        reason = domain_is_an_investor(domain) or looks_like_non_operating_company(own_texts)
        if reason:
            stats.skipped_non_operating += 1
            self.log(f"skipped {name}: {reason}", "debug", "research")
            return None

        extraction = await extractor.extract(name, sources)
        self._apply(profile, extraction, sources, name)

        # --- pass 2: fill only the gaps, and only if it is worth a search -----
        gaps = self._open_gaps(profile, extraction)
        if gaps and not search.budget_exhausted and fetcher.pages_fetched < budget.max_pages_fetched:
            extra = await self._gather_external_sources(name, domain, search, fetcher, budget, gaps)
            if extra:
                sources.extend(extra)
                extraction = await extractor.extract(name, sources)
                self._apply(profile, extraction, sources, name)

        if extraction is None:
            stats.no_usable_sources += 1
            return None

        profile.email = await validate_email(extraction, profile.founder, domain, verifier)

        # A founder with no usable address is the commonest near-miss, and it is
        # worth one more look in the places companies actually publish them.
        if (profile.founder.known and not profile.email.is_verified
                and not search.budget_exhausted
                and fetcher.pages_fetched < budget.max_pages_fetched):
            more = await self._hunt_founder_email(
                name, domain, profile.founder.value.name, search, fetcher, budget)
            if more:
                sources.extend(more)
                extraction = await extractor.extract(name, sources)
                self._apply(profile, extraction, sources, name)
                profile.email = await validate_email(extraction, profile.founder, domain, verifier)

        # Last resort, and only for a founder we identified ourselves: ask a
        # contact database. Most companies never publish their founder's
        # address, so without this the agent can only ever find the minority
        # that do. The answer is held to the same technical checks, and its
        # provenance is recorded literally so a database-supplied address is
        # never mistaken for one published on a page.
        if (not profile.email.is_verified and profile.founder.known and domain
                and contacts is not None and contacts.configured):
            finding = await contacts.find(profile.founder.value.name, domain, name)
            if finding:
                # Hunter says *where* it saw the address. Go and read that page:
                # if the address is really on it, this stops being something we
                # were told and becomes something we checked, with the page's own
                # standing behind it.
                confirmed = await self._confirm_cited_address(
                    finding, fetcher, budget, domain)
                if confirmed is None and getattr(finding, "source_urls", None):
                    # Hunter named a page. We read it and the address was not on
                    # it - so the citation does not hold up, and without it the
                    # finding is a pattern guess. Accepting it anyway made the
                    # whole confirmation step decorative.
                    self.log(f"Discarded {finding.address}: the page {finding.provider} cited "
                             f"does not contain it.", "debug", "contacts")
                    finding = None
                if confirmed is not None:
                    sources.append(confirmed)
                    extraction = await extractor.extract(name, sources)
                    self._apply(profile, extraction, sources, name)
                    reread = await validate_email(extraction, profile.founder, domain, verifier)
                    if _stronger(reread, profile.email):
                        profile.email = reread
                        if reread.is_verified:
                            stats.emails_confirmed_on_cited_page += 1
                enriched = (await verify_enriched_address(
                    finding, profile.founder, domain, verifier)) if finding else None
                if enriched is not None and _stronger(enriched, profile.email):
                    enriched.role_fallback = enriched.role_fallback or profile.email.role_fallback
                    profile.email = enriched
                    if enriched.is_verified:
                        stats.emails_from_enrichment += 1

        status = profile.email.status.value
        stats.email_outcomes[status] = stats.email_outcomes.get(status, 0) + 1

        qualification = qualify(profile, self.settings)
        self.store.upsert_company(profile, self.run_id)
        self.store.save_qualification(profile.id, self.run_id, qualification)
        if candidate.cell_key and qualification.qualified:
            self.store.bump_cell(candidate.cell_key, {}, self.run_id, qualified=1)
        return Lead(company=profile, qualification=qualification, run_id=self.run_id)

    @staticmethod
    def _worth_a_website_search(candidate: Candidate, excerpt) -> bool:
        """Is this candidate worth one search credit to locate a website?

        Yes for anything that looks like a real company name found through a
        real channel. No for the bare labels scraped off portfolio grids, which
        in an earlier run spent the research budget and returned nothing.
        """
        from .discovery.candidate_extractor import is_generic_name

        name = (candidate.name or "").strip()
        if len(name) < 3 or is_generic_name(name):
            return False
        if excerpt is not None:
            return True
        # A headline or an article named this company in prose; that is a real
        # channel even when the excerpt itself failed the evidence bar.
        if candidate.source_kind in ("headline", "article", "company_site"):
            return True
        # A link-hub label with nothing else behind it has to earn it by being
        # corroborated more than once.
        return len(candidate.discovered_via) > 1

    # Ordered by how often a startup's own site actually sits there.
    _COMMON_TLDS = ("com", "io", "co", "ai", "app", "tech", "org", "net")

    async def _probe_obvious_domains(self, name: str, candidate: Candidate,
                                     fetcher: Fetcher, budget: Budget) -> str | None:
        """Resolve a company's own site by trying its name as a domain.

        This is not a guess that gets recorded: a candidate address is accepted
        only when the page fetched from it names the company and does not read
        like a parked or for-sale placeholder. Anything short of that is
        discarded and the search-based lookup runs instead.
        """
        compact = "".join(ch for ch in (name or "").lower() if ch.isalnum())
        if len(compact) < 5 or len(compact) > 24:
            return None
        # Probing costs pages, not credits, and finding the website is what makes
        # every later page worth fetching at all - so it gets nearly the whole
        # budget. At 60% it switched itself off a third of the way into a run and
        # every company after that went unresolved.
        if fetcher.pages_fetched >= int(budget.max_pages_fetched * 0.95):
            return None

        tlds = list(self._COMMON_TLDS)
        # The market the discovery excerpt pointed at, so a Kenyan company gets
        # .co.ke tried alongside .com.
        cc = _cctld_for(candidate.hint_snippet)
        if cc:
            tlds.insert(1, cc)

        for tld in tlds[:3]:
            host = f"{compact}.{tld}"
            if fetcher.host_is_abandoned(f"https://{host}"):
                continue
            page = await fetcher.fetch_safe(f"https://{host}")
            if isinstance(page, Exception) or not getattr(page, "ok", False):
                continue
            text = (page.text or "")[:8_000]
            if _looks_parked(text) or not mentions_company(text, name):
                continue
            self.log(f"Resolved {name} to {host} by probing, no search spent.", "debug", "research")
            return normalise_domain(host)
        return None

    async def _confirm_cited_address(self, finding, fetcher: Fetcher, budget: Budget,
                                     company_domain: str | None = None) -> Source | None:
        """Read the page a provider cited, and check the address is really on it.

        This is the whole difference between being told an address and knowing
        one. If the page is reachable and the address is in its text, the finding
        stops being a database claim and becomes an ordinary evidenced source
        with that page's own standing - quotable, checkable, and auditable by
        anyone who follows the link.
        """
        if not getattr(finding, "source_urls", None):
            return None
        if fetcher.pages_fetched >= budget.max_pages_fetched:
            return None
        for url in finding.source_urls[:2]:
            if not url or fetcher.host_is_abandoned(url):
                continue
            page = await fetcher.fetch_safe(url)
            if isinstance(page, Exception) or not getattr(page, "ok", False):
                continue
            low = (page.text or "").lower()
            addr = finding.address.lower()
            # Whole-address match. As a bare substring, a page containing
            # mary-jane@acme.com "confirmed" jane@acme.com.
            if not re.search(rf"(?<![A-Za-z0-9._%+-]){re.escape(addr)}(?![A-Za-z0-9.-])", low):
                continue      # the citation does not hold up
            self.log(f"Confirmed {finding.address} on the page {finding.provider} cited "
                     f"({host_of(url)}).", "debug", "contacts")
            return Source(url=page.url, text=page.text, title=page.title,
                          authority=classify_authority(page.url, company_domain),
                          note=f"page cited by {finding.provider}; read and confirmed here")
        return None

    @staticmethod
    def _discovery_excerpt(candidate: Candidate, domain: str | None) -> Source | None:
        """The search result that found this company, kept as quotable evidence.

        Only from a host with standing (tier-1 press or an aggregator), only when
        the excerpt actually names the company, and always labelled as an excerpt
        so the evidence trail says where the words came from.
        """
        snippet = (candidate.hint_snippet or "").strip()
        if not snippet:
            return None
        url = next((u for u in candidate.discovered_via if u), "")
        if not url or host_of(url) == domain:
            return None
        authority = classify_authority(url, domain)
        if AUTHORITY_RANK[authority] < AUTHORITY_RANK[SourceAuthority.AGGREGATOR]:
            return None
        if not mentions_company(snippet, candidate.name):
            return None
        return Source(url=url, text=snippet, title="", authority=authority,
                      note="search-engine excerpt from the result that surfaced this company")

    @staticmethod
    def _open_gaps(profile: CompanyProfile, extraction=None) -> list[str]:
        """Which gates still lack evidence, in the order worth spending on.

        Funding first: it is the fact companies publish least often on their own
        site and the one most likely to decide the verdict.
        """
        gaps: list[str] = []
        if not profile.funding.known:
            gaps.append("funding")
        if not profile.founder.known:
            gaps.append("founder")
        if extraction is not None and not extraction.by_field("email"):
            gaps.append("email")
        if not profile.country.known:
            gaps.append("location")
        if profile.country.known and not profile.us_presence.known:
            gaps.append("us_presence")
        return gaps

    async def _hunt_founder_email(self, company: str, domain: str | None, founder: str,
                                  search: SearchService, fetcher: Fetcher,
                                  budget: Budget) -> list[Source]:
        """One focused attempt to find where a named founder's address is published.

        Searches for the person by name alongside the company, and reads the
        conventional contact pages that are often unlinked from the navigation
        (imprint pages in particular are legally required in several European
        markets and routinely carry a named contact).
        """
        urls: list[str] = []
        queries = [f'"{founder}" "{company}" email contact',
                   # Wire releases carry "Media contact: Name, name@company.com",
                   # and those hosts already have first-tier standing.
                   f'"{company}" "media contact" OR "press contact" email {founder.split()[-1]}']
        if domain:
            # A leading "@" makes Serper answer HTTP 400 - run 9 burned sixteen
            # searches on queries that could never succeed. The site: operator
            # does the same job and is accepted.
            queries.insert(0, f'site:{domain} "{founder}" email')
            queries.append(f'site:{domain} contact imprint email')
        for q in queries[:2]:
            if search.budget_exhausted:
                break
            for r in await search.search(q, limit=4):
                if r.url not in urls:
                    urls.append(r.url)

        if domain:
            for path in ("/imprint", "/impressum", "/legal-notice", "/mentions-legales",
                         "/contact-us", "/about/contact", "/company/contact", "/press",
                         "/team", "/our-team", "/leadership", "/about-us", "/contacts",
                         "/kontakt", "/contacto", "/contatti", "/aviso-legal", "/colofon"):
                urls.append(f"https://{domain}{path}")

        urls = urls[:14]
        pages = await asyncio.gather(*(fetcher.fetch_safe(u) for u in urls), return_exceptions=True)
        out: list[Source] = []
        surname = (founder.split() or [founder])[-1].lower()
        for p in pages:
            if isinstance(p, Exception) or not getattr(p, "ok", False):
                continue
            low = p.text.lower()[:30_000]
            if surname not in low and "@" not in low:
                continue
            out.append(Source(url=p.url, text=p.text, title=p.title,
                              authority=classify_authority(p.url, domain)))
        return out

    def _apply(self, profile: CompanyProfile, extraction, sources: list[Source], name: str) -> None:
        """Run every validator over the evidence gathered so far."""
        source_texts = [SourceText(url=s.url, text=s.text, authority=s.authority,
                                   title=s.title, note=s.note)
                        for s in sources]
        if profile.domain:
            profile.website = self._website_field(profile.domain)

        d = extraction.first("description")
        if d:
            profile.description = _evidenced(str(d.value), d.evidence)
        sec = extraction.first("sector")
        if sec:
            profile.sector = _evidenced(str(sec.value), sec.evidence)
        city = extraction.first("hq_city")
        if city:
            profile.hq_city = _evidenced(str(city.value), city.evidence)

        profile.funding = validate_funding(extraction, self.settings, sources=source_texts,
                                           company_name=name)
        profile.tech_platform = validate_technology(extraction, profile.pages_fetched, source_texts)
        profile.country = validate_country(extraction, profile.domain, source_texts, company_name=name)
        profile.us_presence = validate_us_presence(extraction, profile.country,
                                                   profile.pages_fetched, self.settings)
        profile.founder = validate_founder(extraction, company_name=name)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _website_field(domain: str):
        from .models import Evidenced

        ev = Evidence(url=f"https://{domain}", quote=f"Company website resolved at {domain}.",
                      authority=SourceAuthority.COMPANY_OWNED)
        return Evidenced[str].of(f"https://{domain}", [ev])

    # Prefixes startups habitually bolt on when the bare name was taken.
    _DOMAIN_PREFIXES = ("get", "try", "use", "join", "go", "my", "the", "app",
                        "we", "hey", "with", "meet", "team")

    @classmethod
    def _host_matches_name(cls, host: str, name: str) -> bool:
        labels = [lbl for lbl in (host or "").lower().split(".") if lbl]
        root = labels[0] if labels else ""
        if root in ("www",) and len(labels) > 1:
            root = labels[1]
        root = re.sub(r"[^a-z0-9]", "", root)
        compact = "".join(ch for ch in (name or "").lower() if ch.isalnum())
        if not root or not compact:
            return False
        if root in compact or compact in root:
            return True
        # "Nova Pay" at usenova.com: the same distinctive word behind a prefix.
        for prefix in cls._DOMAIN_PREFIXES:
            if root.startswith(prefix):
                stem = root[len(prefix):]
                if len(stem) >= 4 and (stem in compact or compact in stem):
                    return True
        # The longest distinctive word carrying the name, e.g. "Zeta Care
        # Systems" at zetahq.com.
        words = [w.lower() for w in re.split(r"[^A-Za-z0-9]+", name or "") if len(w) >= 5]
        if words and max(words, key=len) in root:
            return True
        # Initialism: "Zeta Care Systems" -> zcs
        initials = "".join(w[0] for w in re.split(r"[^A-Za-z0-9]+", name) if w)
        return len(initials) >= 3 and root == initials.lower()

    async def _find_official_domain(self, name: str, search: SearchService,
                                    candidate: Candidate) -> str | None:
        """Locate the company's own site when discovery only gave us a name.

        Deliberately does *not* fall back to "the first company-looking result".
        The page that mentioned a company is not that company - a VC portfolio
        page would otherwise be adopted as the startup's own website, and every
        fact read from it attributed to the wrong business.
        """
        results = await search.search(f'"{name}" official website', limit=6)
        for r in results:
            if is_probable_company_host(r.url) and self._host_matches_name(host_of(r.url), name):
                return normalise_domain(host_of(r.url))
        self.log(f"No official website confidently identified for {name!r}; "
                 f"continuing with external sources only.", "debug", "research")
        return None

    async def _gather_external_sources(self, name: str, domain: str | None, search: SearchService,
                                       fetcher: Fetcher, budget: Budget,
                                       gaps: list[str]) -> list[Source]:
        """Targeted probes for the specific facts still missing.

        Returns fetched pages plus, where a page could not be retrieved, the
        search engine's own excerpt of it. Press sites refuse automated requests
        constantly, and without the excerpt a real company gets rejected for
        "no verifiable funding" while the figure sits in the result just read.
        """
        probes = QueryPlanner().targeted_queries(name, domain)
        urls: list[str] = []
        serp_results: list[SearchResult] = []
        # Gap probes and the founder-email hunt draw on the same credits, and the
        # email is the gate that actually decides the verdict: run 12 exhausted
        # the budget on gap probes and finished with the research budget half
        # unused. Once most of the allowance is gone, only the email gets credits.
        budget_is_tight = search.calls >= search.max_calls * 0.6
        allowed_gaps = 1 if budget_is_tight else 2
        for key in gaps[:allowed_gaps]:
            for q in probes.get(key, [])[:1]:
                for r in await search.search(q, limit=5):
                    serp_results.append(r)
                    if r.url not in urls and host_of(r.url) != domain:
                        urls.append(r.url)

        first = first_token_of(name)

        # Excerpts are kept only from sources that would have counted had the
        # page been reachable, and each one is labelled as an excerpt.
        snippet_sources: list[Source] = []
        for r in serp_results:
            if not r.snippet or host_of(r.url) == domain:
                continue
            authority = classify_authority(r.url, domain)
            if AUTHORITY_RANK[authority] < AUTHORITY_RANK[SourceAuthority.AGGREGATOR]:
                continue
            text = f"{r.title}. {r.snippet}".strip()
            if first and first not in text.lower():
                continue
            snippet_sources.append(Source(
                url=r.url, text=text, title=r.title, authority=authority,
                note="search-engine excerpt; the full page could not be retrieved"))

        urls = urls[:6]
        if not urls or fetcher.pages_fetched >= budget.max_pages_fetched:
            return snippet_sources[:4]

        pages = await asyncio.gather(*(fetcher.fetch_safe(u) for u in urls), return_exceptions=True)
        out: list[Source] = []
        for page in pages:
            if isinstance(page, Exception) or not getattr(page, "ok", False):
                continue
            if first and first not in page.text.lower()[:20_000]:
                continue
            out.append(Source(url=page.url, text=page.text, title=page.title,
                              authority=classify_authority(page.url, domain)))

        fetched_hosts = {host_of(src.url) for src in out}
        out.extend(src for src in snippet_sources if host_of(src.url) not in fetched_hosts)
        return out


def _stronger(candidate, current) -> bool:
    """Is this record a better answer than the one already on the profile?"""
    from .models import EMAIL_STATUS_RANK

    return EMAIL_STATUS_RANK[candidate.status] > EMAIL_STATUS_RANK[current.status]


def first_token_of(name: str) -> str:
    parts = [t for t in re.split(r"[^A-Za-z0-9]+", name or "") if t]
    return parts[0].lower() if parts else ""


def _evidenced(value, evidence):
    from .models import Evidenced

    return Evidenced[str].of(value, [evidence])


def _looks_parked(text: str) -> bool:
    """Is this a placeholder rather than a company's site?"""
    low = (text or "").lower()[:4_000]
    tells = ("this domain is for sale", "buy this domain", "domain for sale",
             "parked free", "courtesy of godaddy", "coming soon", "under construction",
             "website is currently unavailable", "default web page", "namecheap",
             "sedo", "index of /")
    if any(t in low for t in tells):
        return True
    # A page with almost no words on it is a placeholder. The threshold is low
    # on purpose: plenty of real startup home pages are a headline, a sentence
    # and two buttons, and treating those as parked loses the company.
    return len(re.sub(r"\s+", " ", low).strip()) < 120


# Where a market's companies actually register, used only to widen the probe.
_CCTLDS = {
    "kenya": "co.ke", "kenyan": "co.ke", "nairobi": "co.ke",
    "nigeria": "ng", "nigerian": "ng", "lagos": "ng",
    "south africa": "co.za", "south african": "co.za",
    "egypt": "com.eg", "egyptian": "com.eg",
    "india": "in", "indian": "in", "bengaluru": "in", "mumbai": "in",
    "pakistan": "pk", "pakistani": "pk", "bangladesh": "com.bd", "bangladeshi": "com.bd",
    "indonesia": "co.id", "indonesian": "co.id", "jakarta": "co.id",
    "vietnam": "vn", "vietnamese": "vn", "malaysia": "com.my", "malaysian": "com.my",
    "thailand": "co.th", "thai": "co.th", "philippines": "ph", "filipino": "ph",
    "singapore": "sg", "singaporean": "sg", "japan": "jp", "japanese": "jp",
    "south korea": "kr", "korean": "kr", "australia": "com.au", "australian": "com.au",
    "new zealand": "co.nz", "brazil": "com.br", "brazilian": "com.br",
    "mexico": "mx", "mexican": "mx", "colombia": "co", "chile": "cl", "argentina": "com.ar",
    "germany": "de", "german": "de", "berlin": "de", "munich": "de",
    "france": "fr", "french": "fr", "paris": "fr",
    "netherlands": "nl", "dutch": "nl", "amsterdam": "nl",
    "spain": "es", "spanish": "es", "italy": "it", "italian": "it",
    "poland": "pl", "polish": "pl", "portugal": "pt", "portuguese": "pt",
    "sweden": "se", "swedish": "se", "denmark": "dk", "danish": "dk",
    "norway": "no", "norwegian": "no", "finland": "fi", "finnish": "fi",
    "switzerland": "ch", "swiss": "ch", "austria": "at", "austrian": "at",
    "ireland": "ie", "irish": "ie", "uk": "co.uk", "british": "co.uk", "london": "co.uk",
    "turkey": "com.tr", "turkish": "com.tr", "israel": "co.il", "israeli": "co.il",
    "uae": "ae", "dubai": "ae", "emirati": "ae", "saudi": "com.sa",
    "estonia": "ee", "czech": "cz", "romania": "ro", "greece": "gr",
}


def _cctld_for(text: str) -> str | None:
    low = (text or "").lower()
    for key in sorted(_CCTLDS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", low):
            return _CCTLDS[key]
    return None
