"""Per-prospect JSON records: business, scoring and one finding per rubric signal.

The keys follow the record format agreed for exports. A few extra keys
(confidence, review reasons, top findings, source links, next step, outcome,
test name/result, note) carry the rest of the required per-prospect output.
The same export rules as the CSV apply: fields and evidence from sources that
do not allow export are set to null and listed in ``withheld_fields`` / ``note``.
"""

from __future__ import annotations

import json
from datetime import datetime

from .dedupe import phone_key
from .export import field_export_status
from .models import EvidenceType, utcnow
from .repository import Repository
from .rubric import BUSINESS_FIT, COMMERCIAL_READINESS, MARKETING_OPPORTUNITY
from .scoring import SignalResult, SignalState
from .service import Prospect
from .sources import policies

FINDING_TYPES = {
    EvidenceType.OBSERVED: "observed_fact",
    EvidenceType.INFERRED: "inference",
    EvidenceType.CONFIRMED_FAILURE: "confirmed_failure",
}
NICHE_MATCH = {SignalState.COUNTED: "confirmed", SignalState.NOT_MET: "not_matched"}


def _num(x: float) -> int | float:
    return int(x) if float(x).is_integer() else round(x, 1)


def _ts(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _e164(phone: str | None) -> str | None:
    key = phone_key(phone)
    return f"+1{key}" if key else phone


def review_status(p: Prospect) -> str:
    if p.card.tier == "do_not_contact":
        return "opted_out"
    if p.card.tier == "not_a_fit":
        return "not_a_fit"
    return "needs_manual_verification" if p.card.needs_review else "evidence_sufficient"


def finding_record(r: SignalResult, google_cache_days: int = 0) -> dict:
    record = {
        "signal": r.signal.key,
        "points_awarded": _num(r.points),
        "max_points": _num(r.signal.max_points),
        "status": "unknown",
        "finding_type": "not_verified",
        "confidence": None,
        "evidence_url": None,
        "evidence": None,
        "checked_at": None,
        "outcome": None,
        "test_name": None,
        "test_result": None,
        "note": "; ".join(r.notes) or None,
    }
    f = r.finding
    if r.state in (SignalState.COUNTED, SignalState.NOT_MET) and f:
        record.update(status=f.evidence_type.value, finding_type=FINDING_TYPES[f.evidence_type],
                      confidence=round(f.confidence, 2), checked_at=_ts(f.observed_at), outcome=f.outcome.value)
        policy = policies(google_cache_days).get(f.source_type)
        if policy and policy.exportable:
            record.update(evidence_url=f.source_url, evidence=f.observation, test_name=f.test_name,
                          test_result=f.test_result)
        else:
            label = policy.label if policy else f.source_type
            record["note"] = "; ".join(filter(None, [record["note"], f"evidence from {label} is not exported"]))
    elif r.state == SignalState.SUPERSEDED:
        record.update(status="superseded", finding_type="not_applicable")
    else:
        if f is not None:  # an attempted check that could not determine the result
            record["checked_at"] = _ts(f.observed_at)
        if r.state != SignalState.UNKNOWN and r.set_aside:
            record["note"] = f"{r.state.value.replace('_', ' ')}: {r.set_aside[0][1]}"
    return record


def prospect_record(p: Prospect, batches: dict[int, dict], google_cache_days: int = 0) -> dict:
    b, card = p.business, p.card
    withheld, attribution = [], []
    all_policies = policies(google_cache_days)

    def field(name: str):
        value = getattr(b, name)
        if value is None:
            return None
        prov = b.field_sources.get(name)
        reason = field_export_status(prov, batches, google_cache_days)
        if reason:
            withheld.append(f"{name} ({reason})")
            return None
        credit = all_policies[prov["source"]].attribution
        if credit and credit not in attribution:
            attribution.append(credit)
        return value

    business = {
        "business_id": str(b.id),
        "name": field("name"),
        "website": field("website"),
        "phone": _e164(field("phone")),
        "city": field("city"),
        "state": field("state"),
        "niche_match": NICHE_MATCH.get(card.result("roofing_core_service").state, "unverified"),
        "withheld_fields": withheld,
    }
    findings = [finding_record(r, google_cache_days) for r in card.results]
    exported = {fr["signal"]: fr for fr in findings}
    return {
        "business": business,
        "scoring": {
            "business_fit": _num(card.categories[BUSINESS_FIT].score),
            "marketing_opportunity": _num(card.categories[MARKETING_OPPORTUNITY].score),
            "commercial_readiness": _num(card.categories[COMMERCIAL_READINESS].score),
            "raw_score": _num(card.total),
            "evidence_coverage": round(card.coverage, 2),
            "priority": card.provisional_tier,
            "review_status": review_status(p),
            "confidence": round(card.confidence, 2),
            "score_upper_bound": _num(card.upper_bound),
            "review_reasons": card.review_reasons,
        },
        "findings": findings,
        "top_findings": [r.signal.key for r in card.top_findings],
        "source_links": list(dict.fromkeys(
            fr["evidence_url"] for fr in exported.values() if fr["evidence_url"] and fr["outcome"])),
        "next_step": card.next_step,
        "attribution": attribution,
        "outreach": {
            "status": p.outreach.get("status"),
            "last_contact_date": p.outreach.get("last_contact_date"),
            "response": p.outreach.get("response"),
            "opted_out": bool(p.outreach.get("opted_out")),
        },
    }


def export_prospects_json(repo: Repository, prospects: list[Prospect]) -> str:
    """All exportable prospects (opted-out ones are only in the suppression export)."""
    repo.purge_expired()
    batches = repo.batches()
    records = [prospect_record(p, batches, repo.google_cache_days) for p in prospects
               if not p.outreach.get("opted_out")]
    return json.dumps({"generated_at": _ts(utcnow()), "count": len(records), "prospects": records}, indent=2)
