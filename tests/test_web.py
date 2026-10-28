import re
from pathlib import Path

import pytest

from prospect_finder import create_app
from prospect_finder.config import Config

SAMPLE = (Path(__file__).parent.parent / "sample_data" / "sample_prospects.csv").read_bytes()


@pytest.fixture
def client(tmp_path):
    app = create_app(Config(database_path=str(tmp_path / "test.db"), secret_key="test"))
    app.testing = True
    with app.test_client() as c:
        yield c


def token(client):
    html = client.get("/").get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', html).group(1) if 'csrf_token' in html else \
        _token_from_import(client)


def _token_from_import(client):
    html = client.get("/import").get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', html).group(1)


def upload(client, export_permitted=True):
    data = {"csrf_token": _token_from_import(client), "source_name": "sample", "storage_permitted": "1",
            "file": (__import__("io").BytesIO(SAMPLE), "sample.csv")}
    if export_permitted:
        data["export_permitted"] = "1"
    return client.post("/import", data=data, content_type="multipart/form-data")


def test_post_without_csrf_token_is_rejected(client):
    assert client.post("/prospects/new", data={"name": "X"}).status_code == 400


def test_import_list_detail_and_export_flow(client):
    resp = upload(client)
    assert resp.status_code == 200
    assert "3 new, 1 merged" in resp.get_data(as_text=True)
    listing = client.get("/").get_data(as_text=True)
    assert "Summit Peak Roofing LLC" in listing and "Needs manual review" in listing
    detail = client.get("/prospects/1").get_data(as_text=True)
    assert "Next recommended manual research step" in detail
    assert "Strongest verified findings" in detail
    csv_text = client.get("/export.csv").get_data(as_text=True)
    assert csv_text.startswith("prospect_id,name,phone")
    assert "Summit Peak Roofing LLC" in csv_text


def test_manual_finding_requires_evidence_and_changes_score(client):
    upload(client)
    t = _token_from_import(client)
    bad = client.post("/prospects/1/findings", data={
        "csrf_token": t, "signal_key": "no_lead_form", "outcome": "met", "evidence_type": "observed",
        "confidence": "0.9", "observation": "No form on contact page"}, follow_redirects=True)
    assert "missing source URL" in bad.get_data(as_text=True)
    good = client.post("/prospects/1/findings", data={
        "csrf_token": t, "signal_key": "no_lead_form", "outcome": "met", "evidence_type": "observed",
        "confidence": "0.9", "observation": "Contact page lists a phone number only",
        "source_url": "https://summitpeakroofing.example/contact"}, follow_redirects=True)
    page = good.get_data(as_text=True)
    assert "Finding recorded" in page
    assert "+10" in page  # appears among the strongest verified findings


def test_opt_out_blocks_contact_and_audit(client):
    upload(client)
    t = _token_from_import(client)
    client.post("/prospects/1/opt-out", data={"csrf_token": t})
    page = client.post("/prospects/1/outreach", data={"csrf_token": t, "status": "contacted"},
                       follow_redirects=True).get_data(as_text=True)
    assert "opted out and cannot be contacted" in page
    audit = client.get("/prospects/1/audit", follow_redirects=True).get_data(as_text=True)
    assert "opted out" in audit
    assert "Summit Peak" not in client.get("/export.csv").get_data(as_text=True)
    assert "2145550142" in client.get("/export/suppression.csv").get_data(as_text=True)


def test_audit_page_uses_verified_findings_only(client):
    upload(client)
    t = _token_from_import(client)
    client.post("/prospects/1/findings", data={
        "csrf_token": t, "signal_key": "no_click_to_call", "outcome": "met", "evidence_type": "observed",
        "confidence": "0.9", "observation": "Phone number is plain text", "source_url": "https://summitpeakroofing.example/"})
    page = client.get("/prospects/1/audit").get_data(as_text=True)
    assert "isn&#39;t a tap-to-call link" in page or "isn't a tap-to-call link" in page
    assert "Draft only" in page  # coverage is still low


def test_rubric_and_duplicates_pages_render(client):
    assert "Marketing opportunities" in client.get("/rubric").get_data(as_text=True)
    assert client.get("/duplicates").status_code == 200


def test_json_endpoints(client):
    upload(client)
    rec = client.get("/prospects/1.json").get_json()
    assert rec["business"]["business_id"] == "1" and rec["business"]["phone"] == "+12145550142"
    assert rec["scoring"]["review_status"] == "needs_manual_verification"
    assert {f["signal"] for f in rec["findings"]} >= {"no_lead_form", "roofing_core_service"}
    data = client.get("/export.json").get_json()
    assert data["count"] == 3
