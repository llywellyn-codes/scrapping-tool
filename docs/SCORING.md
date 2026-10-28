# How the score is calculated

The application calculates every score from stored findings. No language model takes part in
scoring. The weights live in one file, `prospect_finder/rubric.py`. A test checks that the table
below matches that file and that each category can reach exactly its budget.

## The 100-point model

| Category | Budget | What it means |
|---|---:|---|
| Business fit | 30 | Is this a US residential roofing contractor that is operating? |
| Marketing opportunities | 50 | Are there problems an agency could fix: paid ads and tracking, website conversion, lead capture, follow-up automation? A "met" signal here means the problem was found. |
| Commercial readiness | 20 | Are there signs the business can afford marketing and act on it? |

## Signals and weights

<!-- rubric:start -->
| Category | Signal (`key`) | Max | How it is measured | Overlap rule |
|---|---|---:|---|---|
| Business fit | Roofing is the primary service (`roofing_core_service`) | 10 | Homepage title, H1, meta description or schema.org type names roofing; or a manual check of the website / state license record. |  |
| Business fit | Serves residential homeowners (`residential_services`) | 6 | Site text mentions residential roofing, homeowners, roof replacement, storm/hail damage or insurance claims. |  |
| Business fit | Service area is stated (`service_area_stated`) | 6 | Site text says 'serving ...', 'service area' or 'areas we serve', or schema.org areaServed. |  |
| Business fit | Business appears active (`active_business`) | 4 | Website loads and shows a current copyright year, or business status is operational in a permitted listing source, or confirmed manually. |  |
| Business fit | Reviews or testimonials are visible (`social_proof`) | 4 | Testimonials/reviews section or review widget on the site, or reviews confirmed manually. |  |
| Marketing opportunities | No working website (`no_working_website`) | 30 | A manual check confirmed the website is missing or broken (opened in a browser, or a search found no site). Automated request failures are recorded as failed checks, never as evidence; a missing URL alone is 'unknown'. | when met, replaces `no_lead_form`, `no_click_to_call`, `no_clear_cta`, `missing_mobile_viewport`, `no_https`, `no_ad_conversion_tracking`, `no_analytics`, `no_chat_or_booking` |
| Marketing opportunities | No lead-capture form found (`no_lead_form`) | 10 | No form with name/email/phone fields (or known form embed) on the homepage or contact page. | shares the `conversion_path` cap of 17 |
| Marketing opportunities | No tap-to-call phone link (`no_click_to_call`) | 6 | Homepage HTML has no tel: link. | shares the `conversion_path` cap of 17 |
| Marketing opportunities | No estimate/inspection call-to-action (`no_clear_cta`) | 5 | No link, button or heading on the homepage offering a quote, estimate, inspection or booking. | shares the `conversion_path` cap of 17 |
| Marketing opportunities | Not configured for mobile screens (`missing_mobile_viewport`) | 7 | Homepage has no <meta name=viewport content=width=device-width> tag. |  |
| Marketing opportunities | No working HTTPS (`no_https`) | 4 | https:// fails (connection or certificate error) while http:// loads. |  |
| Marketing opportunities | No ad conversion tracking detected (`no_ad_conversion_tracking`) | 8 | No Google Ads, Meta, Microsoft Ads, LinkedIn, TikTok or Nextdoor tags in homepage HTML. If a Google Tag Manager container is present the result is 'unknown'. | shares the `tracking` cap of 10 |
| Marketing opportunities | No website analytics detected (`no_analytics`) | 4 | No Google Analytics, Tag Manager or other analytics script in homepage HTML. | shares the `tracking` cap of 10 |
| Marketing opportunities | No chat, text or online booking tool (`no_chat_or_booking`) | 6 | No known chat, text-us, scheduling or booking widget/link on fetched pages. |  |
| Marketing opportunities | Fewer than 25 Google reviews (`review_generation_gap`) | 6 | Review count below 25 from the Google Places API (within its caching terms) or a manual check. |  |
| Commercial readiness | Offers financing (`financing_offered`) | 4 | Site mentions financing, payment plans or a known home-improvement lender. |  |
| Commercial readiness | Claims manufacturer certification (`manufacturer_certification`) | 4 | Site claims GAF, Owens Corning, CertainTeed, Malarkey, IKO, Atlas or TAMKO certification (a claim until verified in the manufacturer's directory). |  |
| Commercial readiness | Offers multiple service lines (`multiple_service_lines`) | 3 | Site mentions at least two of: siding, gutters, windows, commercial roofing, solar, skylights, insulation, painting. |  |
| Commercial readiness | States licensed / insured (`licensed_insured`) | 3 | Site states licensed, insured or bonded, or shows a license number; or a manual state license lookup. |  |
| Commercial readiness | In business 5+ years (`established_years`) | 3 | Site states a founding year ('since 2009', 'established 2004') at least five years ago, or 'N years in business' with N >= 5. No statement = unknown. |  |
| Commercial readiness | Links to social profiles (`active_social_profiles`) | 3 | Website links to at least one business profile on Facebook, Instagram, YouTube, LinkedIn, TikTok, Nextdoor or X. |  |
<!-- rubric:end -->

> **About this rubric.** Your request mentioned a scoring rubric with specific signals and maximum
> weights, but the rubric itself was not attached. The signals above are a starting rubric that
> fits the 30/50/20 split. To use your own, edit `SIGNALS` and `GROUPS` in
> `prospect_finder/rubric.py`, run `pytest` (it fails if a category no longer adds up to its
> budget), and regenerate this table with
> `python -c "from prospect_finder.rubric import rubric_markdown; print(rubric_markdown())"`.

## What a finding is

Each signal is stored as a separate **finding** for a business:

| Field | Meaning |
|---|---|
| `outcome` | `met` (the condition that earns points is true), `not_met` (checked and false) or `unknown` |
| `evidence_type` | `observed` fact, `inferred` conclusion, `confirmed_failure` (a test was run and failed) or `unknown` |
| `confidence` | 0 to 1, how sure the check is (fixed per check, see the analyzer table below) |
| `source_type` | `website_scan`, `manual`, `csv_import`, `google_places`, `openstreetmap` |
| `source_url`, `observation`, `observed_at` | Required for every `met` or `not_met` finding |
| `test_name`, `test_result` | Also required for automated sources and for every confirmed failure |
| `expires_at` | Set when the source limits how long content may be kept (Google Places) |

The app rejects a determinate finding with incomplete evidence when it is saved. The scoring
engine checks again, so incomplete evidence can never earn points.

## Step by step

1. **Choose one finding per signal.** For each signal the engine sets aside:
   - findings with missing evidence (the signal shows `missing evidence` and needs review),
   - stale findings, meaning older than the freshness limit for their source or past `expires_at`,
   - findings with confidence below **0.5** (the signal shows `low confidence` and needs review).

   Among the rest, a manual check wins over automated sources, then the most recent observation,
   then the higher confidence. Every other finding for that signal is listed as a duplicate or a
   disagreement and **never counted a second time**. If two different automated sources disagree,
   the prospect is flagged for review.
2. **Award points.** A `met` signal earns its full weight. A `not_met` signal earns 0. An unknown
   signal earns 0 and is **excluded from coverage**.
3. **Supersession.** When "No working website" is met, the eight website-dependent problems it
   replaces score 0 and count as resolved. A broken site is one problem, not nine.
4. **Group caps.** Related problems share one budget, filled from the largest signal down:
   - `conversion_path` (lead form 10, tap-to-call 6, call-to-action 5) is capped at **17**.
   - `tracking` (ad conversion tracking 8, analytics 4) is capped at **10**.
5. **Category scores** are summed, capped at the category budget, and added up to the total.

### Evidence coverage

For each category: *coverage = (weight of signals with a usable determination) ÷ (weight of all
signals in the category)*. "Usable" means counted, not met, or superseded. Overall coverage
weights each category by its budget:

```
coverage = (fit_cov × 30 + opportunity_cov × 50 + readiness_cov × 20) / 100
```

So "coverage 72%" means there is evidence for signals worth about 72 of the 100 points.

### Confidence

The weighted average confidence of the findings that were used (met or not met), weighted by
each signal's maximum points.

### Upper bound

The score the prospect would reach if every unknown signal turned out to be met, capped per
category. It tells you how much a prospect could still move once research is done.

### Freshness limits

| Source | Stale after |
|---|---:|
| `website_scan` | 90 days |
| `manual` | 180 days |
| `csv_import`, `openstreetmap` | 365 days |
| `google_places` | 30 days, or earlier at its `expires_at` |

## Priority tiers and manual review

| Tier | Total score |
|---|---:|
| A - Priority | ≥ 60 |
| B - Good | ≥ 40 |
| C - Nurture | ≥ 20 |
| D - Low | < 20 |

The tier changes to **Needs manual review** (the score-based tier is still shown as "provisional")
when any of these are true:

- overall evidence coverage is below 60%,
- business-fit coverage is below 50%,
- average confidence is below 0.60,
- any signal has missing evidence, low confidence, only stale evidence, or a conflict.

Two other states override the tier: **Not a fit** when "Roofing is the primary service" is
confirmed `not_met`, and **Do not contact** when the prospect has opted out.

## Next recommended research step

The step is chosen in this order: opted out → not a fit → fix missing evidence → resolve a conflict
→ retry a website check that failed → confirm a site that would not load → re-check stale evidence → verify a low-confidence finding →
run the website scan if it has never run → research the highest-weight unknown signal → manually
confirm the strongest finding before outreach.

## Worked example

A website scan of a small roofer's site produced these results (all `website_scan` findings):

| Signal | Result | Confidence | Points |
|---|---|---:|---:|
| Roofing is the primary service | met (observed) | 0.90 | 10 |
| Serves residential homeowners | met (observed) | 0.85 | 6 |
| Service area is stated | not met | 0.60 | 0 |
| Business appears active | unknown (old copyright year) | — | excluded |
| Reviews or testimonials | not met | 0.55 | 0 |
| No working website | not met (site loaded) | 0.95 | 0 |
| No lead-capture form | met (observed) | 0.65 | 10 |
| No tap-to-call link | met (observed) | 0.85 | 6 |
| No estimate CTA | met (observed) | 0.75 | 5 → **1** (conversion cap 17) |
| Not configured for mobile | met (observed) | 0.90 | 7 |
| No working HTTPS | met (confirmed failure) | 0.85 | 4 |
| No ad conversion tracking | met (inferred) | 0.70 | 8 |
| No analytics | met (inferred) | 0.70 | 4 → **2** (tracking cap 10) |
| No chat/booking tool | met (inferred) | 0.65 | 6 |
| Fewer than 25 Google reviews | unknown (no finding) | — | excluded |
| Financing / certification / multiple services | not met | 0.55–0.60 | 0 |
| States licensed / insured | met (observed) | 0.75 | 3 |
| In business 5+ years | unknown | — | excluded |
| Links to social profiles | met (observed) | 0.85 | 3 |

- Business fit 16 / 30 (coverage 26/30 = 87%)
- Marketing opportunities 44 / 50 (coverage 80/86 = 93%)
- Commercial readiness 6 / 20 (coverage 17/20 = 85%)
- **Total 66**, upper bound 79, evidence coverage 89.5%, confidence 0.79, **Tier A**
- Strongest findings: roofing is the primary service (10), no lead form (10), no mobile viewport (7)
- Next step: look up the Google Business Profile review count

## Website analyzer checks

**A failed request is never evidence.** When the website cannot be read (DNS failure, timeout,
connection error, server error, rate limit, unavailable robots.txt, even a 404), the scanner saves
no findings at all and records the check as **failed**. The prospect's score is not changed by the
failure; earlier findings are kept and go stale on their own. "No working website" can only be
recorded by a person who confirmed it. Before giving up, the fetcher retries timeouts, connection
errors, HTTP 5xx and 429 responses (see [README → Bulk website checks](../README.md#bulk-website-checks)).

The analyzer fetches at most three pages: the homepage, a contact page, and a service-area or
about page. It reads the HTML only and never runs JavaScript. When the homepage has fewer than 250
characters of server-rendered text, "absence" checks report **unknown** instead of a problem,
because the content is probably built by JavaScript.

| Signal | Met when | Not met when | Confidence |
|---|---|---|---|
| `no_working_website` | never set by the scanner: only a manual finding (site confirmed missing or broken in a browser) | homepage loads | not met 0.95 |
| `roofing_core_service` | schema.org `RoofingContractor`; or roofing in title/H1/meta; or 3+ mentions in text | no mention on checked pages | 0.95 / 0.90 / 0.65 inferred; not met 0.75 |
| `residential_services` | residential, homeowner, replacement, storm/hail, insurance terms | none found | 0.85; not met 0.60 |
| `service_area_stated` | schema.org `areaServed`, or "serving <Place>", "service area(s)", "areas we serve" | none found | 0.90 / 0.85; not met 0.60 |
| `active_business` | site loads and © year ≥ last year | (old or missing year → unknown) | 0.75 |
| `social_proof` | review widget or reviews/testimonials text | none found | 0.80; not met 0.55 |
| `no_lead_form` | no form with name/email/phone fields and no known form embed | form found | 0.80 (contact page checked) / 0.65 (no contact page); not met 0.90 |
| `no_click_to_call` | homepage has no `tel:` link | `tel:` link found | 0.85; not met 0.95 |
| `no_clear_cta` | no estimate/inspection/quote/booking wording in homepage links, buttons, headings | found | 0.75; not met 0.85 |
| `missing_mobile_viewport` | no `meta viewport` with `width=device-width` | present | 0.90; not met 0.95 |
| `no_https` | https fails with a certificate/TLS error while http loads, or https redirects to http (a timeout or connection error on https → unknown) | https loads | 0.85 confirmed failure / 0.80; not met 0.95 |
| `no_ad_conversion_tracking` | no Google Ads, Meta, Microsoft, LinkedIn, TikTok or Nextdoor tag (inferred) | tag found | 0.70 (0.55 thin page); not met 0.90; GTM present → unknown |
| `no_analytics` | no analytics or tag-manager script (inferred) | GA4/UA/Clarity/Hotjar/… found, or GTM present (0.60 inferred) | 0.70 (0.55 thin page); not met 0.90 |
| `no_chat_or_booking` | no known chat/text/booking widget (inferred) | widget or `sms:` link found | 0.65; not met 0.85 |
| `financing_offered` | financing / payment plan / lender named | none found | 0.85; not met 0.55 |
| `manufacturer_certification` | certification claim (marked unverified) | none found | 0.70; not met 0.55 |
| `multiple_service_lines` | 2+ of siding, gutters, windows, commercial, solar, skylights, insulation, painting | fewer | 0.80; not met 0.60 |
| `licensed_insured` | licensed/insured/bonded/license # text | none found | 0.75; not met 0.55 |
| `established_years` | founding year ≥ 5 years ago, or "N years in business/experience" with N ≥ 5 (inferred) | year < 5 years ago | 0.75 / 0.60 inferred; no statement → unknown |
| `active_social_profiles` | link to a business profile (not a share link) | none | 0.85; not met 0.60 |

`review_generation_gap` comes from the Google Places API (when caching is allowed) or from a manual
check. The website scan does not produce it.

Not-met results at 0.55 are deliberately weak. One more manual check is usually enough to raise or
reverse them.
