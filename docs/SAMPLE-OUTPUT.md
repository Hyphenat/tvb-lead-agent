# Sample output

> **These records were produced by the test suite's simulated web, not by a real run.**
> They exist so a reviewer can see the exact shape of the output — including the evidence
> and gate trail — without installing anything or spending API credits. The companies below
> are fixtures defined in `tests/fake_web.py`; they are not real businesses, and the email
> addresses are not real addresses.
>
> To see genuine results, run the app and press **Find New Companies**.

Two of the four fixture companies qualify. The third (`Orbit Sim`) is rejected because its
own site states a New York office, and the fourth is the VC portfolio page the agent used
for discovery, which correctly fails every gate.

## CSV shape

```csv
company_name,description,industry_sector,funding_or_revenue,funding_type,ceo_or_cofounder,founder_title,verified_email,website,country,us_presence,confidence,tvb_fit_score,evidence_urls
Zeta Care,Zeta Care is a care coordination platform used by clinics across India.,Healthtech / Digital health,$2.40M,funding_raised,Priya Raman,Co-founder & CEO,priya.raman@zetacare.in,https://zetacare.in,India,none,0.886,1.0,https://techcrunch.com/2026/zeta-care-seed | https://zetacare.in | https://zetacare.in/about | https://zetacare.in/careers | https://zetacare.in/contact | https://zetacare.in/team
Nova Pay,Nova Pay is an embedded finance platform for marketplaces in Nigeria.,Fintech / Payments,$3.10M,funding_raised,Chidi Okonkwo,Founder and CEO,chidi.okonkwo@novapay.ng,https://novapay.ng,Nigeria,none,0.886,0.94,https://novapay.ng | https://novapay.ng/about | https://novapay.ng/team | https://techcrunch.com/2026/zeta-care-seed
```

## JSON shape (one lead, abbreviated)

```json
{
  "company_name": "Zeta Care",
  "description": "Zeta Care is a care coordination platform used by clinics across India.",
  "industry_sector": "Healthtech / Digital health",
  "funding_or_revenue": "$2.40M",
  "funding_type": "funding_raised",
  "ceo_or_cofounder": "Priya Raman",
  "founder_title": "Co-founder & CEO",
  "verified_email": "priya.raman@zetacare.in",
  "website": "https://zetacare.in",
  "country": "India",
  "us_presence": "none",
  "qualified": true,
  "confidence": 0.886,
  "tvb_fit_score": 1.0,
  "evidence_urls": "https://techcrunch.com/2026/zeta-care-seed | https://zetacare.in | https://zetacare.in/about | https://zetacare.in/careers | https://zetacare.in/contact | https://zetacare.in/team",
  "evidence": [
    {
      "url": "https://zetacare.in",
      "quote": "Company website resolved at zetacare.in.",
      "authority": "company_owned",
      "title": null,
      "note": null
    },
    {
      "url": "https://zetacare.in",
      "quote": "Zeta Care Zeta Care is a care coordination platform used by clinics across India. Our software connects providers, patients and social care teams through a single dashboard and API. About Team Contact Careers Product",
      "authority": "company_owned",
      "title": "Zeta Care - Care coordination platform",
      "note": "rule-based extraction"
    },
    {
      "url": "https://zetacare.in",
      "quote": "a Care Zeta Care is a care coordination platform used by clinics across India. Our software connects providers, patients and social care teams through a single dashboard and API. About Team Contact Careers Product",
      "authority": "company_owned",
      "title": "Zeta Care - Care coordination platform",
      "note": "rule-based classification"
    },
    {
      "...": "6 more"
    }
  ],
  "gates": [
    {
      "gate": "funding_or_revenue_pass",
      "passed": true,
      "reason": "$2.40M of funding raised is within $1M-$5M.",
      "evidence_urls": [
        "https://techcrunch.com/2026/zeta-care-seed"
      ]
    },
    {
      "gate": "technology_platform_pass",
      "passed": true,
      "reason": "Evidence of an operating technology product or platform was found.",
      "evidence_urls": [
        "https://zetacare.in",
        "https://zetacare.in/careers",
        "https://zetacare.in/team"
      ]
    },
    {
      "gate": "us_presence_pass",
      "passed": true,
      "reason": "Headquarters in India. No US office, subsidiary, US incorporation or US job posting found across 6 page(s) checked; 0 weak signal(s), within the threshold of 1.",
      "evidence_urls": [
        "https://zetacare.in/about"
      ]
    },
    {
      "gate": "founder_identified",
      "passed": true,
      "reason": "Priya Raman identified as Co-founder & CEO.",
      "evidence_urls": [
        "https://zetacare.in/team"
      ]
    },
    {
      "gate": "email_verified",
      "passed": true,
      "reason": "priya.raman@zetacare.in verified (source-attributed, MX valid, zerobounce says deliverable).",
      "evidence_urls": [
        "https://zetacare.in/contact"
      ]
    }
  ],
  "email_detail": {
    "status": "verified",
    "mx_ok": true,
    "mx_hosts": [
      "mx1.zetacare.in"
    ],
    "deliverability": "deliverable",
    "verifier": "zerobounce",
    "is_catch_all": false,
    "domain_matches_company": true,
    "role_fallback": "info@zetacare.in",
    "notes": [
      "Deliverability check (zerobounce): valid/alias_address"
    ]
  },
  "discovered_via": [
    "https://techcrunch.com/2026/zeta-care-seed",
    "https://exampleventures.com/portfolio"
  ],
  "pages_fetched": [
    "https://zetacare.in",
    "https://zetacare.in/about",
    "https://zetacare.in/team",
    "https://zetacare.in/contact",
    "https://zetacare.in/careers",
    "https://zetacare.in/product"
  ],
  "near_boundary": false,
  "fit_reasons": [
    "Sector 'health' aligns with a TVB Orbit.",
    "India is a TVB Hub geography.",
    "$2.40M sits in TVB's seed-to-Series-A sweet spot.",
    "Direct founder-level contact available."
  ]
}
```

## What to look for

* every gate carries a `reason` and the URLs whose text supports it;
* `email_detail` shows the full verification trail, including MX hosts and whether the domain
  is catch-all;
* `discovered_via` records where the company was *found*, kept separate from the evidence that
  proves anything about it;
* `funding_type` is always `funding_raised`, `revenue` or `arr` — never a valuation or market size.
