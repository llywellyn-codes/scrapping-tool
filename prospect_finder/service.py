"""Glue between storage and the scoring engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import Business, utcnow
from .repository import Repository
from .scoring import ScoreCard, score_findings


@dataclass
class Prospect:
    business: Business
    card: ScoreCard
    outreach: dict


def score_business(repo: Repository, b: Business, now: datetime | None = None,
                   findings=None, outreach: dict | None = None) -> Prospect:
    outreach = outreach if outreach is not None else repo.get_outreach(b.id)
    findings = findings if findings is not None else repo.findings_for(b.id)
    has_website = True if b.website else (None if b.google_place_id else False)
    card = score_findings(findings, now=now or utcnow(), opted_out=bool(outreach.get("opted_out")),
                          has_website=has_website)
    return Prospect(b, card, outreach)


def all_prospects(repo: Repository, now: datetime | None = None) -> list[Prospect]:
    now = now or utcnow()
    findings = repo.all_findings()
    outreach = repo.all_outreach()
    return [score_business(repo, b, now, findings.get(b.id, []), outreach.get(b.id, {}))
            for b in repo.list_businesses()]
