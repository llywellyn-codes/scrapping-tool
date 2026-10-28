from datetime import timedelta
from pathlib import Path

import pytest

from prospect_finder import dedupe
from prospect_finder.export import export_prospects_csv, export_suppression_csv
from prospect_finder.importers.csv_import import import_csv
from prospect_finder.models import Business, Outcome, utcnow
from prospect_finder.repository import EvidenceError, Provenance
from prospect_finder.service import all_prospects

from .helpers import finding

SAMPLE = (Path(__file__).parent.parent / "sample_data" / "sample_prospects.csv").read_text()


def csv_import(repo, text=SAMPLE, **kwargs):
    kwargs.setdefault("source_name", "test list")
    kwargs.setdefault("storage_permitted", True)
    return import_csv(repo, text, **kwargs)


# ---------------------------------------------------------------- normalization


@pytest.mark.parametrize("raw, key", [
    ("(214) 555-0142", "2145550142"), ("+1 214.555.0142", "2145550142"), ("555-0142", None), (None, None),
])
def test_phone_key(raw, key):
    assert dedupe.phone_key(raw) == key


@pytest.mark.parametrize("raw, key", [
    ("summitpeakroofing.example", "summitpeakroofing.example"),
    ("https://WWW.SummitPeakRoofing.example/contact", "summitpeakroofing.example"),
    ("https://facebook.com/summitpeak", "facebook.com/summitpeak"),
    ("https://www.facebook.com/", None),
    ("not a url", None),
    ("http://127.0.0.1:8771/", "127.0.0.1:8771"),
    ("https://acme.example:443/", "acme.example"),
])
def test_domain_key(raw, key):
    assert dedupe.domain_key(raw) == key


def test_name_key_drops_legal_suffixes_and_punctuation():
    assert dedupe.name_key("Summit Peak Roofing, LLC") == dedupe.name_key("summit peak roofing")
    assert dedupe.name_key("A & B Roofing Co.") == "a and b roofing"


def test_name_match_requires_location():
    a = Business(name="Acme Roofing", city="Dallas", state="TX")
    assert dedupe.match_reason(a, Business(name="ACME Roofing LLC", city="dallas", state="tx"))
    assert dedupe.match_reason(a, Business(name="Acme Roofing", city="Austin", state="TX")) is None
    assert dedupe.match_reason(a, Business(name="Acme Roofing")) is None
    assert dedupe.possible_duplicate_reason(a, Business(name="Acme Roofing"))


# ---------------------------------------------------------------- import and deduplication


def test_csv_import_dedupes_and_rejects_non_us_rows(repo):
    report = csv_import(repo)
    assert report.counts == {"created": 3, "merged": 1, "suppressed": 0, "rejected": 1}
    assert "not a US state" in next(r.reason for r in report.rows if r.action == "rejected")
    merged = next(r for r in report.rows if r.action == "merged")
    assert merged.reason.startswith("same website")
    summit = repo.get_business(merged.business_id)
    assert summit.phone == "(214) 555-0142"
    assert summit.field_sources["phone"]["source"] == "csv_import"
    assert summit.field_sources["name"]["url"] == "https://www.example.gov/license/12345"


def test_reimport_merges_instead_of_duplicating(repo):
    csv_import(repo)
    report = csv_import(repo)
    assert report.counts["created"] == 0
    assert len(repo.list_businesses()) == 3


def test_import_requires_storage_permission(repo):
    with pytest.raises(PermissionError):
        csv_import(repo, storage_permitted=False)


def test_import_requires_identifying_column(repo):
    with pytest.raises(ValueError):
        csv_import(repo, text="city,state\nDallas,TX\n")


def test_merge_combines_records_findings_and_outreach(repo):
    a = repo.create_business({"name": "Acme Roofing", "city": "Dallas", "state": "TX"}, Provenance("manual"))
    b = repo.create_business({"name": "Acme Roofing Co", "phone": "2145550100"}, Provenance("manual"))
    repo.add_finding(b.id, finding("no_lead_form"))
    repo.update_outreach(b.id, status="contacted", last_contact_date="2026-09-01", response="call back in May")
    repo.merge(a.id, b.id)
    kept = repo.get_business(a.id)
    assert kept.phone == "(214) 555-0100"
    assert repo.get_business(b.id) is None
    assert [f.signal_key for f in repo.findings_for(a.id)] == ["no_lead_form"]
    outreach = repo.get_outreach(a.id)
    assert outreach["status"] == "contacted"
    assert outreach["last_contact_date"] == "2026-09-01"
    assert outreach["response"] == "call back in May"


# ---------------------------------------------------------------- findings storage


def test_finding_without_source_url_is_rejected_at_write_time(repo):
    b = repo.create_business({"name": "Acme Roofing"}, Provenance("manual"))
    with pytest.raises(EvidenceError, match="missing source URL"):
        repo.add_finding(b.id, finding("no_lead_form", url=None))


def test_unknown_finding_needs_no_evidence(repo):
    b = repo.create_business({"name": "Acme Roofing"}, Provenance("manual"))
    repo.add_finding(b.id, finding("no_lead_form", Outcome.UNKNOWN, url=None, observation=None))
    assert repo.findings_for(b.id)[0].outcome == Outcome.UNKNOWN


def test_rescan_replaces_determinate_results_but_blocked_rescan_keeps_them(repo):
    b = repo.create_business({"name": "Acme Roofing"}, Provenance("manual"))
    repo.replace_source_findings(b.id, "website_scan", [finding("no_lead_form")])
    repo.replace_source_findings(b.id, "website_scan", [finding("no_lead_form", Outcome.NOT_MET)])
    assert [f.outcome for f in repo.findings_for(b.id)] == [Outcome.NOT_MET]
    repo.replace_source_findings(b.id, "website_scan", [finding("no_lead_form", Outcome.UNKNOWN)])
    assert sorted(f.outcome.value for f in repo.findings_for(b.id)) == ["not_met", "unknown"]


def test_google_content_is_not_stored_when_cache_days_is_zero(repo):
    b = repo.upsert({"name": "Acme Roofing", "phone": "2145550100"}, Provenance("google_places"),
                    google_place_id="ChIJabc")
    stored = repo.get_business(b.business_id)
    assert stored.google_place_id == "ChIJabc"
    assert stored.name is None and stored.phone is None
    assert repo.add_finding(stored.id, finding("review_generation_gap", source="google_places")) is None
    assert repo.findings_for(stored.id) == []


def test_google_content_expires_and_is_purged(repo):
    repo.google_cache_days = 30
    result = repo.upsert({"name": "Acme Roofing"}, Provenance("google_places"), google_place_id="ChIJabc")
    b = repo.get_business(result.business_id)
    assert b.name == "Acme Roofing"
    assert b.field_sources["name"]["expires_at"]
    repo.add_finding(b.id, finding("review_generation_gap", source="google_places", age_days=0))
    counts = repo.purge_expired(now=utcnow() + timedelta(days=31))
    assert counts == {"findings": 1, "fields": 1}
    b = repo.get_business(b.id)
    assert b.name is None and b.google_place_id == "ChIJabc"


def test_permanent_source_replaces_expiring_google_value(repo):
    repo.google_cache_days = 30
    result = repo.upsert({"phone": "2145550100"}, Provenance("google_places"), google_place_id="ChIJabc")
    b = repo.get_business(result.business_id)
    repo.set_fields(b, {"phone": "2145550100"}, Provenance("website_scan", url="https://acme.test/"))
    assert b.field_sources["phone"]["source"] == "website_scan"
    assert b.field_sources["phone"]["expires_at"] is None


# ---------------------------------------------------------------- outreach and opt-out


def test_opt_out_is_sticky_across_reimport(repo):
    csv_import(repo)
    summit = next(b for b in repo.list_businesses() if b.name.startswith("Summit"))
    repo.update_outreach(summit.id, status="contacted", last_contact_date="2026-09-15", response="not now")
    repo.opt_out(summit.id)
    with pytest.raises(ValueError, match="opted out"):
        repo.update_outreach(summit.id, status="contacted")
    # Deleting the record and re-importing must not bring them back.
    repo.conn.execute("DELETE FROM businesses WHERE id = ?", (summit.id,))
    report = csv_import(repo)
    assert report.counts["suppressed"] == 2
    assert not any(b.name and b.name.startswith("Summit") for b in repo.list_businesses())


def test_invalid_outreach_values_are_rejected(repo):
    b = repo.create_business({"name": "Acme Roofing"}, Provenance("manual"))
    with pytest.raises(ValueError):
        repo.update_outreach(b.id, status="spammed")
    with pytest.raises(ValueError):
        repo.update_outreach(b.id, last_contact_date="09/01/2026")


# ---------------------------------------------------------------- export


def _rows(text):
    import csv
    import io
    return list(csv.DictReader(io.StringIO(text)))


def test_export_withholds_fields_from_non_exportable_batches(repo):
    csv_import(repo, export_permitted=False)
    rows = _rows(export_prospects_csv(repo, all_prospects(repo)))
    assert rows and all(r["name"] == "" and r["phone"] == "" for r in rows)
    assert "import batch not marked export-permitted" in rows[0]["withheld_fields"]


def test_export_includes_permitted_fields_and_scores(repo):
    csv_import(repo, export_permitted=True)
    summit = next(b for b in repo.list_businesses() if b.name.startswith("Summit"))
    repo.add_finding(summit.id, finding("no_lead_form", url="https://summitpeakroofing.example/contact",
                                        observation="No form on homepage or /contact", age_days=0))
    rows = {r["name"]: r for r in _rows(export_prospects_csv(repo, all_prospects(repo)))}
    row = rows["Summit Peak Roofing LLC"]
    assert row["phone"] == "(214) 555-0142"
    assert row["marketing_opportunity"] == "10.0"
    assert "No form on homepage" in row["finding_1"]
    assert row["needs_review"] == "yes"


def test_export_excludes_opted_out_and_google_content(repo):
    repo.google_cache_days = 30
    csv_import(repo, export_permitted=True)
    g = repo.upsert({"name": "Google Only Roofing", "phone": "3055550111"}, Provenance("google_places"),
                    google_place_id="ChIJxyz")
    summit = next(b for b in repo.list_businesses() if b.name and b.name.startswith("Summit"))
    repo.opt_out(summit.id)
    rows = _rows(export_prospects_csv(repo, all_prospects(repo)))
    assert all(r["prospect_id"] != str(summit.id) for r in rows)
    google_row = next(r for r in rows if r["prospect_id"] == str(g.business_id))
    assert google_row["name"] == "" and google_row["phone"] == ""
    assert "Google Places API terms do not allow export" in google_row["withheld_fields"]
    suppression = _rows(export_suppression_csv(repo))
    assert {"phone", "domain"} <= {r["kind"] for r in suppression}


def test_export_neutralizes_formula_injection(repo):
    repo.create_business({"name": "=HYPERLINK(\"http://evil\")"}, Provenance("manual"))
    text = export_prospects_csv(repo, all_prospects(repo))
    assert "'=HYPERLINK" in text
