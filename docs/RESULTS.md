# Results, and what they cost

The honest account of what this agent produced, measured from its own run
database rather than from memory. Every number below comes from
`data/tvb_agent.sqlite3` across **17 runs** and can be recomputed from the
shipped bank (`data/shipped_leads.json`), from `python tvb.py leads`, or from
the **Run history** tab in the app.

## The short version

**One company on file today clears all four requirements with a verified
founder email.** 750 distinct companies were researched to find it. The brief
asks for 15; this is the honest count, not the achievable one, and the rest of
this document is the measurement that explains the gap.

## The funnel, across 17 runs

| Stage | Count |
|---|---|
| Search queries issued | 2,874 |
| · discovery queries | 950 |
| · per-company probes | 1,940 |
| Candidates found | 2,286 |
| Candidates already seen in an earlier run (skipped) | 965 |
| Funds, publications, agencies and coworking spaces skipped before any budget was spent | 124 |
| Companies researched | 1,294 attempts across 750 distinct companies |
| Pages fetched | 9,721 |
| Cache hits (pages not re-fetched) | 1,022 |
| Claims dropped as ungrounded | 2 |
| Total run time | 2 h 38 m |

## Which requirement rejected companies failed

A company can fail more than one, so these do not sum to the rejection count
(744 rejected company records).

| Requirement | Companies that failed it |
|---|---|
| **Verified founder email** | **733** |
| $1M–$5M revenue or funding | 503 |
| Minimal-to-no US presence | 498 |
| Named CEO or co-founder | 372 |
| Operates a technology platform | 331 |

## The email, in detail

This is the finding, and it is the point of the whole exercise. Across the 750
distinct companies researched:

| Outcome | Count |
|---|---|
| No address published on any source read | 449 |
| Address found but not attributable to the named founder, or on a source without standing | 183 |
| Only a generic role address (`info@`, `hello@`) | 94 |
| Address found and rejected as undeliverable | 5 |
| Source-verified, no delivery confirmation | 2 |
| **Verified founder address** | **17** |

**2.3% of researched companies yielded a verified founder email**, and only a
fraction of those companies also sat in the $1M–$5M band with no US presence —
which is why 17 verified addresses produce one qualified lead, not seventeen.

## Why the number is small, in order of size

1. **Most companies do not publish a founder's personal email.** This is a fact
   about the world, not a defect in the agent. A contact form and `info@` are
   the norm. The two contact databases that would close this gap are gated:
   Apollo's free tier returns an `email_not_unlocked@` placeholder rather than
   an address, and Hunter's free allowance is a few dozen lookups a month. Of
   the 17 verified addresses, 7 came from enrichment and the rest from pages the
   agent read directly.
2. **Free-tier search limits.** Serper is 2,500 queries for the lifetime of an
   account; this project has issued 2,874 across two accounts' allowances.
   Tavily now requires a card, Brave retired its free tier, and Google's Custom
   Search API is closed to new customers. A run large enough to reach 15 leads
   costs more search credits than a free account has ever had.
3. **The gates are strict on purpose.** The rejections above are companies
   correctly excluded. The brief asks for companies in a narrow band with a rare
   contact type; the intersection is genuinely small.

## What was NOT done to make the number bigger

Each of these was available, and each was refused:

- **No email address is ever constructed from a name and a domain.** Not once,
  on any path. `firstname@company.com` is the single easiest way to manufacture
  15 leads, and every one would be a guess. Hunter answers that cite no source
  page are discarded for the same reason — a pattern guess is a guess whoever
  makes it.
- **A figure outside $1M–$5M is never swapped for a smaller one found on the
  same page.** An earlier build did exactly this — a company that had raised
  $52M qualified on a $2.5M seed round in the same paragraph — and three
  false-positive leads reached a delivered list before it was caught.
- **A generic `info@` address is never presented as a founder contact.** The
  named officer's own desk (`ceo@`, `founder@`) is accepted and labelled as
  such; a shared inbox is not.
- **Leads found under older, weaker rules are not grandfathered in.** Every
  stored lead is re-checked against the current rules on every read. Five
  earlier "qualified" leads have been dropped by this mechanism, by name and
  with a reason, and the app and CLI both print why:

  | Dropped lead | Reason it no longer passes |
  |---|---|
  | Spreaker | the evidence for France never says where Spreaker is based — it quotes a page that merely mentions the place |
  | Kaiko Systems | "Personal E-Mail" is a job description, not a person |
  | QuoteMachine | retail-insider.com names itself a publication, not a company TVB can invest in |
  | Lift99 | "Ex-Pipedrive Founder" is a job description, not a person |
  | Serena | serena.vc is a `.vc` domain, which is a venture fund's address |

  The Spreaker case is worth reading in full, because it is the sharpest example
  of what "evidence" has to mean here. Spreaker is a podcast hosting platform;
  its home page lists shows in many languages, one of which is titled *"Little
  Talk in Slow French"*. The agent read that title as a statement that the
  company is French, cleared the US-presence gate on it, and banked a lead for a
  company owned by a US broadcaster. Nothing was fabricated — the quote is real
  and the page is real — and it was still wrong, because the sentence never
  located anybody. A demonym now only establishes a country when it actually
  modifies a company ("the French fintech startup"), and the same check runs
  over every lead already on file.

## What would change the number

- **A paid search tier.** ~2,000 additional Serper credits would let a single
  run research the several hundred companies the measured conversion needs. This
  is a few dollars, and it is the largest single lever.
- **A paid contact-data seat.** Apollo or Hunter on a paid plan addresses the
  binding constraint directly: it turns the 449 "no address found" companies
  into candidates rather than dead ends.
- **A headless browser** for the minority of sites that render their contact
  page in JavaScript. Measured at 14% of hosts, so smaller than it looks — this
  was checked rather than assumed.

## What ships with the repository

`data/shipped_leads.json` carries the verified lead **and 80 rejected
companies**, each with the requirement it failed and the sentence behind that
verdict. The hosted app replays them on first load so a reviewer sees real
output without spending a credit. Two properties of that file matter:

- The rejections load as rejections. They are stored with their failing gates
  and can never be read back as leads.
- Contact details are masked in every rejected record before the file is
  committed. A company that did not qualify is carried for its gate reason, not
  as a contact, and this repository is public.

## How to verify all of this yourself

```bash
python tvb.py leads --json leads.json   # every verified lead on file
python tvb.py audit leads.json          # re-verify them independently, from scratch
python tvb.py run -v                    # watch a live run, with its reasoning
```

`audit` re-fetches every cited source and re-checks every claim without
trusting anything the original run recorded. A lead that cannot survive that is
not a lead.
