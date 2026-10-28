"""Normalization and duplicate matching for prospects."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .models import Business

# Hosts shared by many businesses: the path identifies the business, not the host.
PLATFORM_HOSTS = {
    "facebook.com", "m.facebook.com", "instagram.com", "yelp.com", "google.com", "g.page",
    "maps.google.com", "linktr.ee", "nextdoor.com", "angi.com", "homeadvisor.com", "houzz.com",
    "bbb.org", "linkedin.com", "youtube.com", "x.com", "twitter.com", "tiktok.com",
}
LEGAL_SUFFIXES = {"llc", "inc", "co", "corp", "corporation", "company", "ltd", "pllc", "lp", "llp", "the"}


def phone_key(phone: str | None) -> str | None:
    """Ten-digit US number, or None if it does not look like one."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def format_phone(phone: str | None) -> str | None:
    key = phone_key(phone)
    if not key:
        return phone.strip() if phone else None
    return f"({key[:3]}) {key[3:6]}-{key[6:]}"


def normalize_url(url: str | None) -> str | None:
    if not url or not url.strip():
        return None
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    parsed = urlparse(url)
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    return url


def domain_key(url: str | None) -> str | None:
    url = normalize_url(url)
    if not url:
        return None
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port and port not in (80, 443):
        host = f"{host}:{port}"
    if host in PLATFORM_HOSTS:
        segment = next((p for p in parsed.path.split("/") if p), "")
        return f"{host}/{segment.lower()}" if segment else None
    return host


def name_key(name: str | None) -> str | None:
    if not name:
        return None
    text = name.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    words = [w for w in text.split() if w not in LEGAL_SUFFIXES]
    return " ".join(words) or None


def keys_for(b: Business) -> dict[str, str | None]:
    return {
        "name_key": name_key(b.name),
        "phone_key": phone_key(b.phone),
        "domain_key": domain_key(b.website),
    }


def match_reason(a: Business, b: Business) -> str | None:
    """Why two records are the same business, or None.

    Strong identifiers (place ID, OSM ID, website domain, phone) are enough on
    their own. A matching name also needs the same ZIP, or the same city and state.
    """
    if a.google_place_id and a.google_place_id == b.google_place_id:
        return "same Google place ID"
    if a.osm_id and a.osm_id == b.osm_id:
        return "same OpenStreetMap ID"
    ka, kb = keys_for(a), keys_for(b)
    if ka["domain_key"] and ka["domain_key"] == kb["domain_key"]:
        return f"same website ({ka['domain_key']})"
    if ka["phone_key"] and ka["phone_key"] == kb["phone_key"]:
        return f"same phone ({format_phone(a.phone)})"
    if ka["name_key"] and ka["name_key"] == kb["name_key"]:
        if a.postal_code and a.postal_code[:5] == (b.postal_code or "")[:5]:
            return "same name and ZIP"
        if a.city and b.city and a.state and b.state and a.city.lower() == b.city.lower() \
                and a.state.upper() == b.state.upper():
            return "same name, city and state"
    return None


def possible_duplicate_reason(a: Business, b: Business) -> str | None:
    """Weaker match shown for manual review but never merged automatically."""
    ka, kb = keys_for(a), keys_for(b)
    if ka["name_key"] and ka["name_key"] == kb["name_key"]:
        if not (a.city and b.city) or (a.state and b.state and a.state.upper() == b.state.upper()):
            return "same name; location missing or different city"
    return None
