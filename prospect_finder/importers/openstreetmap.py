"""Import roofers tagged in OpenStreetMap via the Overpass API.

OpenStreetMap data is © OpenStreetMap contributors under the ODbL 1.0: it may
be stored and exported with attribution. The public Overpass instance asks for
modest use (no parallel queries, reasonable volume), so this module runs one
query per request and identifies itself with the configured User-Agent.
Coverage of US roofers in OSM is sparse; treat it as one source among several.
"""

from __future__ import annotations

import re

import requests

from ..repository import Provenance, Repository
from ..sources import OPENSTREETMAP
from .csv_import import US_STATES


class OverpassError(RuntimeError):
    pass


def build_query(state: str, limit: int = 500) -> str:
    return (
        "[out:json][timeout:60];\n"
        f'area["ISO3166-2"="US-{state}"]["admin_level"="4"]->.a;\n'
        '(nwr["craft"="roofer"](area.a););\n'
        f"out center tags {int(limit)};"
    )


def search(state: str, *, city: str | None, overpass_url: str, user_agent: str,
           session: requests.Session | None = None, timeout: float = 90.0) -> list[dict]:
    state = (state or "").strip().upper()
    if state not in US_STATES:
        raise OverpassError("Enter a two-letter US state code.")
    session = session or requests.Session()
    try:
        resp = session.post(overpass_url, data={"data": build_query(state)},
                            headers={"User-Agent": user_agent}, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise OverpassError(f"Could not reach the Overpass API: {exc}") from exc
    if resp.status_code in (429, 504):
        raise OverpassError("The Overpass server is busy or rate-limiting; wait a few minutes and retry.")
    if resp.status_code >= 400:
        raise OverpassError(f"Overpass API error {resp.status_code}")
    results = []
    for el in resp.json().get("elements", []):
        tags = el.get("tags", {})
        if not tags.get("name"):
            continue
        record = {
            "osm_id": f"{el['type']}/{el['id']}",
            "name": tags.get("name"),
            "phone": tags.get("phone") or tags.get("contact:phone"),
            "email": tags.get("email") or tags.get("contact:email"),
            "website": tags.get("website") or tags.get("contact:website") or tags.get("url"),
            "street": " ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x) or None,
            "city": tags.get("addr:city"),
            "state": tags.get("addr:state") or state,
            "postal_code": tags.get("addr:postcode"),
        }
        if city and (record["city"] or "").lower() != city.strip().lower():
            continue
        if record["state"] and not re.fullmatch(r"[A-Za-z]{2}", record["state"]):
            record["state"] = state
        results.append(record)
    return results


def import_osm(repo: Repository, records: list[dict], label: str) -> dict[str, int]:
    batch_id = repo.create_batch(OPENSTREETMAP, f"OpenStreetMap craft=roofer: {label}",
                                 source_url="https://www.openstreetmap.org/copyright",
                                 terms_note="ODbL 1.0; attribution required: © OpenStreetMap contributors",
                                 export_permitted=True)
    counts = {"created": 0, "merged": 0, "suppressed": 0, "rejected": 0}
    for r in records:
        fields = {k: r.get(k) for k in ("name", "phone", "email", "website", "street", "city", "state",
                                        "postal_code")}
        prov = Provenance(OPENSTREETMAP, url=f"https://www.openstreetmap.org/{r['osm_id']}", batch_id=batch_id)
        counts[repo.upsert(fields, prov, osm_id=r["osm_id"]).action] += 1
    repo.finish_batch(batch_id, counts)
    return counts
