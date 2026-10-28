"""Glue between storage and the scoring engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .checks import check_state
from .models import Business, utcnow
from .repository import Repository
from .scoring import ScoreCard, score_findings


@dataclass
class Prospect:
    business: Business
    card: ScoreCard
    outreach: dict
    check: dict | None = None
    check_state: str = "unevaluated"


def score_business(repo: Repository, b: Business, now: datetime | None = None,
                   findings=None, outreach: dict | None = None, check: dict | None | bool = False) -> Prospect:
    now = now or utcnow()
    outreach = outreach if outreach is not None else repo.get_outreach(b.id)
    findings = findings if findings is not None else repo.findings_for(b.id)
    check = repo.website_check(b.id) if check is False else check
    state = check_state(check, now)
    has_website = True if b.website else (None if b.google_place_id else False)
    card = score_findings(findings, now=now, opted_out=bool(outreach.get("opted_out")),
                          has_website=has_website,
                          website_check_failed=(check or {}).get("detail") if state == "failed" else None)
    return Prospect(b, card, outreach, check, state)


def all_prospects(repo: Repository, now: datetime | None = None) -> list[Prospect]:
    now = now or utcnow()
    findings = repo.all_findings()
    outreach = repo.all_outreach()
    checks = repo.all_website_checks()
    return [score_business(repo, b, now, findings.get(b.id, []), outreach.get(b.id, {}), checks.get(b.id))
            for b in repo.list_businesses()]
