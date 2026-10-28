# Roofing SMMA Prospect Finder

A small local web app (with a CLI) for finding US roofing contractors who could use help with
paid ads, website conversion, lead capture or follow-up automation. Every prospect gets a
transparent, evidence-based score.

- **The app calculates the score, not a language model.** A 100-point rubric (Business fit 30,
  Marketing opportunities 50, Commercial readiness 20) is applied to stored findings. Each signal
  is a separate finding with an outcome, an evidence type (observed, inferred, confirmed failure
  or unknown), a 0–1 confidence, and its evidence: source URL, observation, timestamp, and test
  result where one applies.
- **Unknowns are never guessed.** They score 0 and lower **evidence coverage** instead. Stale,
  low-confidence and evidence-less findings never earn points, and they send the prospect to
  **manual review**.
- **No double counting.** Duplicate findings count once. Related problems share a capped budget,
  and a dead website supersedes the website problems it causes.
- **Permitted sources only.** CSV lists you have rights to, the business's own public website
  (robots.txt and rate limits respected, nothing bypassed), the official Google Places API within
  its caching terms, and OpenStreetMap.

Each prospect shows its total score, category scores, evidence coverage, confidence, priority
tier, three strongest verified findings, source links, and the next recommended manual research
step.

Further reading: [docs/SCORING.md](docs/SCORING.md) explains how every point is calculated, with a
worked example. [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) covers storage, export, scanning
rules and outreach compliance.

> **Your rubric.** Your brief mentioned a rubric with specific signals and weights, but it was
> not attached. The app ships with a starting rubric that fits the 30/50/20 split
> (`prospect_finder/rubric.py`). Swapping in your own signals and weights means editing that
> one file. The tests check that each category still adds up to its budget.

## Setup

Requires Python 3.10+.

```bash
git clone https://github.com/llywellyn-codes/scrapping-tool.git
```

```bash
cd scrapping-tool
```

```bash
python3 -m venv .venv && source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Edit `.env` and set at least `SECRET_KEY` and `CRAWLER_CONTACT`. Then create the database and
start the app:

```bash
flask --app prospect_finder init-db
```

```bash
flask --app prospect_finder run
```

Open http://127.0.0.1:5000. The app has no login, so keep it bound to localhost. If you deploy it
anywhere else, put it behind authentication.

To run the tests:

```bash
pip install -r requirements-dev.txt && pytest
```

## Workflow

1. **Import.** On the Import page: upload a CSV (try `sample_data/sample_prospects.csv`), search
   Google Places, import OpenStreetMap roofers for a state, or add one prospect by hand. Each
   import records the source and its terms. Non-US rows are rejected, duplicates are merged, and
   opted-out businesses are skipped.
2. **Scan.** Click **Scan website** on a prospect, or run `flask --app prospect_finder scan --all`.
   The scan fetches up to three public pages, waiting a few seconds between requests (about 10–15 s
   per site), and records about 20 findings with evidence.
3. **Review.** The prospect page shows the score card, any review reasons, the next research step
   and every signal with its evidence and the findings that were set aside. Record what you check
   by hand as **manual findings** (source URL and observation required). A manual check beats an
   automated one.
4. **Deduplicate.** The Duplicates page lists likely duplicates that were not merged automatically.
5. **Audit.** **Audit draft** writes a short email from verified marketing-opportunity findings
   only, with an opt-out line. If `ANTHROPIC_API_KEY` is set you can optionally smooth the wording
   with Claude. The rewrite is thrown away if it adds any number or claim.
6. **Outreach.** Track status, last contact date and response. **Mark opted out** is permanent and
   feeds the suppression list.
7. **Export.** **Export CSV** includes only fields whose source allows export. Withheld fields are
   named with the reason. Opted-out prospects are excluded, and the suppression list is a separate
   download.

### CLI

```bash
flask --app prospect_finder import-csv sample_data/sample_prospects.csv --source-name "Sample list" --storage-permitted --export-permitted
```

```bash
flask --app prospect_finder osm-import TX --city Dallas
```

```bash
flask --app prospect_finder scan --all --stale-only
```

```bash
flask --app prospect_finder report
```

```bash
flask --app prospect_finder export prospects.csv
```

```bash
flask --app prospect_finder export suppression.csv --suppression
```

```bash
flask --app prospect_finder purge-expired
```

## Configuration

All settings come from environment variables (or `.env`). Credentials are never stored in code
or in the database.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_PATH` | `prospects.db` | SQLite file |
| `SECRET_KEY` | random per run | Flask session / CSRF signing key; set a fixed value |
| `CRAWLER_CONTACT` | — | Email or URL added to the scanner's User-Agent so site owners can reach you |
| `CRAWLER_MIN_DELAY_SECONDS` | `3` | Minimum gap between requests to the same host |
| `CRAWLER_TIMEOUT_SECONDS` | `15` | Per-request timeout |
| `CRAWLER_MAX_PAGES_PER_SITE` | `3` | Homepage + contact + one service-area/about page |
| `GOOGLE_PLACES_API_KEY` | — | Enables Google Places search and the place-ID website lookup |
| `GOOGLE_PLACES_CACHE_DAYS` | `0` | Days Places content other than place IDs may be stored (0 = place IDs only) |
| `OVERPASS_URL` | public Overpass | OpenStreetMap query endpoint |
| `ANTHROPIC_API_KEY` | — | Enables the optional audit rewrite |
| `AUDIT_LLM_MODEL` | `claude-opus-5-5` | Model for the optional rewrite |
| `AUDIT_SIGNATURE` | — | Your name, company and postal address for audit emails (CAN-SPAM) |

## API costs

| Service | What uses it | Cost |
|---|---|---|
| Website scanning | Scan website / `scan` | Free (your bandwidth). About 3–4 requests per site |
| OpenStreetMap Overpass | OSM import | Free public service; keep usage modest |
| Google Places **Text Search Pro** | Each Google search page (up to 20 results) | First 5,000 calls/month free, then about **$32 per 1,000** |
| Google Places **Place Details Enterprise** | Scanning a Google-imported prospect with no stored website (one call per scan) | First 1,000 calls/month free, then about **$20 per 1,000** |
| Claude API (optional) | "Smooth wording with Claude" on an audit | `claude-opus-5-5`: $4 / $20 per million input/output tokens, roughly $0.01–0.02 per audit. `claude-haiku-5-5` ($0.10 / $0.50) is far cheaper if you set `AUDIT_LLM_MODEL` |

Google bills by the most expensive field you request, so the app keeps search on the Pro field
set. Website, phone and review count are requested only in Place Details for one prospect at a
time. Example: 200 searches (up to 4,000 businesses) stay inside the free 5,000 Text Search calls.
Scanning 4,000 Google-only prospects needs 4,000 Place Details calls, which is about $60 after the
free 1,000. Prices change and were checked in October 2026; confirm them on Google's
[pricing page](https://developers.google.com/maps/billing-and-pricing/pricing) and Anthropic's
pricing page before budgeting. Set budget alerts and API quotas in the Google Cloud console.

The Claude rewrite sends only the audit text, which is built from findings already shown in the
app. It uses low effort and the server-side refusal fallback (`fallbacks: "default"`). If the
request is declined, fails, or the result adds anything, the template text is kept.

## Project layout

```
prospect_finder/
  rubric.py          signals, weights, group caps, tier policy (single source of truth)
  scoring.py         deterministic scoring engine (no network, no LLM)
  models.py          Finding / Business data types
  sources.py         per-source storage, retention and export policy
  repository.py      SQLite storage, provenance, dedupe-on-import, opt-out suppression
  dedupe.py          phone / domain / name normalization and matching
  analyzer/          polite fetcher (robots, rate limits, no bypass) and website checks
  importers/         CSV, Google Places (New), OpenStreetMap Overpass
  audit.py           template audit + optional guarded Claude rewrite
  export.py          export of permitted fields only
  web.py, cli.py     Flask UI and commands
tests/               scoring, unknowns, stale evidence, duplicates, missing sources, analyzer, web
```

## Limitations

- The scanner reads HTML and does not run JavaScript. Pages built entirely in JavaScript give
  "unknown" rather than findings. Confirm those by hand.
- Absence checks (no form, no tracking tag) only cover the pages fetched. Tags loaded server-side
  or after consent are invisible, which is why those findings are marked **inferred**.
- Certification and licence statements are the business's own claims until you verify them in the
  manufacturer's or state's directory. Record that check as a manual finding.
- OpenStreetMap coverage of US roofers is sparse. Plan on state license data or Google Places as
  your main discovery source.
