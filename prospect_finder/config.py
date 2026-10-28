"""Settings loaded from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


@dataclass
class Config:
    database_path: str = "prospects.db"
    secret_key: str = "dev-only-change-me"
    # Google Places API (New). Optional; leave blank to disable.
    google_places_api_key: str = ""
    # Days that Google Places content other than place IDs may be kept. 0 = never
    # written to the database (place IDs only). Set this from your own reading of
    # the current Google Maps Platform terms.
    google_places_cache_days: int = 0
    # Identification sent with every website and Overpass request.
    crawler_contact: str = ""
    crawler_min_delay_seconds: float = 3.0
    crawler_timeout_seconds: float = 15.0
    crawler_max_pages_per_site: int = 3
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    # Optional: rewrite audits with Claude (facts are still produced by the app).
    anthropic_api_key: str = ""
    audit_llm_model: str = "claude-opus-5-5"
    audit_signature: str = ""
    # Bulk website checks.
    check_batch_size: int = 10
    check_concurrency: int = 3            # sites checked in parallel (capped at 5)
    check_max_retries: int = 2            # extra attempts per request for timeouts, 5xx and 429
    check_site_time_limit: float = 120.0  # seconds per prospect before the check is marked failed
    check_failed_retry_hours: float = 24.0
    check_max_failed_attempts: int = 3

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            database_path=os.environ.get("DATABASE_PATH", cls.database_path),
            secret_key=os.environ.get("SECRET_KEY", cls.secret_key),
            google_places_api_key=os.environ.get("GOOGLE_PLACES_API_KEY", ""),
            google_places_cache_days=_int("GOOGLE_PLACES_CACHE_DAYS", 0),
            crawler_contact=os.environ.get("CRAWLER_CONTACT", ""),
            crawler_min_delay_seconds=_float("CRAWLER_MIN_DELAY_SECONDS", 3.0),
            crawler_timeout_seconds=_float("CRAWLER_TIMEOUT_SECONDS", 15.0),
            crawler_max_pages_per_site=_int("CRAWLER_MAX_PAGES_PER_SITE", 3),
            overpass_url=os.environ.get("OVERPASS_URL", cls.overpass_url),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            audit_llm_model=os.environ.get("AUDIT_LLM_MODEL", cls.audit_llm_model),
            audit_signature=os.environ.get("AUDIT_SIGNATURE", ""),
            check_batch_size=_int("CHECK_BATCH_SIZE", 10),
            check_concurrency=_int("CHECK_CONCURRENCY", 3),
            check_max_retries=_int("CHECK_MAX_RETRIES", 2),
            check_site_time_limit=_float("CHECK_SITE_TIME_LIMIT_SECONDS", 120.0),
            check_failed_retry_hours=_float("CHECK_FAILED_RETRY_HOURS", 24.0),
            check_max_failed_attempts=_int("CHECK_MAX_FAILED_ATTEMPTS", 3),
        )

    @property
    def user_agent(self) -> str:
        contact = f"; contact: {self.crawler_contact}" if self.crawler_contact else ""
        return f"RoofingProspectFinder/1.0 (+manual business research{contact})"
