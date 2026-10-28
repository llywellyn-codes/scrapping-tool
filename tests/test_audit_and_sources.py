from types import SimpleNamespace

import pytest

from prospect_finder.audit import AuditRefused, build_audit, polish_with_claude, rewrite_problems
from prospect_finder.importers import google_places, openstreetmap
from prospect_finder.models import Business, EvidenceType, Outcome
from prospect_finder.scoring import score_findings

from .helpers import NOW, all_signals, finding


def card_with(findings):
    return score_findings(findings, now=NOW)


BIZ = Business(id=1, name="Summit Peak Roofing", website="https://summitpeak.example/")


# ---------------------------------------------------------------- audit


def test_audit_uses_only_counted_opportunity_findings():
    findings = [
        finding("no_lead_form", url="https://summitpeak.example/contact"),
        finding("missing_mobile_viewport"),
        finding("no_click_to_call", Outcome.NOT_MET),            # not a problem
        finding("no_chat_or_booking", confidence=0.3),           # too uncertain to mention
        finding("no_https", url=None),                           # no evidence
        finding("financing_offered"),                            # not an opportunity
    ]
    audit = build_audit(BIZ, card_with(findings))
    keys = [r.signal.key for r in audit.used]
    assert keys == ["no_lead_form", "missing_mobile_viewport"]
    assert "quote or contact form" in audit.body and "summitpeak.example/contact" in audit.body
    assert "tap-to-call" not in audit.body and "chat" not in audit.body and "https" not in audit.body.split("look at")[0]
    assert "won't contact you again" in audit.body
    assert audit.warnings and audit.warnings[0].startswith("Draft only")


def test_audit_never_contains_invented_metrics():
    audit = build_audit(BIZ, card_with(all_signals()))
    assert len(audit.used) == 3
    assert rewrite_problems("", audit.body.replace("won't contact you again", "")) != []  # sanity
    for word in ("revenue", "ad spend", "budget", "leads per", "ranking", "response time", "$", "%"):
        assert word not in audit.body.lower()


def test_audit_refuses_without_verified_findings_or_when_opted_out():
    with pytest.raises(AuditRefused):
        build_audit(BIZ, card_with([finding("no_lead_form", Outcome.UNKNOWN)]))
    with pytest.raises(AuditRefused):
        build_audit(BIZ, card_with(all_signals()), opted_out=True)


def test_audit_excludes_google_places_findings():
    card = card_with([finding("review_generation_gap", source="google_places")])
    with pytest.raises(AuditRefused):
        build_audit(BIZ, card)


def test_inferred_findings_are_flagged_for_verification():
    card = card_with([finding("no_ad_conversion_tracking", evidence=EvidenceType.INFERRED, confidence=0.7)])
    audit = build_audit(BIZ, card)
    assert any("inference" in w for w in audit.warnings)


@pytest.mark.parametrize("addition, problem", [
    ("You are probably losing 30 jobs a month.", "introduced numbers"),
    ("This is likely hurting your revenue.", "revenue"),
    ("Competitors rank above you.", "competitor"),
])
def test_rewrite_validator_rejects_invented_claims(addition, problem):
    original = "We couldn't find a form. If you'd rather not hear from me, reply and I won't contact you again."
    rewritten = original.replace("We couldn't find a form.", "We couldn't find a form. " + addition)
    assert any(problem in p.lower() for p in rewrite_problems(original, rewritten))


class FakeClaude:
    def __init__(self, text, stop_reason="end_turn"):
        response = SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])
        self.calls = []

        def create(**kwargs):
            self.calls.append(kwargs)
            return response
        self.messages = SimpleNamespace(create=create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def test_claude_rewrite_is_accepted_only_when_it_adds_nothing():
    pytest.importorskip("anthropic")
    audit = build_audit(BIZ, card_with([finding("no_lead_form")]))
    good = audit.body.replace("A few things stood out", "Some things stood out")
    fake = FakeClaude(good)
    result = polish_with_claude(audit, "key", "claude-opus-5-5", client=fake)
    assert result.body == good and "rewritten" in result.generated_by
    assert fake.calls[0]["fallbacks"] == "default"
    assert fake.calls[0]["betas"] == ["server-side-fallback-2026-07-01"]

    audit = build_audit(BIZ, card_with([finding("no_lead_form")]))
    original = audit.body
    bad = FakeClaude(original + "\nMost roofers see 40% more leads per month.")
    result = polish_with_claude(audit, "key", "claude-opus-5-5", client=bad)
    assert result.body == original and result.generated_by == "template"
    assert any("rejected" in w for w in result.warnings)


# ---------------------------------------------------------------- Google Places


class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._payload, self.text = status, payload, str(payload)

    def json(self):
        return self._payload


class FakeGoogleSession:
    def __init__(self, payload, status=200):
        self.payload, self.status, self.calls = payload, status, []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, headers, json))
        return FakeResp(self.status, self.payload)


def test_text_search_uses_field_mask_and_us_region():
    session = FakeGoogleSession({"places": [{
        "id": "ChIJ123", "displayName": {"text": "Tulsa Roof Pros"},
        "formattedAddress": "10 Main St, Tulsa, OK 74103, USA", "primaryType": "roofing_contractor",
        "businessStatus": "OPERATIONAL", "googleMapsUri": "https://maps.google.com/?cid=1"}]})
    client = google_places.GooglePlacesClient("key", session=session)
    results, _ = client.text_search("roofing contractor in Tulsa, OK")
    method, url, headers, body = session.calls[0]
    assert headers["X-Goog-FieldMask"] == google_places.SEARCH_FIELDS
    assert "websiteUri" not in headers["X-Goog-FieldMask"]  # keeps search on the cheaper SKU
    assert body["regionCode"] == "US" and body["includedType"] == "roofing_contractor"
    assert results[0] | {} == results[0]
    assert results[0]["city"] == "Tulsa" and results[0]["state"] == "OK" and results[0]["postal_code"] == "74103"


def test_google_errors_are_reported():
    client = google_places.GooglePlacesClient("key", session=FakeGoogleSession({"error": {"message": "bad key"}}, 403))
    with pytest.raises(google_places.GooglePlacesError, match="bad key"):
        client.text_search("roofers")


def test_google_import_stores_place_id_only_by_default(repo):
    places = [{"place_id": "ChIJ123", "name": "Tulsa Roof Pros", "city": "Tulsa", "state": "OK",
               "maps_uri": "https://maps.google.com/?cid=1"}]
    counts = google_places.import_places(repo, places, "roofers tulsa")
    assert counts["created"] == 1
    b = repo.list_businesses()[0]
    assert b.google_place_id == "ChIJ123" and b.name is None and b.city is None
    # Importing the same place again merges by place ID.
    assert google_places.import_places(repo, places, "again")["merged"] == 1


def test_details_findings_are_dropped_unless_caching_enabled(repo):
    details = {"place_id": "ChIJ1", "business_status": "OPERATIONAL", "primary_type": "roofing_contractor",
               "rating_count": 12, "maps_uri": "https://maps.google.com/?cid=1"}
    found = {f.signal_key: f for f in google_places.findings_from_details(details)}
    assert found["review_generation_gap"].outcome == Outcome.MET
    b = repo.create_business({"name": "X"}, __import__("prospect_finder.repository", fromlist=["Provenance"]).Provenance("manual"))
    client = google_places.GooglePlacesClient("key", session=FakeGoogleSession({}))
    assert client.record_findings(repo, b, details) == 0
    repo.google_cache_days = 30
    assert client.record_findings(repo, b, details) == 3
    assert all(f.expires_at is not None for f in repo.findings_for(b.id))


def test_parse_us_address():
    assert google_places.parse_us_address("1200 Elm St, Suite 4, Dallas, TX 75201, USA") == {
        "street": "1200 Elm St, Suite 4", "city": "Dallas", "state": "TX", "postal_code": "75201"}


# ---------------------------------------------------------------- OpenStreetMap


class FakeOverpass:
    def __init__(self, payload, status=200):
        self.payload, self.status, self.calls = payload, status, []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append((url, data, headers))
        return FakeResp(self.status, self.payload)


def test_osm_search_and_import(repo):
    payload = {"elements": [
        {"type": "node", "id": 1, "tags": {"craft": "roofer", "name": "Okie Roofing", "phone": "+1 918 555 0100",
                                            "addr:city": "Tulsa", "website": "okieroofing.example"}},
        {"type": "way", "id": 2, "tags": {"craft": "roofer", "name": "OKC Roof Co", "addr:city": "Oklahoma City"}},
        {"type": "node", "id": 3, "tags": {"craft": "roofer"}},  # unnamed: skipped
    ]}
    session = FakeOverpass(payload)
    records = openstreetmap.search("ok", city="Tulsa", overpass_url="https://overpass.test/api", user_agent="UA",
                                   session=session)
    assert [r["name"] for r in records] == ["Okie Roofing"]
    assert 'area["ISO3166-2"="US-OK"]' in session.calls[0][1]["data"]
    assert session.calls[0][2]["User-Agent"] == "UA"
    counts = openstreetmap.import_osm(repo, records, "Tulsa, OK")
    assert counts["created"] == 1
    b = repo.list_businesses()[0]
    assert b.osm_id == "node/1" and b.phone == "(918) 555-0100"
    assert b.field_sources["name"]["source"] == "openstreetmap"


def test_osm_rejects_non_us_state_and_busy_server():
    with pytest.raises(openstreetmap.OverpassError):
        openstreetmap.search("ON", city=None, overpass_url="x", user_agent="UA", session=FakeOverpass({}))
    with pytest.raises(openstreetmap.OverpassError, match="busy"):
        openstreetmap.search("TX", city=None, overpass_url="x", user_agent="UA", session=FakeOverpass({}, 429))
