"""CSV export limited to fields we may store and pass on.

A field is exported only when its recorded source allows export (and, for CSV
imports, when the batch was marked export-permitted). Google Places content is
never exported. Opted-out prospects are left out of the prospect export and
listed only in the separate suppression export.
"""

from __future__ import annotations

import csv
import io

from .models import format_ts
from .repository import IDENTITY_FIELDS, Repository
from .rubric import BUSINESS_FIT, COMMERCIAL_READINESS, MARKETING_OPPORTUNITY
from .scoring import TIER_LABELS
from .service import Prospect
from .sources import CSV_IMPORT, policies

COLUMNS = [
    "prospect_id", "name", "phone", "email", "website", "street", "city", "state", "postal_code",
    "field_sources", "withheld_fields", "attribution",
    "total_score", "business_fit", "marketing_opportunity", "commercial_readiness", "score_upper_bound",
    "evidence_coverage", "confidence", "priority_tier", "needs_review", "review_reasons",
    "finding_1", "finding_2", "finding_3", "source_links", "next_step",
    "outreach_status", "last_contact_date", "response", "last_checked_at",
]


def _safe(value) -> str:
    """Neutralize spreadsheet formula injection."""
    text = "" if value is None else str(value)
    if text and text[0] in "=+-@\t\r":
        return "'" + text
    return text


def field_export_status(prov: dict | None, batches: dict[int, dict], google_cache_days: int) -> str | None:
    """None if exportable, else the reason it is withheld."""
    if not prov:
        return "no recorded source"
    policy = policies(google_cache_days).get(prov.get("source"))
    if policy is None:
        return "unrecognized source"
    if not policy.exportable:
        return f"{policy.label} terms do not allow export"
    if prov.get("source") == CSV_IMPORT:
        batch = batches.get(prov.get("batch_id"))
        if not batch or not batch["export_permitted"]:
            return "import batch not marked export-permitted"
    return None


def _finding_exportable(result, google_cache_days: int) -> bool:
    policy = policies(google_cache_days).get(result.finding.source_type) if result.finding else None
    return bool(policy and policy.exportable)


def prospect_row(p: Prospect, batches: dict[int, dict], google_cache_days: int) -> dict:
    b, card = p.business, p.card
    row: dict = {"prospect_id": b.id}
    sources, withheld, attributions = [], [], set()
    all_policies = policies(google_cache_days)
    for name in IDENTITY_FIELDS:
        value = getattr(b, name)
        if value is None:
            row[name] = ""
            continue
        prov = b.field_sources.get(name)
        reason = field_export_status(prov, batches, google_cache_days)
        if reason:
            row[name] = ""
            withheld.append(f"{name} ({reason})")
        else:
            row[name] = value
            sources.append(f"{name}:{prov['source']}")
            if all_policies[prov["source"]].attribution:
                attributions.add(all_policies[prov["source"]].attribution)
    exportable_top = [r for r in card.top_findings if _finding_exportable(r, google_cache_days)]
    for i in range(3):
        if i < len(exportable_top):
            r = exportable_top[i]
            row[f"finding_{i + 1}"] = f"{r.signal.label} ({r.points:g} pts): {r.finding.observation} " \
                                      f"[{r.finding.source_url}]"
        else:
            row[f"finding_{i + 1}"] = ""
    links = [r.finding.source_url for r in card.results
             if r.finding and r.finding.source_url and r.covered and _finding_exportable(r, google_cache_days)]
    row.update({
        "field_sources": "; ".join(sources),
        "withheld_fields": "; ".join(withheld),
        "attribution": "; ".join(sorted(attributions)),
        "total_score": card.total,
        "business_fit": card.categories[BUSINESS_FIT].score,
        "marketing_opportunity": card.categories[MARKETING_OPPORTUNITY].score,
        "commercial_readiness": card.categories[COMMERCIAL_READINESS].score,
        "score_upper_bound": card.upper_bound,
        "evidence_coverage": f"{card.coverage:.0%}",
        "confidence": f"{card.confidence:.2f}",
        "priority_tier": TIER_LABELS[card.tier],
        "needs_review": "yes" if card.needs_review else "no",
        "review_reasons": "; ".join(card.review_reasons),
        "source_links": " ".join(dict.fromkeys(links)),
        "next_step": card.next_step,
        "outreach_status": p.outreach.get("status", ""),
        "last_contact_date": p.outreach.get("last_contact_date") or "",
        "response": p.outreach.get("response") or "",
        "last_checked_at": format_ts(b.last_checked_at) or "",
    })
    return row


def export_prospects_csv(repo: Repository, prospects: list[Prospect]) -> str:
    repo.purge_expired()
    batches = repo.batches()
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS)
    writer.writeheader()
    for p in prospects:
        if p.outreach.get("opted_out"):
            continue
        row = prospect_row(p, batches, repo.google_cache_days)
        writer.writerow({k: _safe(row.get(k)) for k in COLUMNS})
    return out.getvalue()


def export_suppression_csv(repo: Repository) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["kind", "value", "reason", "created_at"])
    writer.writeheader()
    for entry in repo.suppression_list():
        writer.writerow({"kind": entry["kind"], "value": _safe(entry["value_key"]),
                         "reason": _safe(entry["reason"]), "created_at": entry["created_at"]})
    return out.getvalue()
