# Data sources, storage and compliance

This is how the app handles each source. It is not legal advice. Terms change, so check each
provider's current terms before you rely on a default, especially before exporting or sharing data.

## Summary

| Source | How it is accessed | Stored | Exported | Notes |
|---|---|---|---|---|
| CSV import | File you upload | Yes | Only if the batch was marked export-permitted | You confirm the provider allows storage before importing |
| Manual research | Typed into the app | Yes | Yes | Every determinate finding needs a source URL and an observation |
| Public website scan | `PoliteFetcher` over HTTP(S) | Yes (facts observed on the business's own site) | Yes | robots.txt, rate limits, no bypassing, see below |
| Google Places API (New) | Official API, API key, field masks | Place IDs; other content only for `GOOGLE_PLACES_CACHE_DAYS` (default **0**: not stored) | **Never** | Shown with Google attribution; no scraping of Google Maps or Search |
| OpenStreetMap | Public Overpass API | Yes | Yes, with attribution | ODbL 1.0, © OpenStreetMap contributors |

Every identity field (name, phone, email, website, address) records its source, URL, timestamp,
import batch, and an expiry when the source requires one. The export reads that record field by
field.

## CSV imports (permitted lists)

Good inputs include:

- **State contractor-license data.** Several states publish licensee lists as public records or
  downloadable files (for example Florida DBPR, or the licensing boards in states that license
  roofers). Check each state's reuse terms.
- **Your own research spreadsheets.**
- **Purchased lists**, only if the licence allows storage (and export, if you need it).

The import form makes you confirm storage is permitted and asks whether export is permitted. If
you do not tick export, those fields are withheld from CSV exports and the export says why.

## Website scanning rules

- **robots.txt (RFC 9309).** The scanner checks robots.txt for every origin and redirect target. A
  missing file (4xx) allows crawling. A server error (5xx) means do not crawl. `Crawl-delay` is
  honoured when it is longer than the configured delay.
- **Rate limit.** One request at a time per host, at least `CRAWLER_MIN_DELAY_SECONDS` apart
  (default 3 s). Bulk scans run one site after another. Each site gets at most
  `CRAWLER_MAX_PAGES_PER_SITE` pages (default 3) plus robots.txt.
- **No bypassing.** HTTP 401/403/407/429, redirects to login pages, and CAPTCHA or bot-challenge
  pages stop the scan. The checks they block are recorded as **unknown** with the reason. After a
  429 the scanner makes no more requests to that host in the run. It never submits forms, logs
  in, solves CAPTCHAs, rotates IPs or spoofs a browser.
- **Identification.** Requests send `User-Agent: RoofingProspectFinder/1.0 (+manual business
  research; contact: $CRAWLER_CONTACT)`. Set `CRAWLER_CONTACT` to an email or URL where site
  owners can reach you.
- **Limits.** HTML only, 2 MB per page, no JavaScript. Pages built entirely in JavaScript produce
  "unknown" results rather than false problems.

## Google Business Profile data

Google Business Profile information is used only through Google's official, paid
**Places API (New)**. The app never scrapes Google Maps or Search.

- **Storage.** Google's terms allow place IDs to be stored. Other Places content may only be
  cached in limited ways. The app therefore stores place IDs permanently and writes other Places
  content (name, address, phone, website, business status, review count) to the database only
  when `GOOGLE_PLACES_CACHE_DAYS` is above 0. That content gets an expiry and is deleted by
  `purge-expired`, which also runs at startup and before every export. The default is **0**, so
  only place IDs are stored. Set a higher value only if your reading of the current
  [Google Maps Platform Service Specific Terms](https://cloud.google.com/maps-platform/terms/maps-service-terms)
  allows it.
- **With the default of 0:**
  1. Search results are shown live with Google attribution, and importing keeps only the place ID.
  2. "Scan website" makes one live Place Details call to get the website URL for that scan. The
     URL is stored only after the business's own site confirms it, and then as a `website_scan`
     fact.
  3. Places-derived findings (operational status, review count) are not stored, so "Fewer than 25
     Google reviews" stays unknown unless a researcher records it as a manual finding.
- **Export and audits.** Google content is never included in CSV exports. Google-sourced findings
  are left out of prospect-facing audits.
- **Costs.** See the README section on API costs. The search uses the Text Search **Pro** field
  set. Website, phone and review count are requested only in Place Details for one prospect at a
  time.
- **Business Profile API.** Google's Business Profile APIs only work for profiles you manage. They
  become a permitted source after a prospect becomes a client and grants you access, not before.

## OpenStreetMap

The import queries `craft=roofer` features inside a US state through the public Overpass API, one
query per request, with the app's User-Agent. If the server returns 429 or 504, the app tells you
to wait. OSM data may be stored and exported under the ODbL. Exports include an `attribution`
column. If you publish a database derived from OSM, the ODbL share-alike terms apply.

## What the app never stores or invents

The app has no field for revenue, ad spend, lead volume, response times or search ranking. It
never estimates them, and audits cannot mention them. The optional Claude rewrite is rejected if
it adds any number, money, percentage or one of those terms.

## Outreach compliance

- **Opt-outs are permanent.** Opting out records the date and adds the phone, website domain,
  email and source IDs to a suppression list that outlives the prospect record. Re-imports that
  match the list are skipped. Opted-out prospects cannot be set to "contacted" or "ready", get no
  audit, and are left out of the prospect export. Download the suppression list from the
  Duplicates page or with `flask export --suppression`.
- **Email (US CAN-SPAM Act).** Identify yourself, include a valid physical postal address, give a
  clear way to opt out, and honour opt-outs within 10 business days. The audit template includes
  an opt-out sentence. Add your signature and address with `AUDIT_SIGNATURE`.
- **Calls and texts (US TCPA, state laws, National Do Not Call Registry).** Don't use autodialed
  or prerecorded calls, or texts to mobile numbers, without the consent the law requires. Check
  numbers against the registry and your own suppression list.
