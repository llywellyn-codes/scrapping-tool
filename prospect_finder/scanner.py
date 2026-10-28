"""Run the website analyzer for a stored prospect and record the results."""

from __future__ import annotations

from .analyzer.fetcher import PoliteFetcher
from .analyzer.website import ScanResult, analyze_website
from .config import Config
from .models import Business, utcnow
from .repository import Provenance, Repository
from .sources import WEBSITE_SCAN


def make_fetcher(config: Config) -> PoliteFetcher:
    return PoliteFetcher(config.user_agent, min_delay=config.crawler_min_delay_seconds,
                         timeout=config.crawler_timeout_seconds)


def scan_business(repo: Repository, b: Business, fetcher: PoliteFetcher, config: Config,
                  google_client=None) -> ScanResult:
    started = utcnow()
    website = b.website
    if not website and b.google_place_id and google_client is not None:
        # Live lookup; the URL is used for this scan and only stored once the site itself confirms it.
        details = google_client.place_details(b.google_place_id)
        website = details.get("website")
        google_client.record_findings(repo, b, details)
    if not website:
        repo.log_scan(b.id, started, "no_website", "no website URL on record")
        return ScanResult("no_website", "no website URL on record")

    result = analyze_website(website, fetcher, now=started, max_pages=config.crawler_max_pages_per_site)
    repo.replace_source_findings(b.id, WEBSITE_SCAN, result.findings)
    if result.status == "ok" and result.identity:
        repo.set_fields(b, result.identity, Provenance(WEBSITE_SCAN, url=result.final_url, at=started))
    repo.touch_checked(b, started)
    repo.log_scan(b.id, started, result.status, result.detail)
    return result

