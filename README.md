# TVB Lead Agent

An autonomous discovery-and-qualification agent that finds technology companies matching
**The Venture Build's** target profile, verifies them against evidence, and returns only the
leads a TVB operator could contact today.

> **Live app:** https://tvb-lead-agent-zvfedv42etsdexrmrbqsiy.streamlit.app/
> **Repository:** https://github.com/Hyphenat/tvb-lead-agent

### Start here

| If you want to… | Go to |
|---|---|
| **Run it with your own API key** | [`docs/SETUP.md`](docs/SETUP.md) — or just paste a key into the **🔑 Use your own API keys** panel in the hosted app's sidebar |
| **See what it actually produced, and what that cost** | [`docs/RESULTS.md`](docs/RESULTS.md) — the measured funnel over 750 researched companies |
| **See the shape of a lead without running anything** | [`docs/SAMPLE-OUTPUT.md`](docs/SAMPLE-OUTPUT.md) — from the test fixtures, clearly not real companies |
| **Understand why the number is small** | [Limitations](#limitations-and-the-measured-cost-of-them), below |

**Read this before judging the output.** This agent returns *fewer* leads than the brief's
minimum of 15, and that is a deliberate, measured choice rather than an unfinished one. Across 17
runs it researched 750 distinct companies on the live web; **17 of them published a founder email
that could be verified**, and one of those also sat inside the $1M–$5M band with no US presence.
That one is the lead on file. Most companies simply do not publish a founder's address, and the
contact databases that would close that gap are paywalled. The alternative was to construct
addresses like `firstname@company.com` — fifteen rows instantly, every one a guess.

**So the hosted app opens with what actually happened**: the verified lead, and 80 rejected
companies each carrying the requirement it failed and the sentence behind that verdict. The
rejections are the substance of the result, not an apology for it.
[`docs/RESULTS.md`](docs/RESULTS.md) sets out the full funnel, the five earlier "qualified" leads
that later rules retroactively dropped (with reasons), and the two levers that would change the
number.

---

## Running it with your own keys

Full instructions in [`docs/SETUP.md`](docs/SETUP.md). The short version:

**In the hosted app** — sidebar → **🔑 Use your own API keys** → paste a Serper key → run. The key
lives in your browser session only: never written to disk, never logged, gone when the tab closes,
and invisible to anyone else using the same URL. This exists because the deployment ships with
free-tier keys whose allowances run out, and an agent that cannot search says nothing about whether
it works.

**On your own machine:**

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # 565 tests, no API key needed - the whole pipeline
                             # runs against a simulated web with no network access
cp .env.example .env         # run this ONCE; running it again wipes your keys
# edit .env, then:
python tvb.py doctor --live  # says which keys work, which are broken, credits left
python tvb.py run
```

Only `SERPER_API_KEY` (serper.dev, free tier) is genuinely required. Everything else degrades
gracefully and says so on each affected lead.

---

## The problem this solves

TVB is an execution ecosystem, not a fund. Its economics work when it reaches companies that
already have a product and early traction but still have gaps in GTM, market access, capital
readiness or operations — and it reaches them by emailing the founder. So the job is not
"scrape some startups". The job is:

> Continuously surface non-US, seed-to-Series-A technology companies in TVB's Orbit sectors,
> with enough evidence attached that an operator can send a cold email without doing their own
> diligence first.

That framing drove two decisions that shape the whole codebase:

1. **Precision over volume.** A false positive costs TVB a burned founder relationship. The
   system would rather return 15 airtight leads than 40 shaky ones.
2. **Evidence is the product.** Every claim carries a source URL *and the verbatim sentence
   that supports it*. This doubles as the anti-hallucination mechanism.

## Target profile

A company qualifies only when **all five** hard gates pass:

| Gate | Requirement |
|---|---|
| `funding_or_revenue_pass` | $1M–$5M USD raised **or** earned, evidenced and correctly typed |
| `technology_platform_pass` | Operates a real technology product or platform |
| `us_presence_pass` | Minimal-to-no US presence, per the documented rule below |
| `founder_identified` | Named CEO or co-founder, bound to the company by a source |
| `email_verified` | Founder email found on an authoritative source and verified |

Anything that cannot be established is **left blank and fails its gate**. Nothing is guessed.

---

## Architecture

```
                 ┌─────────────────────── Streamlit UI ───────────────────────┐
                 │  Run · Qualified · Rejected · Log · History · Methodology   │
                 └────────────────────────────┬───────────────────────────────┘
                                  ┌───────────▼───────────┐
                                  │   Agent orchestrator   │◄── stop: target reached
                                  │  (budgeted, resumable) │    OR budget exhausted
                                  └───────────┬───────────┘
              ┌───────────────────────────────┼───────────────────────────────┐
              ▼                               ▼                               ▼
   ┌──────────────────┐          ┌─────────────────────┐        ┌────────────────────┐
   │  Query planner   │          │ Candidate extractor │        │  Frontier queue    │
   │ sector × geo ×   │─queries─▶│ SERP · articles ·   │─cands─▶│ dedup + priority   │
   │ phrasing × window│          │ portfolio expansion │        │                    │
   │ × source shape   │          └─────────────────────┘        └─────────┬──────────┘
   └────────▲─────────┘                                                    │
            └──────────── novelty feedback (SQLite frontier) ──────────────┘
                                                                           ▼
                                            ┌──────────────────────────────────────────┐
                                            │  Research: company site (about/team/     │
                                            │  contact/careers/product) + targeted SERP │
                                            └──────────────┬───────────────────────────┘
                                                           ▼
                                            ┌──────────────────────────────────────────┐
                                            │  Grounded extractor                      │
                                            │  every claim ⇒ (value, verbatim quote)    │
                                            │  quote not in page ⇒ claim DISCARDED      │
                                            └──────────────┬───────────────────────────┘
                                                           ▼
        ┌──────────────┬──────────────┬───────────────┬────────────────┬─────────────────┐
        ▼              ▼              ▼               ▼                ▼                 ▼
    Funding /      Technology      US presence     Founder         Email discovery   Email
    revenue        platform        (documented     identification  (authoritative    verification
    (FX, type)     (product        rule)                            sources only)     ladder
                    evidence)
        └──────────────┴──────────────┴───────────────┴────────────────┴─────────────────┘
                                                           ▼
                                    ┌──────────────────────────────────────────┐
                                    │  Qualification engine: 5 hard gates      │
                                    │  verdict FIRST, scores after (advisory)  │
                                    └────────────────┬─────────────────────────┘
                                                     ▼
                            SQLite  ·  Qualified leads  ·  Rejection ledger  ·  CSV/JSON
```

### Layout

```
tvb-lead-agent/
├── tvb.py                        # zero-install launcher for the CLI
├── app/streamlit_app.py          # web interface
├── src/tvb_agent/
│   ├── agent.py                  # orchestrator: the discovery → qualification loop
│   ├── audit.py                  # independent re-verification of an exported list
│   ├── diagnostics.py            # key format + live provider checks
│   ├── config.py                 # env/secrets, budgets, thresholds
│   ├── models.py                 # Evidence, Evidenced[T], CompanyProfile, gates
│   ├── storage.py                # SQLite: companies, evidence, frontier, runs, caches
│   ├── export.py  cli.py         # CSV/JSON export, command-line runner
│   ├── providers/                # search · LLM · fetcher · email verification
│   ├── discovery/                # query planner · candidate extraction · frontier
│   ├── research/                 # site crawler · grounded extractor · authority
│   ├── validation/               # money · fx · geo · validators · email ladder
│   └── qualify/                  # engine (hard gates) · TVB fit score
├── tests/                        # unit, integration, resilience, audit and UI tests
├── pyproject.toml                # deps, entry point, pytest and ruff config
└── .github/workflows/ci.yml      # lint + tests on 3.11 and 3.12
```

---

## Discovery methodology

There is **no built-in company list**. The agent explores a *space of places to look*:

```
cell = (sector × geography × funding-phrasing × time-window × source-shape)
```

- **17 sectors** mirroring TVB's Orbits (healthtech, edtech, AI, cybersecurity, digital twin,
  fintech, traveltech, B2B SaaS, …)
- **36 geographies** mirroring TVB's Hubs plus adjacent non-US markets
- **8 funding phrasings**, including non-English ones (`levée de fonds`, `Millionen Euro`,
  `ronda de financiación`, `crore`) because genuinely non-US companies are covered in local media
- **7 time windows**, **9 source shapes** (in-band amount, funding trackers, funding news,
  funding roundups, VC portfolios, accelerator cohorts, directories, company sites, award lists)

The highest-weighted shape searches the **figure itself** — `"raises $1.5 million" logistics
startup Vietnam 2026` — because it is the only shape that is in-band by construction: the company
is discovered *by* the number that has to clear the funding gate, and the headline carrying it
becomes the evidence. Shape weights were set by running the queries against the live web, not by
intuition; VC portfolio pages score *negatively* because a real run found they mostly surface the
fund rather than its companies. No shape may take more than half the cells in a run, so the best
one leads without the agent ever narrowing to a single way of looking.

Geographies with **imprint law** (Germany, France, the Netherlands, Italy, Spain, Poland,
Switzerland, Austria …) are weighted up, because a named and contactable company representative is
a legal requirement there rather than a courtesy — and a verified founder email is the scarcest
thing in this entire pipeline. This is a yield bias, not a relaxed requirement: those companies
clear exactly the same five gates as everyone else.

That is **~308,000 distinct cells**. Each run scores cells by novelty × observed yield and works
the best ones. Exploration state lives in SQLite, so **a second run searches different ground and
skips companies it has already seen** — this is a structural property, and it has a test.

A second channel does the heavy lifting: any page that looks like a **link hub** (VC portfolio,
accelerator cohort, "top 20 startups in X") is fetched and its outbound company links are
expanded into candidates. This is how the agent finds sources nobody handed it.

> **Discovery is not evidence.** *Where* a company was found proves nothing about it.
> `discovered_via` is stored separately from `evidence` and can never satisfy a gate.
>
> The words that source *published* are a different thing. "Kenyan AI startup Flowt raises pre-seed
> round… Flowt, a Nairobi-based workflow automation platform, has raised $2 million… Founder Alice
> Wanjiru" carries the funding, the country and the founder. That excerpt is admitted as an ordinary
> source, held to every ordinary rule — the host must have standing, the sentence must name the
> company, the quote must be verbatim — and it is labelled in the evidence trail as a search-engine
> excerpt so a reader always knows the full page was not retrieved. An earlier build discarded these
> outright and then rejected real companies for having no verifiable funding.

---

## Validation methodology

### Funding / revenue

Parsed **deterministically** (no LLM) from page text across ~50 currencies, including the Indian
crore/lakh system and both `1,234.56` and `1.234,56` notations. Every figure is then **classified**:

| Accepted | Rejected as evidence for this criterion |
|---|---|
| `funding_raised`, `revenue`, `arr` | `valuation`, `tam`, `acv`, `aum`, `grant`, `fund_size`, `projection`, `debt`, `unknown` |

So "valued at $50 million", "the market is worth $4 billion", "closes $5M maiden fund" and
"expects to reach $10M by 2028" can never become funding. Further rules:

- **Attribution.** Unless the page is the company's own site, the sentence containing the figure
  must name the company. A press article covering three startups cannot cross-assign their
  numbers — the most damaging error this pipeline could make, and it has a dedicated test.
- **Source standing.** Funding evidence must come from an aggregator-or-better source.
  Recognised outlets and databases are listed by host; an *un*recognised outlet is admitted
  at the floor rank — the same standing as a startup database, never higher — and only when
  its name reads like a publication *and* the URL is an article rather than a home page. This
  exists because no curated list can name every regional outlet that reports a seed round,
  and a real run discarded genuine funding evidence from a dozen of them. Every other
  safeguard still applies: the sentence must name the company, the quote must be verbatim,
  and the figure is parsed out of the page rather than summarised.
- **Cumulative preferred.** "has raised a total of $4.2M to date" beats a single round, avoiding
  double-counting of extensions.
- **FX.** A dated rate table (`validation/fx.py`, date stored on every amount). Amounts within
  10% of a band edge are flagged `near_boundary` in the UI and exports.
- **Local-language coverage.** Non-US companies are reported in local media, so the parser reads
  funding and revenue vocabulary in German, French, Spanish, Portuguese, Italian, Dutch, Swedish,
  Polish, Turkish, Indonesian, Vietnamese and Arabic, along with their number words
  (`Millionen`, `millones`, `milhões`, `miljoen`, `milionów`, `juta`, `crore`, `lakh`).
- **Ambiguous currencies are resolved, never assumed.** "Pesos" spans a 200x range across Mexico,
  Colombia, Chile and the Philippines, and "Rs" spans four countries, so these resolve from the
  country, demonym or city named on the page — and the figure is **dropped** when nothing settles
  it. An unrecognised currency token is likewise never treated as dollars: reading "40 milyon TL"
  as $40m would put a company in the band on money it never raised.

### Technology platform

Requires evidence of a real product — platform, app, API, docs or pricing page — not a sector
label. A services/consulting business is rejected even if its copy says "platform".

### US presence — the documented rule

**PASS ("minimal / no US presence") requires all of:**

1. Headquarters country established as non-US from evidence;
2. no US office or US address found on the company's site (about / contact / locations / legal);
3. no US subsidiary or US entity stated;
4. no US incorporation (including Delaware) stated;
5. no US-located job postings on the careers page;
6. at most `MAX_WEAK_US_SIGNALS` (default 1) weak signals — US phone number, US locale page,
   US press dateline, incidental US address mention.

**FAIL if** any of 2–5 is positively found, **or if the headquarters country cannot be
established.** An unknown headquarters *never* passes: "we could not tell" is not the same as
"there is no US presence", and treating it as such is how a list quietly fills with companies TVB
cannot use. The pages checked and every signal found are stored as evidence.

### Founder

A named CEO or co-founder, tied to the company by a source mentioning both. A CTO, Head of Sales
or unnamed "our team" page does not satisfy this gate.

Imprint pages are read in German, French, Dutch, Italian, Spanish and Polish, and the office the
imprint actually states is recorded rather than assumed. Only offices that genuinely *are* the
chief executive are accepted — `Geschäftsführer`, `Gérant`, `Bestuurder`, `Prezes Zarządu`,
`Amministratore delegato`, which are the statutory managing director. A `Directeur de la
publication`, a `Legale rappresentante` or a `Vorstand` member is recorded and then **rejected**,
because a press-law officer, a company lawyer and a board member are none of them a CEO or a
co-founder. Surnames carrying lowercase particles — *van*, *de*, *dos*, *bin* — are read correctly;
a pattern insisting on initial capitals silently loses the person.

### Email verification ladder

| Status | Meaning | Reaches output? |
|---|---|---|
| `not_found` | no address located | ✗ |
| `role_only` | only `info@` / `hello@` — stored separately as a fallback contact | ✗ |
| `found_unverified` | seen on a low-authority page, or not attributable to the founder | ✗ |
| `source_verified` | published on an **authoritative** source and attributed to the founder | ✗ |
| **`verified`** | source-verified **+** valid syntax **+** not role/disposable **+** domain has MX **+** deliverability confirmed — or *no provider answered at all* (none configured, free tier spent, every provider errored), in which case the limitation is written onto the lead in full | **✓** |
| `invalid` | bad syntax, no MX, disposable, or the verifier rejected it | ✗ |

Authoritative means the company's own domain, an official registry, or first-tier press. Crunchbase-
style aggregators and LinkedIn are **not** sufficient on their own.

### Contact enrichment, and why it is labelled separately

Most companies never publish their founder's address, so an agent that can only read pages finds
only the minority that do. Two optional providers close that gap, and they are **not** treated
alike, because one of them can show its working:

**Hunter Email Finder — asked first.** Hunter returns the **URLs where it saw the address**. So the
agent goes and reads those pages itself: if the address is really in the text, the finding stops
being a database claim and becomes an ordinary evidenced source with that page's own standing —
quotable, checkable, and auditable by anyone who follows the link. An answer Hunter cites **no**
source for is a pattern inferred from the domain, and is discarded. This project does not ship
guesses, its own or anybody else's.

**Apollo People Enrichment — asked second, and only `email_status: verified`.** Apollo does not say
where it looked, so an Apollo address can never be promoted to a read source; it stays labelled as
a database answer.

Both are held to the same rules:

- they are **asked about a founder the agent already identified from its own evidence**. Neither
  ever supplies the name — that claim is the one most easily corrupted, and it stays grounded;
- the returned record must be **the person asked about**, and the address must sit on the
  company's own domain — an address elsewhere is an old employer or a wrong match;
- it then clears the same technical ladder as any other address: syntax, not a role account, not
  disposable, real MX records, a deliverability check;
- the provenance is recorded literally, naming the provider and, where there is one, the page it
  cited. The run reports how many verified addresses were confirmed by reading a cited page and
  how many rest on a database answer alone.

So a reader of any export can always tell the three apart — found on a page the agent found itself,
found on a page a provider pointed at, or asserted by a database — and `python tvb.py doctor --live`
says up front whether each provider's plan will actually answer.

## The deliverable is cumulative

Every run deliberately searches ground the previous runs did not — that is what the SQLite
exploration state is for — and companies already seen are skipped. So the lead book is the
**union of all runs**, not one run's slice:

```
python tvb.py run                       # add to the book
python tvb.py leads                     # everything verified so far
python tvb.py leads --csv leads.csv --json leads.json
python tvb.py run --all-runs --csv leads.csv    # run, then export the whole book
```

Nothing is pooled, averaged or relaxed by being counted together: each lead on file cleared the
same five hard gates on its own evidence, and `python tvb.py audit leads.json` re-verifies any
export from scratch, independently of the run that produced it.

## How to use the app

1. Open the URL.
2. Check the **sidebar** — provider status shows what is live and what is degraded.
3. Optionally narrow by sector or geography, or raise the target.
4. Press **Find New Companies**. The log streams the agent's reasoning live.
5. **Qualified leads** tab: each lead shows funding, founder, verified email, gate pills, and an
   expander with the exact quote and URL behind every verdict.
6. **Rejected** tab: every company investigated and turned down, with the requirement it failed.
   This is published deliberately — it is what makes the passing list trustworthy.
7. Export **CSV** (flat) or **JSON** (with the full evidence and gate trail).

---

## Independent audit

A pipeline that marks its own homework is not evidence of data quality, so the lead list can be
re-checked from scratch — by you, or by anyone reviewing the output:

```bash
python tvb.py audit leads.json --json audit-report.json
```

It re-fetches every evidence URL, confirms the quoted sentence is still present on the page, and
re-checks each address for syntax, role-account status and MX records. Anything it cannot confirm
is reported rather than quietly kept. The same audit is available in the app, under
**Re-verify this list independently** on the Qualified leads tab.

```
========================================================================
AUDIT: 15/15 leads re-verified cleanly
Evidence: 61/63 quotes still found at their source URL
========================================================================
```

## Testing

```bash
python -m pytest -q          # full suite
python -m pytest -q -k money # the deterministic parser
```

Coverage goes beyond "it starts":

- multi-currency parsing (crore/lakh, euro decimal notation, ~50 currencies)
- every rejecting amount type (valuation, TAM, ACV, AUM, grant, fund size, projection, debt)
- **cross-company contamination** — two startups in one article must not swap figures
- fabricated LLM claims (invented funding, invented founder, invented email) never reach a lead
- a company with a real US office is rejected
- role emails never become the founder contact
- a second run skips companies already seen
- total search-provider failure, and email-verifier outage, both survive
- the Streamlit app renders with an empty database and with results
- description and sector are produced with **no LLM configured** (the output columns the brief
  requires must not depend on an optional key)
- the search budget ceiling, the discovery/probe split, and the LLM and email-verification budgets
- boilerplate ("we use cookies…") is never used as a company description
- name folding merges legal-suffix variants but keeps "Acme Labs" and "Acme Software" apart
- an unrecognised currency token is dropped, never read as dollars
- ambiguous currency words ("pesos", "Rs") resolve from context or are dropped
- funding stated in German, Spanish, Portuguese, Italian, Dutch, Swedish, Polish and Turkish
- a founder email published only on an unlinked `/impressum` page is still found
- the audit flags a lead whose quoted evidence is no longer on the page

Run `python -m pytest -q` — **258 tests, no network access required**.

---

## Limitations, and the measured cost of them

Built and evaluated entirely on free tiers. Those limits are not a footnote —
they set the size of the output, and [`docs/RESULTS.md`](docs/RESULTS.md) has
the full measured funnel across 17 runs. The short version:

| Constraint | Effect |
|---|---|
| Serper: 2,500 searches for the life of an account | A run large enough to reach 15 leads needs more credits than a free account ever has |
| Apollo free tier returns `email_not_unlocked@` rather than an address | The contact database that would close the email gap is unavailable |
| Hunter free tier: a few dozen finder lookups a month | The one provider that cites its sources runs out quickly |
| Gemini free tier: rate-limited within ~8 calls | Extraction falls back to deterministic rules — weaker descriptions, everything else unchanged |
| ZeroBounce: 100 verifications | Deliverability falls back to authoritative-source attribution + MX, recorded on each lead |

**The binding constraint is not search, and it is not the code.** Measured over
750 researched companies, **17 published a founder email that could be verified**
— 2.3%. Most companies simply do not publish one. Everything else in the pipeline — funding
in band, a real platform, no US footprint, a named founder — clears far more
often than that.

The honest consequence: this agent returns fewer leads than the brief's minimum,
and it returns them with evidence rather than returning fifteen with guesses. The
two levers that change the number are a paid search tier and a paid contact-data
seat, in that order.

## Future improvements

- Postgres storage for durable history across deployments
- Scheduled background runs with a lead inbox and change alerts
- TVB-voiced outreach draft per lead, using the positioning language from the TVB overview
- Direct CRM / Airtable / Sheets sync
- Calibration reporting: measured precision against a hand-checked sample
- Multi-language extraction prompts for French, Portuguese and Bahasa sources

---

## License

MIT — see [LICENSE](LICENSE).
