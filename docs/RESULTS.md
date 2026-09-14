# Results, and what they cost

This is the honest account of what the agent produced, measured from its own
run database rather than from memory. Every number here comes from
`data/tvb_agent.sqlite3` and can be recomputed with `python tvb.py leads` and
the run history in the app.

## The funnel, across 14 runs

| Stage | Count |
|---|---|
| Search queries issued | 2,223 |
| Pages fetched | 7,865 |
| Candidate companies examined | 1,592 |
| Companies actually researched | 1,077 |
| Companies rejected at a hard gate | 543 |
| Funds, publications and agencies skipped before any budget was spent | 101 |

## Which requirement rejected companies failed

A company can fail more than one, so these do not sum to the rejection count.

| Requirement | Companies that failed it |
|---|---|
| **Verified founder email** | **533** |
| $1M–$5M revenue or funding | 368 |
| Minimal-to-no US presence | 358 |
| Named CEO or co-founder | 245 |
| Operates a technology platform | 209 |

## The email, in detail

This is the finding, and it is the point of the whole exercise:

| Outcome | Count |
|---|---|
| No address found on any source read | 243 |
| Address found but not attributable to the named founder, or on a source without standing | 90 |
| Only a generic company address (`info@`, `hello@`) | 38 |
| **Verified founder address** | **14** |
| Source-verified but no delivery confirmation | 2 |

**Roughly 1.3% of researched companies yielded a verified founder email.**

That ratio, not the search budget, is what decides the size of the output. At
1.3%, reaching 15 leads requires researching on the order of 1,100 companies —
about 4,000 search credits, against a free tier of 2,500 for the life of the
account.

## Why the number is small, in order of size

1. **Most companies do not publish a founder's personal email.** This is a fact
   about the world, not a defect in the agent. A contact form and `info@` are
   the norm. The two contact databases that would close this gap are gated:
   Apollo's free tier returns an `email_not_unlocked@` placeholder rather than
   an address, and Hunter's free search allowance is a few dozen lookups a month.
2. **Free-tier search limits.** Serper is 2,500 queries for the lifetime of an
   account; this project consumed 2,223 of them. Tavily now requires a card,
   Brave retired its free tier, and Google's Custom Search API is closed to new
   customers. A run large enough to reach 15 leads costs more search credits
   than a free account has ever had.
3. **The gates are strict on purpose.** 533 email failures, 368 out-of-band
   figures and 358 US-presence failures are companies correctly excluded. The
   brief asks for companies in a narrow band with a rare contact type; the
   intersection is genuinely small.

## What was NOT done to make the number bigger

Stating these plainly, because each was available and each was refused:

- No email address is ever constructed from a name and a domain. Not once, on
  any path. `firstname@company.com` is the single easiest way to manufacture 15
  leads, and every one would be a guess.
- A figure outside $1M–$5M is never swapped for a smaller one found on the same
  page. An earlier build did exactly this — a company that had raised $52M
  qualified on a $2.5M seed round mentioned in the same paragraph — and three
  false-positive leads reached a delivered list before it was caught.
- A generic `info@` address is never presented as a founder contact. The
  named officer's own desk (`ceo@`, `founder@`) is accepted and labelled as such;
  a shared inbox is not.
- Leads found under older, weaker rules are not grandfathered in. Every stored
  lead is re-checked against the current rules on every read, and four earlier
  "qualified" leads were dropped by this mechanism after the rules were
  tightened — visible in the run output by name and reason.

## What would change the number

- **A paid search tier.** ~2,000 additional Serper credits would allow a single
  run to research the ~400 companies the measured conversion needs. This is a
  few dollars, and it is the largest single lever.
- **A paid contact-data seat.** Apollo or Hunter on a paid plan addresses the
  binding constraint directly: it turns the 243 "no address found" companies
  into candidates rather than dead ends.
- **A headless browser** for the minority of sites that render their contact
  page in JavaScript. Measured at 14% of hosts, so smaller than it looks — this
  was checked rather than assumed.

## How to verify all of this yourself

```bash
python tvb.py leads --json leads.json   # every verified lead on file
python tvb.py audit leads.json          # re-verify them independently, from scratch
python tvb.py run -v                    # watch a live run, with its reasoning
```

The audit command re-fetches every cited source and re-checks every claim
without trusting anything the original run recorded. A lead that cannot survive
that is not a lead.
