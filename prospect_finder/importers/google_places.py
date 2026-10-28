"""Google Places API (New) client, used within Google Maps Platform terms.

- Uses only the official API with an API key (no scraping of Google Maps or Search).
- Requests the smallest field mask needed, which also sets the billing SKU.
- Place IDs may be stored indefinitely. Other content is written to the database
  only when GOOGLE_PLACES_CACHE_DAYS > 0, with an expiry, and is never exported.
- Search results are shown with Google attribution and are not persisted until
  the user imports selected places.
"""

from __future__ import annotations

import re

import requests

from ..models import EvidenceType, Finding, Outcome, utcnow
from ..repository import Provenance, Repository
from ..sources import GOOGLE_PLACES

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
# Text Search Pro SKU: identity and address only (no phone/website/ratings).
SEARCH_FIELDS = ("places.id,places.displayName,places.formattedAddress,places.primaryType,places.businessStatus,"
                 "places.googleMapsUri,nextPageToken")
# Place Details Enterprise SKU: adds website, phone and review count, fetched only when scanning one prospect.
DETAILS_FIELDS = ("id,displayName,formattedAddress,primaryType,businessStatus,googleMapsUri,websiteUri,"
                  "nationalPhoneNumber,userRatingCount")
REVIEW_GAP_THRESHOLD = 25


class GooglePlacesError(RuntimeError):
    pass


def parse_us_address(formatted: str | None) -> dict[str, str]:
    """'1200 Elm St, Dallas, TX 75201, USA' -> street, city, state, postal_code."""
    if not formatted:
        return {}
    parts = [p.strip() for p in formatted.split(",")]
    if parts and parts[-1] in ("USA", "United States"):
        parts = parts[:-1]
    out: dict[str, str] = {}
    if parts:
        m = re.fullmatch(r"([A-Z]{2})(?:\s+(\d{5})(?:-\d{4})?)?", parts[-1])
        if m:
            out["state"] = m.group(1)
            if m.group(2):
                out["postal_code"] = m.group(2)
            parts = parts[:-1]
    if parts:
        out["city"] = parts[-1]
        if len(parts) > 1:
            out["street"] = ", ".join(parts[:-1])
    return out


class GooglePlacesClient:
    def __init__(self, api_key: str, session: requests.Session | None = None, timeout: float = 15.0):
        if not api_key:
            raise GooglePlacesError("GOOGLE_PLACES_API_KEY is not set.")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout

    def _call(self, method: str, url: str, fields: str, json_body: dict | None = None) -> dict:
        headers = {"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": fields, "Content-Type": "application/json"}
        try:
            resp = self.session.request(method, url, headers=headers, json=json_body, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            raise GooglePlacesError(f"Could not reach the Places API: {exc}") from exc
        if resp.status_code == 429:
            raise GooglePlacesError("Places API quota or rate limit reached; try again later.")
        if resp.status_code >= 400:
            try:
                message = resp.json().get("error", {}).get("message", "")
            except ValueError:
                message = resp.text[:200]
            raise GooglePlacesError(f"Places API error {resp.status_code}: {message}")
        return resp.json()

    @staticmethod
    def _normalize(place: dict) -> dict:
        address = place.get("formattedAddress")
        return {
            "place_id": place.get("id"),
            "name": (place.get("displayName") or {}).get("text"),
            "address": address,
            **parse_us_address(address),
            "primary_type": place.get("primaryType"),
            "business_status": place.get("businessStatus"),
            "maps_uri": place.get("googleMapsUri"),
            "website": place.get("websiteUri"),
            "phone": place.get("nationalPhoneNumber"),
            "rating_count": place.get("userRatingCount"),
        }

    def text_search(self, query: str, page_token: str | None = None) -> tuple[list[dict], str | None]:
        body = {"textQuery": query, "includedType": "roofing_contractor", "regionCode": "US", "pageSize": 20}
        if page_token:
            body["pageToken"] = page_token
        data = self._call("POST", SEARCH_URL, SEARCH_FIELDS, body)
        return [self._normalize(p) for p in data.get("places", [])], data.get("nextPageToken")

    def place_details(self, place_id: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", place_id or ""):
            raise GooglePlacesError("invalid place ID")
        return self._normalize(self._call("GET", DETAILS_URL.format(place_id=place_id), DETAILS_FIELDS))

    def record_findings(self, repo: Repository, b, details: dict) -> int:
        """Store Places-derived findings; the repository drops them when caching is disabled."""
        return sum(1 for f in findings_from_details(details) if repo.add_finding(b.id, f, created_by=GOOGLE_PLACES))


def findings_from_details(details: dict) -> list[Finding]:
    now = utcnow()
    url = details.get("maps_uri") or f"https://www.google.com/maps/place/?q=place_id:{details.get('place_id')}"
    out = []

    def add(key, outcome, conf, observation, result):
        out.append(Finding(signal_key=key, outcome=outcome, evidence_type=EvidenceType.OBSERVED, confidence=conf,
                           source_type=GOOGLE_PLACES, observed_at=now, source_url=url, observation=observation,
                           test_name="Places API place details", test_result=result))

    status = details.get("business_status")
    if status == "OPERATIONAL":
        add("active_business", Outcome.MET, 0.85, "Google Places lists the business as operational.", status)
    elif status in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"):
        add("active_business", Outcome.NOT_MET, 0.85, f"Google Places lists the business as {status.lower()}.", status)
    if details.get("primary_type") == "roofing_contractor":
        add("roofing_core_service", Outcome.MET, 0.85, "Google Places primary type is roofing_contractor.",
            "primaryType=roofing_contractor")
    count = details.get("rating_count")
    if isinstance(count, int):
        met = count < REVIEW_GAP_THRESHOLD
        add("review_generation_gap", Outcome.MET if met else Outcome.NOT_MET, 0.9,
            f"Google Places shows {'fewer than' if met else 'at least'} {REVIEW_GAP_THRESHOLD} reviews.",
            f"userRatingCount={count}")
    return out


def import_places(repo: Repository, places: list[dict], query: str) -> dict[str, int]:
    """Import user-selected search results. Only the place ID is kept unless caching is enabled."""
    batch_id = repo.create_batch(
        GOOGLE_PLACES, f"Google Places text search: {query}", source_url="https://places.googleapis.com/",
        terms_note=f"Place IDs stored; other content cached {repo.google_cache_days} day(s); never exported.",
        export_permitted=False)
    counts = {"created": 0, "merged": 0, "suppressed": 0, "rejected": 0}
    for p in places:
        fields = {k: p.get(k) for k in ("name", "street", "city", "state", "postal_code")}
        prov = Provenance(GOOGLE_PLACES, url=p.get("maps_uri"), batch_id=batch_id)
        counts[repo.upsert(fields, prov, google_place_id=p.get("place_id")).action] += 1
    repo.finish_batch(batch_id, counts)
    return counts
