"""Plain data types shared by the scoring engine, storage layer and analyzers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_ts(value: str | datetime | None) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive values are treated as UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_ts(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


class Outcome(str, Enum):
    """Whether the condition that earns a signal's points is true."""

    MET = "met"
    NOT_MET = "not_met"
    UNKNOWN = "unknown"


class EvidenceType(str, Enum):
    """How we know what we know.

    observed          - directly seen in a source (e.g. "no tel: link in homepage HTML")
    inferred          - concluded from observed facts with some uncertainty
                        (e.g. "no ad tags in HTML, so conversion tracking is probably absent")
    confirmed_failure - a test was run and failed (e.g. "site did not respond on two attempts")
    unknown           - could not be determined
    """

    OBSERVED = "observed"
    INFERRED = "inferred"
    CONFIRMED_FAILURE = "confirmed_failure"
    UNKNOWN = "unknown"


@dataclass
class Finding:
    """One signal's result for one business, with the evidence behind it."""

    signal_key: str
    outcome: Outcome
    evidence_type: EvidenceType
    confidence: float
    source_type: str
    observed_at: datetime | None
    source_url: str | None = None
    observation: str | None = None
    test_name: str | None = None
    test_result: str | None = None
    expires_at: datetime | None = None
    id: int | None = None
    business_id: int | None = None

    def __post_init__(self) -> None:
        self.outcome = Outcome(self.outcome)
        self.evidence_type = EvidenceType(self.evidence_type)
        self.confidence = float(self.confidence)
        self.observed_at = parse_ts(self.observed_at)
        self.expires_at = parse_ts(self.expires_at)

    def with_(self, **changes) -> "Finding":
        return replace(self, **changes)


@dataclass
class Business:
    """A prospect's identity and contact details.

    ``field_sources`` maps each stored field to its provenance, e.g.
    ``{"phone": {"source": "website_scan", "url": "...", "at": "...", "batch_id": None}}``.
    """

    id: int | None = None
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    street: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    google_place_id: str | None = None
    osm_id: str | None = None
    primary_source: str = "manual"
    field_sources: dict = field(default_factory=dict)
    created_at: datetime | None = None
    last_checked_at: datetime | None = None

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        if self.google_place_id:
            return "Unnamed Google place (details not stored)"
        return f"Prospect #{self.id}"

    @property
    def location(self) -> str:
        return ", ".join(p for p in (self.city, self.state) if p)
