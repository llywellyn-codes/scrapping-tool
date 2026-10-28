"""What each data source allows us to store and export.

Every stored identity field and every finding records its source. Storage,
retention and export decisions are made from this table, so a new source only
needs an entry here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

CSV_IMPORT = "csv_import"
MANUAL = "manual"
WEBSITE_SCAN = "website_scan"
GOOGLE_PLACES = "google_places"
OPENSTREETMAP = "openstreetmap"


@dataclass(frozen=True)
class SourcePolicy:
    key: str
    label: str
    exportable: bool
    # Days content may be kept; None = no limit. 0 = must not be written to disk.
    retention_days: int | None
    attribution: str | None = None
    notes: str = ""


def policies(google_cache_days: int = 0) -> dict[str, SourcePolicy]:
    return {
        p.key: p
        for p in (
            SourcePolicy(
                CSV_IMPORT, "CSV import", exportable=True, retention_days=None,
                notes="Export is allowed only when the import batch was marked export-permitted.",
            ),
            SourcePolicy(MANUAL, "Manual research", exportable=True, retention_days=None),
            SourcePolicy(
                WEBSITE_SCAN, "Public website scan", exportable=True, retention_days=None,
                notes="Facts observed on the business's own public website.",
            ),
            SourcePolicy(
                GOOGLE_PLACES, "Google Places API", exportable=False, retention_days=google_cache_days,
                attribution="Google Maps",
                notes="Place IDs may be stored; other content only for the configured cache days "
                      "and never exported.",
            ),
            SourcePolicy(
                OPENSTREETMAP, "OpenStreetMap (Overpass API)", exportable=True, retention_days=None,
                attribution="© OpenStreetMap contributors, ODbL 1.0",
                notes="Exports must keep the OpenStreetMap attribution; a publicly shared derived "
                      "database must be offered under the ODbL.",
            ),
        )
    }


def expiry_for(source: str, observed_at: datetime, google_cache_days: int = 0) -> datetime | None:
    policy = policies(google_cache_days).get(source)
    if policy is None or policy.retention_days is None:
        return None
    return observed_at + timedelta(days=policy.retention_days)


def may_store(source: str, google_cache_days: int = 0) -> bool:
    policy = policies(google_cache_days).get(source)
    return policy is not None and policy.retention_days != 0
