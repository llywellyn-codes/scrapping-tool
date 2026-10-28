import json

from prospect_finder.models import Business, EvidenceType, Outcome
from prospect_finder.output import export_prospects_json, prospect_record
from prospect_finder.repository import Provenance
from prospect_finder.scoring import score_findings
from prospect_finder.service import Prospect, all_prospects

from .helpers import NOW, all_signals, finding

# The record format supplied for exports (values are placeholders).
SAMPLE = {
    "business": {"business_id": "internal_unique_id", "name": "Example Roofing Co.", "website": "https://example.com",
                 "phone": "+1XXXXXXXXXX", "city": "Dallas", "state": "TX", "niche_match": "confirmed"},
    "scoring": {"business_fit": 24, "marketing_opportunity": 35, "commercial_readiness": 14, "raw_score": 73,
                "evidence_coverage": 0.82, "priority": "B", "review_status": "needs_manual_verification"},
    "findings": [
        {"signal": "primary_conversion_cta", "points_awarded": 4, "max_points": 7, "status": "observed",
         "finding_type": "observed_fact", "confidence": 0.94, "evidence_url": "https://example.com",
         "evidence": "No quote or inspection CTA was visible in the reviewed homepage navigation or first screen.",
         "checked_at": "2026-10-28T00:00:00Z"},
        {"signal": "lead_follow_up_speed", "points_awarded": 0, "max_points": 3, "status": "unknown",
         "finding_type": "not_verified", "confidence": None, "evidence_url": None, "evidence": None,
         "checked_at": None},
    ],
}

BIZ = Business(id=7, name="Summit Peak Roofing", website="https://summitpeak.example/", phone="(214) 555-0142",
               city="Dallas", state="TX",
               field_sources={k: {"source": "manual"} for k in ("name", "website", "phone", "city", "state")})


def record(findings, outreach=None, business=BIZ, batches=None, google_days=0):
    card = score_findings(findings, now=NOW, opted_out=bool((outreach or {}).get("opted_out")))
    return prospect_record(Prospect(business, card, outreach or {"status": "new"}), batches or {}, google_days)


def by_signal(rec):
    return {f["signal"]: f for f in rec["findings"]}


def test_record_has_the_sample_keys_in_the_sample_order():
    rec = record(all_signals())
    for section in ("business", "scoring"):
        assert list(rec[section])[:len(SAMPLE[section])] == list(SAMPLE[section])
    sample_finding_keys = list(SAMPLE["findings"][0])
    for f in rec["findings"]:
        assert list(f)[:len(sample_finding_keys)] == sample_finding_keys
    json.dumps(rec)  # serializable


def test_scoring_block_values():
    rec = record(all_signals())
    s = rec["scoring"]
    assert (s["business_fit"], s["marketing_opportunity"], s["commercial_readiness"], s["raw_score"]) == (30, 50, 20, 100)
    assert s["raw_score"] == s["business_fit"] + s["marketing_opportunity"] + s["commercial_readiness"]
    assert s["evidence_coverage"] == 1.0 and s["priority"] == "A" and s["review_status"] == "evidence_sufficient"
    assert rec["business"]["phone"] == "+12145550142" and rec["business"]["business_id"] == "7"
    assert rec["business"]["niche_match"] == "confirmed"
    assert len(rec["top_findings"]) == 3 and rec["next_step"]


def test_unknown_signal_is_not_verified_with_null_evidence():
    rec = record([finding("roofing_core_service")])
    unknown = by_signal(rec)["review_generation_gap"]
    assert unknown == {**unknown, "points_awarded": 0, "max_points": 6, "status": "unknown",
                       "finding_type": "not_verified", "confidence": None, "evidence_url": None, "evidence": None,
                       "checked_at": None}
    s = rec["scoring"]
    assert s["review_status"] == "needs_manual_verification" and s["priority"] == "D" and s["raw_score"] == 10


def test_observed_finding_and_partial_points_from_a_group_cap():
    rec = record([finding("no_lead_form"), finding("no_click_to_call"),
                  finding("no_clear_cta", confidence=0.94, observation="No estimate CTA on homepage")])
    cta = by_signal(rec)["no_clear_cta"]
    assert (cta["points_awarded"], cta["max_points"]) == (1, 5)
    assert cta["status"] == "observed" and cta["finding_type"] == "observed_fact" and cta["confidence"] == 0.94
    assert cta["evidence"] == "No estimate CTA on homepage" and cta["evidence_url"].startswith("https://")
    assert cta["checked_at"].endswith("Z") and cta["outcome"] == "met"
    assert "cap of 17" in cta["note"]


def test_inference_confirmed_failure_superseded_and_stale_states():
    rec = record([finding("no_working_website", evidence=EvidenceType.CONFIRMED_FAILURE),
                  finding("no_analytics", evidence=EvidenceType.INFERRED),
                  finding("financing_offered", age_days=500, source="csv_import")])
    f = by_signal(rec)
    assert f["no_working_website"]["finding_type"] == "confirmed_failure"
    assert f["no_analytics"]["status"] == "superseded" and f["no_analytics"]["points_awarded"] == 0
    assert f["financing_offered"]["status"] == "unknown" and f["financing_offered"]["note"].startswith("stale")
    rec = record([finding("no_analytics", evidence=EvidenceType.INFERRED, confidence=0.7)])
    assert by_signal(rec)["no_analytics"]["finding_type"] == "inference"


def test_review_status_for_opt_out_and_non_roofer():
    assert record(all_signals(), outreach={"opted_out": 1})["scoring"]["review_status"] == "opted_out"
    rec = record(all_signals({"roofing_core_service": Outcome.NOT_MET}))
    assert rec["scoring"]["review_status"] == "not_a_fit"
    assert rec["business"]["niche_match"] == "not_matched"
    assert record([])["business"]["niche_match"] == "unverified"


def test_restricted_sources_are_withheld():
    google_finding = finding("review_generation_gap", source="google_places", url="https://maps.google.com/?cid=1")
    b = Business(id=8, name="Google Roofing", phone="2145550100",
                 field_sources={"name": {"source": "google_places"}, "phone": {"source": "csv_import", "batch_id": 3}})
    rec = record([google_finding], business=b, batches={3: {"export_permitted": 0}}, google_days=30)
    assert rec["business"]["name"] is None and rec["business"]["phone"] is None
    assert len(rec["business"]["withheld_fields"]) == 2
    gap = by_signal(rec)["review_generation_gap"]
    assert gap["points_awarded"] == 6 and gap["evidence"] is None and gap["evidence_url"] is None
    assert "not exported" in gap["note"]
    assert "https://maps.google.com/?cid=1" not in rec["source_links"]


def test_bulk_json_export_skips_opted_out(repo):
    a = repo.create_business({"name": "Keep Roofing", "phone": "2145550100"}, Provenance("manual"))
    b = repo.create_business({"name": "Stop Roofing", "phone": "2145550101"}, Provenance("manual"))
    repo.opt_out(b.id)
    data = json.loads(export_prospects_json(repo, all_prospects(repo)))
    assert data["count"] == 1 and data["prospects"][0]["business"]["business_id"] == str(a.id)
