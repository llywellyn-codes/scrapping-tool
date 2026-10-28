# JSON prospect record

Each prospect can be exported as one JSON record. The first keys of each block follow the agreed
record format. The extra keys after them carry the rest of the required per-prospect output.

Where to get it:

- **One prospect:** `GET /prospects/<id>.json`, or the **JSON** button on the prospect page
- **All prospects:** `GET /export.json`, the **Export JSON** link, `flask --app prospect_finder export prospects.json`, or `flask --app prospect_finder report --json`

The bulk export leaves out opted-out prospects, the same as the CSV.

## Example

This record comes from a demo scan, with the URL and timestamp shortened. It shows 3 of the 21
findings.

```json
{
  "business": {
    "business_id": "1",
    "name": "Bayou Shield Roofing Co.",
    "website": "https://bayoushield.example/",
    "phone": "+15045550117",
    "city": "New Orleans",
    "state": "LA",
    "niche_match": "confirmed",
    "withheld_fields": []
  },
  "scoring": {
    "business_fit": 16,
    "marketing_opportunity": 44,
    "commercial_readiness": 6,
    "raw_score": 66,
    "evidence_coverage": 0.9,
    "priority": "A",
    "review_status": "evidence_sufficient",
    "confidence": 0.79,
    "score_upper_bound": 79,
    "review_reasons": []
  },
  "findings": [
    {
      "signal": "no_clear_cta",
      "points_awarded": 1,
      "max_points": 5,
      "status": "observed",
      "finding_type": "observed_fact",
      "confidence": 0.75,
      "evidence_url": "https://bayoushield.example/",
      "evidence": "No link, button or heading on the homepage offers an estimate, inspection, quote or booking.",
      "checked_at": "2026-10-28T03:23:56Z",
      "outcome": "met",
      "test_name": "CTA text search",
      "test_result": "not found",
      "note": "reduced from 5 to 1: shares the 'Weak conversion path (form, call button, call-to-action)' cap of 17"
    },
    {
      "signal": "no_https",
      "points_awarded": 4,
      "max_points": 4,
      "status": "confirmed_failure",
      "finding_type": "confirmed_failure",
      "confidence": 0.85,
      "evidence_url": "https://bayoushield.example/",
      "evidence": "The https:// version failed while the http:// version loaded.",
      "checked_at": "2026-10-28T03:23:56Z",
      "outcome": "met",
      "test_name": "https load",
      "test_result": "https: TLS/certificate error; http: HTTP 200",
      "note": null
    },
    {
      "signal": "review_generation_gap",
      "points_awarded": 0,
      "max_points": 6,
      "status": "unknown",
      "finding_type": "not_verified",
      "confidence": null,
      "evidence_url": null,
      "evidence": null,
      "checked_at": null,
      "outcome": null,
      "test_name": null,
      "test_result": null,
      "note": "no finding recorded"
    }
  ],
  "top_findings": ["roofing_core_service", "no_lead_form", "missing_mobile_viewport"],
  "source_links": ["https://bayoushield.example/", "https://bayoushield.example/about.html"],
  "next_step": "Research 'Fewer than 25 Google reviews': Look up their Google Business Profile and note the review count.",
  "attribution": [],
  "outreach": {"status": "new", "last_contact_date": null, "response": null, "opted_out": false}
}
```

## Fields

### `business`

| Key | Meaning |
|---|---|
| `business_id` | Internal prospect ID, as a string |
| `name`, `website`, `city`, `state` | Identity fields. `null` if the field's source does not allow export |
| `phone` | E.164 (`+1XXXXXXXXXX`) when it is a valid US number |
| `niche_match` | `confirmed` (roofing is the primary service, verified), `not_matched` (verified not roofing), or `unverified` |
| `withheld_fields` | Fields left out, each with the reason (e.g. Google Places content, or a CSV batch not marked export-permitted) |

### `scoring`

| Key | Meaning |
|---|---|
| `business_fit`, `marketing_opportunity`, `commercial_readiness` | Category scores out of 30 / 50 / 20 |
| `raw_score` | Sum of the three categories. Unknown signals count as 0 |
| `evidence_coverage` | 0–1 share of rubric weight with usable evidence (see [SCORING.md](SCORING.md)) |
| `priority` | Score tier `A`/`B`/`C`/`D` (≥60 / ≥40 / ≥20 / below 20). Read it together with `review_status` |
| `review_status` | `evidence_sufficient`, `needs_manual_verification`, `not_a_fit` or `opted_out` |
| `confidence` | Weighted average confidence of the findings that were used |
| `score_upper_bound` | Score if every unknown signal turned out to be met |
| `review_reasons` | Why manual verification is needed |

### `findings`

There is one entry per rubric signal, unknown ones included, so the record shows what was **not**
checked as well as what was.

| Key | Meaning |
|---|---|
| `signal` | Rubric signal key |
| `points_awarded` / `max_points` | Points after supersession and group caps. A met signal can show fewer than its maximum when it shares a capped budget (see `note`) |
| `status` | `observed`, `inferred`, `confirmed_failure`, `unknown`, or `superseded` (made irrelevant by a broken website) |
| `finding_type` | `observed_fact`, `inference`, `confirmed_failure`, `not_verified`, or `not_applicable` |
| `confidence` | 0–1. `null` when not verified |
| `evidence_url`, `evidence`, `checked_at` | Source URL, observation and UTC time of the check. `null` when not verified, or when the evidence came from a source that does not allow export (`note` says so) |
| `outcome` | `met` (the condition that earns points is true), `not_met` (checked, condition false), or `null` |
| `test_name`, `test_result` | The automated test or confirmed-failure test behind the finding |
| `note` | Cap reductions, supersession, or why the signal is not verified (`no finding recorded`, `stale: …`, `low confidence: …`, `missing evidence: …`, or the reason a check was blocked) |

Stale, low-confidence and evidence-less findings appear as `status: "unknown"` with the reason in
`note`, because they do not count toward the score or coverage. When a check was attempted but
could not decide (for example, robots.txt disallowed the page), `checked_at` holds the time of
the attempt.
