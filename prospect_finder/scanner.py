"""Run the website analyzer for a stored prospect and record the results."""

from __future__ import annotations

from .analyzer.fetcher import HostThrottle, PoliteFetcher
from .analyzer.website import ScanResult, analyze_website
from .config import Config
from .models import Business, utcnow
from .repository import Provenance, Repository
from .sources import WEBSITE_SCAN


def make_fetcher(config: Config, throttle: HostThrottle | None = None) -> PoliteFetcher:
    return PoliteFetcher(config.user_agent, min_delay=config.crawler_min_delay_seconds,
                         timeout=config.crawler_timeout_seconds, max_retries=config.check_max_retries,
                         throttle=throttle)


def scan_business(repo: Repository, b: Business, fetcher: PoliteFetcher, config: Config,
                  google_client=None) -> ScanResult:
    """Check one prospect's website, save the findings and the check status, and return the result.

    Used by the single "Check website" action and by bulk runs. A failed request saves no findings:
    it is recorded as a failed check (older findings are kept and go stale on their own).
    """
    started = utcnow()
    website = b.website
    if not website and b.google_place_id and google_client is not None:
        # Live lookup; the URL is used for this scan and only stored once the site itself confirms it.
        details = google_client.place_details(b.google_place_id)
        website = details.get("website")
        google_client.record_findings(repo, b, details)
    if not website:
        repo.record_website_check(b.id, "no_website", started, "no website URL on record")
        repo.log_scan(b.id, started, "no_website", "no website URL on record")
        return ScanResult("no_website", "no website URL on record")

    fetcher.start_site(config.check_site_time_limit)
    result = analyze_website(website, fetcher, now=started, max_pages=config.crawler_max_pages_per_site)
    if result.status in ("ok", "blocked"):
        repo.replace_source_findings(b.id, WEBSITE_SCAN, result.findings)
        if result.status == "ok" and result.identity:
            repo.set_fields(b, result.identity, Provenance(WEBSITE_SCAN, url=result.final_url, at=started))
        repo.touch_checked(b, started)
    repo.record_website_check(b.id, result.status, started, result.detail)
    repo.log_scan(b.id, started, result.status, result.detail)
    return result
