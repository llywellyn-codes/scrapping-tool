from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prospect_finder.models import EvidenceType, Finding, Outcome
from prospect_finder.rubric import SIGNALS

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def finding(
    key: str,
    outcome: Outcome | str = Outcome.MET,
    *,
    confidence: float = 0.9,
    source: str = "website_scan",
    evidence: EvidenceType | str | None = None,
    age_days: float = 1,
    url: str | None = "https://example-roofing.test/",
    observation: str | None = "observed on homepage",
    test_name: str | None = "html-check",
    test_result: str | None = "result recorded",
    expires_at: datetime | None = None,
) -> Finding:
    outcome = Outcome(outcome)
    if evidence is None:
        evidence = EvidenceType.UNKNOWN if outcome == Outcome.UNKNOWN else EvidenceType.OBSERVED
    return Finding(
        signal_key=key,
        outcome=outcome,
        evidence_type=evidence,
        confidence=confidence,
        source_type=source,
        observed_at=NOW - timedelta(days=age_days),
        source_url=url,
        observation=observation,
        test_name=test_name,
        test_result=test_result,
        expires_at=expires_at,
    )


def all_signals(outcome_overrides: dict[str, Outcome | str] | None = None, **kwargs) -> list[Finding]:
    """One finding per signal: website works, every other signal met unless overridden."""
    overrides = {"no_working_website": Outcome.NOT_MET, **(outcome_overrides or {})}
    return [finding(s.key, overrides.get(s.key, Outcome.MET), **kwargs) for s in SIGNALS]
