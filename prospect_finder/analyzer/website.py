"""Turn a business's public website into evidence-backed findings.

The analyzer fetches at most a few pages (homepage, contact page, one
service-area/about page) through ``PoliteFetcher`` and checks objectively
observable things in the HTML. It never runs JavaScript, so when a page has
almost no server-rendered text, "absence" checks (no form, no CTA, ...) are
reported as unknown instead of as problems.

Confidence values used below are fixed per check and documented in
docs/SCORING.md ("Website analyzer checks").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..dedupe import domain_key, normalize_url, phone_key
from ..models import EvidenceType, Finding, Outcome, utcnow
from ..sources import WEBSITE_SCAN
from .fetcher import FetchResult, PoliteFetcher

THIN_TEXT_CHARS = 250

WEBSITE_SIGNALS = (
    "no_working_website", "roofing_core_service", "residential_services", "service_area_stated", "active_business",
    "social_proof", "no_lead_form", "no_click_to_call", "no_clear_cta", "missing_mobile_viewport", "no_https",
    "no_ad_conversion_tracking", "no_analytics", "no_chat_or_booking", "financing_offered",
    "manufacturer_certification", "multiple_service_lines", "licensed_insured", "established_years",
    "active_social_profiles",
)

RESIDENTIAL = re.compile(r"\b(residential|homeowners?|home owners?|roof replacements?|re-?roof(?:ing)?|"
                         r"storm damage|hail damage|insurance claims?)\b", re.I)
# "Serving Dallas", "serving the Greater Tulsa area", "Service areas", "Areas we serve" - but not "serving homeowners".
SERVICE_AREA = re.compile(r"(?i:\bservice areas?\b|\bareas? we serve\b|\bcommunities we serve\b)|"
                          r"\b(?i:proudly )?(?i:serving|we serve|serves) (?:the )?(?:[Gg]reater )?[A-Z][A-Za-z.]+")
ROOFING = re.compile(r"\broof(?:s|ing|er|ers)?\b", re.I)
COPYRIGHT = re.compile(r"(?:©|&copy;|\(c\)|copyright)\s*(?:(?:19|20)\d{2}\s*[-–]\s*)?((?:19|20)\d{2})", re.I)
SOCIAL_PROOF = re.compile(r"\b(testimonials?|customer reviews?|our reviews|google reviews|reviews|"
                          r"what (?:our )?(?:customers|clients|homeowners) (?:say|are saying)|five[- ]star|5[- ]star)\b",
                          re.I)
REVIEW_WIDGETS = re.compile(r"elfsight|trustindex|birdeye\.com|grade\.us|nicejob|embedsocial|reviewsonmywebsite|"
                            r"widgets\.sociablekit|featurable", re.I)
CTA = re.compile(r"free (?:roof(?:ing)? )?(?:estimate|inspection|quote|consultation)|get (?:a |an |your )?(?:free )?"
                 r"(?:quote|estimate|inspection)|request (?:a |an |your )?(?:free )?(?:quote|estimate|inspection|"
                 r"appointment)|schedule (?:a |an |your )?(?:free )?(?:inspection|estimate|appointment|consultation|"
                 r"service)|book (?:now|online|an? (?:inspection|appointment|estimate))|call (?:us )?(?:now|today)|"
                 r"contact us today", re.I)
PHONE_TEXT = re.compile(r"\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")
# Embedded lead forms: iframes from form/lead tools, form-builder scripts, and form-plugin containers.
FORM_IFRAMES = re.compile(r"jotform|typeform|hsforms|hubspot\.com/forms|formstack|cognitoforms|"
                          r"leadconnectorhq\.com/widget/form|msgsndr\.com/widget/form|123formbuilder|formsite|wufoo|"
                          r"forms\.zohopublic|roofr\.com|roofle|getjobber\.com|housecallpro|servicetitan", re.I)
FORM_SCRIPTS = re.compile(r"jotform|typeform|hsforms|formstack|cognitoforms|123formbuilder|wufoo|"
                          r"leadconnectorhq\.com/js/form_embed", re.I)
FORM_CONTAINERS = re.compile(r"\b(wpcf7|gform_wrapper|wpforms-container|hbspt-form|hs-form|typeform-widget)\b", re.I)
AD_TAGS = {
    "Google Ads": re.compile(r"\bAW-\d{6,}|googleadservices\.com/pagead/conversion", re.I),
    "Meta Pixel": re.compile(r"connect\.facebook\.net/[^\"']*/fbevents\.js|fbq\(\s*['\"]init", re.I),
    "Microsoft Ads (UET)": re.compile(r"bat\.bing\.com/bat\.js", re.I),
    "LinkedIn Insight Tag": re.compile(r"snap\.licdn\.com/li\.lms-analytics|_linkedin_partner_id", re.I),
    "TikTok Pixel": re.compile(r"analytics\.tiktok\.com", re.I),
    "Nextdoor Pixel": re.compile(r"ads\.nextdoor\.com", re.I),
}
GTM = re.compile(r"googletagmanager\.com/gtm\.js|\bGTM-[A-Z0-9]{4,}\b")
ANALYTICS = {
    "Google Analytics 4": re.compile(r"gtag/js\?id=G-|['\"]G-[A-Z0-9]{6,}['\"]", re.I),
    "Universal Analytics": re.compile(r"google-analytics\.com/(?:analytics|ga)\.js|['\"]UA-\d+-\d+['\"]", re.I),
    "Microsoft Clarity": re.compile(r"clarity\.ms", re.I),
    "Hotjar": re.compile(r"static\.hotjar\.com", re.I),
    "Plausible": re.compile(r"plausible\.io/js", re.I),
    "Matomo": re.compile(r"matomo\.js|piwik\.js", re.I),
    "Segment": re.compile(r"cdn\.segment\.com", re.I),
    "StatCounter": re.compile(r"statcounter\.com/counter", re.I),
}
CHAT_BOOKING = {
    "Podium": r"podium\.com", "Birdeye": r"birdeye\.com", "Tawk.to": r"tawk\.to", "Intercom": r"intercom(?:cdn)?\.io|intercomcdn",
    "Drift": r"driftt?\.com", "LiveChat": r"livechatinc\.com", "HubSpot chat": r"usemessages\.com",
    "LeadConnector": r"leadconnectorhq\.com|msgsndr\.com", "Tidio": r"tidio\.co", "Zendesk chat": r"zdassets\.com|zopim",
    "Crisp": r"crisp\.chat", "Olark": r"olark\.com", "Smith.ai": r"smith\.ai", "Ngage": r"ngageics",
    "ApexChat": r"apexchat", "Calendly": r"calendly\.com", "Acuity": r"acuityscheduling\.com",
    "Housecall Pro": r"housecallpro\.com", "Jobber": r"getjobber\.com", "ServiceTitan": r"servicetitan\.com",
    "Roofr": r"roofr\.com", "Roofle": r"roofle", "Setmore": r"setmore\.com", "Square Appointments": r"squareup\.com/appointments",
}
CHAT_BOOKING_RE = {k: re.compile(v, re.I) for k, v in CHAT_BOOKING.items()}
FINANCING = re.compile(r"\b(financing|finance options|payment plans?|monthly payments|0% apr|greensky|hearth|"
                       r"service finance|enhancify|synchrony|ygrene)\b", re.I)
CERTIFICATIONS = re.compile(
    r"\b(GAF (?:Master Elite|Certified|President'?s Club)|Owens Corning (?:Preferred|Platinum)|"
    r"CertainTeed (?:SELECT ShingleMaster|ShingleMaster|Master Shingle Applicator)|Malarkey (?:Emerald Pro|Certified)|"
    r"IKO (?:ShieldPRO|ROOFPRO)|Atlas (?:PRO\+?|Pro Plus)|TAMKO (?:Pro Certified|Edge|Certified))", re.I)
SERVICE_LINES = {
    "siding": r"\bsiding\b", "gutters": r"\bgutters?\b", "windows": r"\bwindows\b", "commercial roofing": r"\bcommercial\b",
    "solar": r"\bsolar\b", "skylights": r"\bskylights?\b", "insulation": r"\binsulation\b", "painting": r"\bpainting\b",
}
LICENSED = re.compile(r"\b(licensed|insured|bonded|license\s*(?:#|no\.?|number))", re.I)
FOUNDED = re.compile(r"\b(?:since|established(?: in)?|est\.?|founded(?: in)?)\s*((?:19|20)\d{2})\b", re.I)
YEARS = re.compile(r"\b(\d{1,3})\+?\s*years?\s+(?:of experience|in business|of service|serving)", re.I)
SOCIAL_HOSTS = ("facebook.com", "instagram.com", "youtube.com", "linkedin.com", "tiktok.com", "nextdoor.com",
                "x.com", "twitter.com")
SHARE_PATH = re.compile(r"share|sharer|intent|dialog|plugins", re.I)
LOCAL_TYPES = {"localbusiness", "roofingcontractor", "homeandconstructionbusiness", "generalcontractor",
               "professionalservice", "organization", "corporation", "housepainter", "plumber", "electrician"}


@dataclass
class Page:
    url: str
    html: str
    soup: BeautifulSoup
    text: str
    kind: str  # home | contact | extra

    @classmethod
    def parse(cls, url: str, html: str, kind: str) -> "Page":
        soup = BeautifulSoup(html, "html.parser")
        clean = BeautifulSoup(html, "html.parser")
        for tag in clean(["script", "style", "noscript", "template", "svg"]):
            tag.decompose()
        text = re.sub(r"\s+", " ", clean.get_text(" ", strip=True))
        return cls(url, html, soup, text, kind)


@dataclass
class ScanResult:
    # ok | blocked (access restricted, not retried) | failed (request failed; no evidence) | no_website
    status: str
    detail: str
    findings: list[Finding] = field(default_factory=list)
    identity: dict[str, str] = field(default_factory=dict)
    pages: list[str] = field(default_factory=list)
    final_url: str | None = None
    temporary: bool = False  # for "failed": worth retrying later


def _snippet(text: str, match: re.Match, width: int = 70) -> str:
    start, end = max(0, match.start() - width), min(len(text), match.end() + width)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


class _Builder:
    def __init__(self, now: datetime):
        self.now = now
        self.findings: list[Finding] = []

    def add(self, key, outcome, etype, confidence, url, observation, test_name, test_result):
        self.findings.append(Finding(
            signal_key=key, outcome=outcome, evidence_type=etype, confidence=confidence, source_type=WEBSITE_SCAN,
            observed_at=self.now, source_url=url, observation=observation, test_name=test_name,
            test_result=test_result))

    def unknown(self, key, url, observation, test_name="website scan"):
        self.add(key, Outcome.UNKNOWN, EvidenceType.UNKNOWN, 0.0, url, observation, test_name, "undetermined")


def _https_variant(url: str, scheme: str) -> str:
    p = urlparse(url)
    return f"{scheme}://{p.netloc}{p.path or '/'}" + (f"?{p.query}" if p.query else "")


def analyze_website(url: str | None, fetcher: PoliteFetcher, now: datetime | None = None,
                    max_pages: int = 3) -> ScanResult:
    now = now or utcnow()
    url = normalize_url(url)
    if not url:
        return ScanResult("no_website", "no website URL on record")
    b = _Builder(now)
    https_url, http_url = _https_variant(url, "https"), _https_variant(url, "http")

    attempts: list[str] = []
    r_https = fetcher.get(https_url)
    attempts.append(f"{https_url} -> {r_https.describe()}")
    if r_https.blocked:
        return _blocked(b, r_https, https_url)
    home_res, r_http = (r_https, None) if r_https.ok else (None, None)
    if home_res is None:
        r_http = fetcher.get(http_url)
        attempts.append(f"{http_url} -> {r_http.describe()}")
        if r_http.blocked:
            return _blocked(b, r_http, http_url)
        if r_http.ok:
            home_res = r_http
    if home_res is None:
        # The fetcher has already retried temporary errors. A failed request is not evidence that the
        # business has a bad website, so nothing is recorded as a finding; the check is marked failed.
        tries = (r_https.attempts or 1) + (r_http.attempts or 1 if r_http else 0)
        detail = "; ".join(attempts) + f" ({tries} request(s) in total)"
        temporary = r_https.temporary or bool(r_http and r_http.temporary)
        return ScanResult("failed", detail, temporary=temporary)

    home_url = home_res.final_url or home_res.url
    home = Page.parse(home_url, home_res.text, "home")
    b.add("no_working_website", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.95, home_url,
          f"Homepage loaded from {home_url}.", "homepage load", f"HTTP {home_res.status}")
    _check_https(b, r_https, r_http, https_url, http_url, home_url)

    pages = [home]
    notes = []
    unread_kinds: set[str] = set()
    for target, kind in _extra_pages(home)[: max(0, max_pages - 1)]:
        res = fetcher.get(target)
        if res.ok and res.final_url and domain_key(res.final_url) == domain_key(home_url):
            pages.append(Page.parse(res.final_url, res.text, kind))
        else:
            notes.append(f"{target}: {res.describe()}")
            if not res.ok and res.final_url and domain_key(res.final_url) == domain_key(home_url):
                unread_kinds.add(kind)
    _analyze_pages(b, home, pages, now, unread_kinds)
    identity = extract_identity(home, pages)
    detail = f"checked {len(pages)} page(s): " + ", ".join(p.url for p in pages)
    if notes:
        detail += "; skipped " + "; ".join(notes)
    return ScanResult("ok", detail, b.findings, identity, [p.url for p in pages], home_url)


def _blocked(b: _Builder, res: FetchResult, url: str) -> ScanResult:
    reason = res.blocked
    for key in WEBSITE_SIGNALS:
        b.unknown(key, url, f"Access restricted ({reason}); the scanner does not bypass this. Check manually.",
                  "access check")
    return ScanResult("blocked", f"{url}: {reason}", b.findings)


def _check_https(b: _Builder, r_https, r_http, https_url, http_url, home_url):
    if r_https is not None and r_https.ok and (r_https.final_url or "").startswith("https://"):
        b.add("no_https", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.95, r_https.final_url,
              "Site loads over HTTPS with a certificate the scanner accepted.", "https load",
              f"HTTP {r_https.status} over HTTPS")
    elif r_https is not None and r_https.ok:
        b.add("no_https", Outcome.MET, EvidenceType.OBSERVED, 0.8, https_url,
              f"Requesting {https_url} redirects to an insecure http:// page ({r_https.final_url}).",
              "https load", f"redirected to {r_https.final_url}")
    elif r_http is not None and r_http.ok and (r_http.final_url or "").startswith("https://"):
        b.unknown("no_https", https_url, "HTTPS failed once but the http:// version redirected to HTTPS; re-check.",
                  "https load")
    elif r_http is not None and r_http.ok and r_https is not None and r_https.error_kind == "tls":
        b.add("no_https", Outcome.MET, EvidenceType.CONFIRMED_FAILURE, 0.85, https_url,
              f"The https:// version failed with a certificate/TLS error while the http:// version loaded "
              f"({home_url}).", "https load", f"https: {r_https.describe()}; http: HTTP {r_http.status}")
    elif r_http is not None and r_http.ok:
        # A timeout or connection error on https says nothing certain about the certificate.
        b.unknown("no_https", https_url, f"The https:// request failed ({r_https.describe() if r_https else 'not tried'}) "
                  "but that can be temporary; re-check before treating HTTPS as broken.", "https load")


def _extra_pages(home: Page) -> list[tuple[str, str]]:
    """Contact page first, then a service-area or about page, same site only."""
    home_domain = domain_key(home.url)
    found: dict[str, tuple[str, int]] = {}
    for a in home.soup.find_all("a", href=True):
        href = urljoin(home.url, a["href"]).split("#")[0]
        if domain_key(href) != home_domain or not href.startswith("http") or href.rstrip("/") == home.url.rstrip("/"):
            continue
        label = (a.get_text(" ", strip=True) + " " + urlparse(href).path).lower()
        if re.search(r"contact|free-estimate|get-a-quote|request-a-quote|estimate", label):
            rank, kind = 0, "contact"
        elif re.search(r"service.?area|areas.?we.?serve|locations", label):
            rank, kind = 1, "service_area"
        elif re.search(r"\babout", label):
            rank, kind = 2, "about"
        else:
            continue
        if href not in found or found[href][1] > rank:
            found[href] = (kind, rank)
    ordered = sorted(found.items(), key=lambda kv: kv[1][1])
    picked, kinds = [], set()
    for href, (kind, _) in ordered:
        if kind in kinds:
            continue
        picked.append((href, kind))
        kinds.add(kind)
    return picked


def _all_text(pages: list[Page]) -> str:
    return " ".join(p.text for p in pages)


def _all_html(pages: list[Page]) -> str:
    return "\n".join(p.html for p in pages)


def _search_pages(pages: list[Page], pattern: re.Pattern):
    for p in pages:
        m = pattern.search(p.text)
        if m:
            return p, m
    return None, None


def _analyze_pages(b: _Builder, home: Page, pages: list[Page], now: datetime,
                   unread_kinds: set[str] | None = None) -> None:
    thin = len(home.text) < THIN_TEXT_CHARS
    checked = ", ".join(p.url for p in pages)
    thin_note = (f"Homepage has only {len(home.text)} characters of server-rendered text; content is probably "
                 f"built by JavaScript, which this scanner does not run.")
    has_contact = any(p.kind == "contact" for p in pages)
    jsonld = _jsonld_business(home)

    def absent(key, outcome, etype, conf, observation, test, result, url=home.url):
        if thin:
            b.unknown(key, url, thin_note, test)
        else:
            b.add(key, outcome, etype, conf, url, observation, test, result)

    # --- roofing_core_service
    title = home.soup.title.get_text(" ", strip=True) if home.soup.title else ""
    h1s = " | ".join(h.get_text(" ", strip=True) for h in home.soup.find_all("h1"))
    meta = home.soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    desc = meta.get("content", "") if meta else ""
    prominent = " | ".join(x for x in (title, h1s, desc) if x)
    types = {t.lower() for t in jsonld.get("@types", [])}
    roof_mentions = len(ROOFING.findall(_all_text(pages)))
    if "roofingcontractor" in types:
        b.add("roofing_core_service", Outcome.MET, EvidenceType.OBSERVED, 0.95, home.url,
              "Homepage structured data declares the business type RoofingContractor.", "schema.org @type",
              "RoofingContractor")
    elif ROOFING.search(prominent):
        b.add("roofing_core_service", Outcome.MET, EvidenceType.OBSERVED, 0.9, home.url,
              f"Homepage title/heading/description mentions roofing: \"{prominent[:160]}\"", "title/H1/meta text",
              "roofing term found")
    elif roof_mentions >= 3:
        b.add("roofing_core_service", Outcome.MET, EvidenceType.INFERRED, 0.65, home.url,
              f"Roofing is mentioned {roof_mentions} times in page text but not in the title or main heading.",
              "page text", f"{roof_mentions} mentions")
    else:
        absent("roofing_core_service", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.75,
               f"No mention of roofing in the title, headings or text of: {checked}.", "title/H1/page text",
               f"{roof_mentions} mentions")

    # --- residential_services
    page, m = _search_pages(pages, RESIDENTIAL)
    if m:
        terms = sorted({t.lower() for t in RESIDENTIAL.findall(_all_text(pages))})[:4]
        b.add("residential_services", Outcome.MET, EvidenceType.OBSERVED, 0.85, page.url,
              f"Mentions {', '.join(terms)}: \"{_snippet(page.text, m)}\"", "page text", "residential terms found")
    else:
        absent("residential_services", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.6,
               f"No residential/homeowner/storm-damage terms on: {checked}.", "page text", "no residential terms")

    # --- service_area_stated
    area = jsonld.get("areaServed")
    page, m = _search_pages(pages, SERVICE_AREA)
    if area:
        b.add("service_area_stated", Outcome.MET, EvidenceType.OBSERVED, 0.9, home.url,
              f"Structured data lists areaServed: {area[:160]}", "schema.org areaServed", "present")
    elif m:
        b.add("service_area_stated", Outcome.MET, EvidenceType.OBSERVED, 0.85, page.url,
              f"\"{_snippet(page.text, m)}\"", "page text", "service-area phrase found")
    else:
        absent("service_area_stated", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.6,
               f"No 'serving…' / 'service area' statement on: {checked}.", "page text", "no service-area phrase")

    # --- active_business
    years = [int(y) for y in COPYRIGHT.findall(home.text + " " + home.html[-5000:])]
    latest = max(years) if years else None
    if latest and latest >= now.year - 1:
        b.add("active_business", Outcome.MET, EvidenceType.OBSERVED, 0.75, home.url,
              f"Homepage loads and its copyright notice shows {latest}.", "copyright year", str(latest))
    else:
        shown = f"copyright year {latest}" if latest else "no copyright year"
        b.unknown("active_business", home.url,
                  f"Homepage loads but shows {shown}; current activity not confirmed.", "copyright year")

    # --- social_proof
    page, m = _search_pages(pages, SOCIAL_PROOF)
    widget = REVIEW_WIDGETS.search(_all_html(pages))
    if widget:
        b.add("social_proof", Outcome.MET, EvidenceType.OBSERVED, 0.8, home.url,
              f"Review widget embedded ({widget.group(0)}).", "review widget script", widget.group(0))
    elif m:
        b.add("social_proof", Outcome.MET, EvidenceType.OBSERVED, 0.8, page.url,
              f"\"{_snippet(page.text, m)}\"", "page text", "reviews/testimonials found")
    else:
        absent("social_proof", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.55,
               f"No reviews or testimonials section found on: {checked}.", "page text", "not found")

    # --- no_lead_form
    form_hit = None
    for p in pages:
        form_hit = _lead_form(p)
        if form_hit:
            break
    if form_hit:
        p, desc_ = form_hit
        b.add("no_lead_form", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.9, p.url, desc_, "lead form search",
              "form found")
    elif "contact" in (unread_kinds or set()):
        b.unknown("no_lead_form", home.url, "The contact page link exists but the page could not be loaded, so "
                  "the absence of a form is not counted.", "lead form search")
    elif has_contact:
        absent("no_lead_form", Outcome.MET, EvidenceType.OBSERVED, 0.8,
               f"No form with name/email/phone fields and no known form embed on: {checked}.", "lead form search",
               f"not found on {len(pages)} page(s)")
    else:
        absent("no_lead_form", Outcome.MET, EvidenceType.OBSERVED, 0.65,
               "No lead form on the homepage and no contact page link was found.", "lead form search",
               "not found on homepage; no contact page")

    # --- no_click_to_call
    tels = [a["href"] for a in home.soup.find_all("a", href=True) if a["href"].lower().startswith("tel:")]
    if tels:
        b.add("no_click_to_call", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.95, home.url,
              f"Homepage has a tap-to-call link ({tels[0]}).", "tel: link search", f"{len(tels)} tel: link(s)")
    else:
        shown = PHONE_TEXT.search(home.text)
        extra = f" The number {shown.group(0)} appears as plain text." if shown else ""
        absent("no_click_to_call", Outcome.MET, EvidenceType.OBSERVED, 0.85,
               "Homepage HTML has no tel: (tap-to-call) link." + extra, "tel: link search", "0 tel: links")

    # --- no_clear_cta
    cta_texts = [el.get_text(" ", strip=True) or el.get("value", "") for el in
                 home.soup.find_all(["a", "button", "h1", "h2", "h3"])]
    cta_texts += [el.get("value", "") for el in home.soup.find_all("input", attrs={"type": re.compile("submit|button")})]
    cta = next((t for t in cta_texts if t and CTA.search(t)), None)
    if cta:
        b.add("no_clear_cta", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.85, home.url,
              f"Homepage call-to-action: \"{cta[:80]}\".", "CTA text search", "found")
    else:
        absent("no_clear_cta", Outcome.MET, EvidenceType.OBSERVED, 0.75,
               "No link, button or heading on the homepage offers an estimate, inspection, quote or booking.",
               "CTA text search", "not found")

    # --- missing_mobile_viewport
    vp = home.soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
    content = (vp.get("content", "") if vp else "").lower().replace(" ", "")
    if "width=device-width" in content:
        b.add("missing_mobile_viewport", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.95, home.url,
              f"Homepage sets <meta name=\"viewport\" content=\"{vp.get('content')}\">.", "meta viewport", "present")
    else:
        b.add("missing_mobile_viewport", Outcome.MET, EvidenceType.OBSERVED, 0.9, home.url,
              "Homepage has no <meta name=\"viewport\" content=\"width=device-width…\"> tag."
              + (f" Found: \"{vp.get('content')}\"." if vp else ""), "meta viewport", "missing")

    # --- tracking
    html = _all_html(pages)
    ads = [name for name, rx in AD_TAGS.items() if rx.search(html)]
    gtm = GTM.search(html)
    if ads:
        b.add("no_ad_conversion_tracking", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.9, home.url,
              f"Found ad tracking tag(s) in page HTML: {', '.join(ads)}.", "ad tag search", ", ".join(ads))
    elif gtm:
        b.unknown("no_ad_conversion_tracking", home.url,
                  f"Google Tag Manager container ({gtm.group(0)}) present; ad tags may load inside it. "
                  "Check with Google Tag Assistant or Meta Pixel Helper.", "ad tag search")
    else:
        b.add("no_ad_conversion_tracking", Outcome.MET, EvidenceType.INFERRED, 0.55 if thin else 0.7, home.url,
              "No Google Ads, Meta, Microsoft, LinkedIn, TikTok or Nextdoor tags in the HTML of: "
              f"{checked}. Tags added server-side or after consent would not be visible.", "ad tag search",
              "none found")
    analytics = [name for name, rx in ANALYTICS.items() if rx.search(html)]
    if analytics:
        b.add("no_analytics", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.9, home.url,
              f"Found analytics: {', '.join(analytics)}.", "analytics tag search", ", ".join(analytics))
    elif gtm:
        b.add("no_analytics", Outcome.NOT_MET, EvidenceType.INFERRED, 0.6, home.url,
              f"Google Tag Manager ({gtm.group(0)}) is installed; analytics is usually deployed through it.",
              "analytics tag search", "tag manager found")
    else:
        b.add("no_analytics", Outcome.MET, EvidenceType.INFERRED, 0.55 if thin else 0.7, home.url,
              f"No analytics or tag-manager script in the HTML of: {checked}.", "analytics tag search", "none found")

    # --- no_chat_or_booking
    tools = [name for name, rx in CHAT_BOOKING_RE.items() if rx.search(html)]
    if any(a["href"].lower().startswith("sms:") for p in pages for a in p.soup.find_all("a", href=True)):
        tools.append("text-us (sms:) link")
    if tools:
        b.add("no_chat_or_booking", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.85, home.url,
              f"Found chat/booking tool(s): {', '.join(tools)}.", "chat/booking widget search", ", ".join(tools))
    else:
        absent("no_chat_or_booking", Outcome.MET, EvidenceType.INFERRED, 0.65,
               f"No known chat, text-us, scheduling or booking widget on: {checked}. Widgets injected later by "
               "JavaScript would not be visible.", "chat/booking widget search", "none found")

    # --- commercial readiness
    page, m = _search_pages(pages, FINANCING)
    if m:
        b.add("financing_offered", Outcome.MET, EvidenceType.OBSERVED, 0.85, page.url, f"\"{_snippet(page.text, m)}\"",
              "page text", "financing mention found")
    else:
        absent("financing_offered", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.55,
               f"No financing or payment-plan mention on: {checked}.", "page text", "not found")

    page, m = _search_pages(pages, CERTIFICATIONS)
    if m:
        b.add("manufacturer_certification", Outcome.MET, EvidenceType.OBSERVED, 0.7, page.url,
              f"Site claims \"{m.group(0)}\" (claim not yet verified in the manufacturer's directory).", "page text",
              m.group(0))
    else:
        absent("manufacturer_certification", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.55,
               f"No manufacturer certification claim on: {checked}.", "page text", "not found")

    text_all = _all_text(pages)
    lines = [name for name, rx in SERVICE_LINES.items() if re.search(rx, text_all, re.I)]
    if len(lines) >= 2:
        b.add("multiple_service_lines", Outcome.MET, EvidenceType.OBSERVED, 0.8, home.url,
              f"Mentions other services: {', '.join(lines)}.", "page text", f"{len(lines)} service lines")
    else:
        absent("multiple_service_lines", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.6,
               f"Fewer than two other service lines mentioned ({', '.join(lines) or 'none'}).", "page text",
               f"{len(lines)} service lines")

    page, m = _search_pages(pages, LICENSED)
    if m:
        b.add("licensed_insured", Outcome.MET, EvidenceType.OBSERVED, 0.75, page.url,
              f"\"{_snippet(page.text, m)}\" (claim; verify in the state license database).", "page text",
              "licensed/insured statement found")
    else:
        absent("licensed_insured", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.55,
               f"No licensed/insured statement on: {checked}.", "page text", "not found")

    page, m = _search_pages(pages, FOUNDED)
    if m:
        year = int(m.group(1))
        met = now.year - year >= 5
        b.add("established_years", Outcome.MET if met else Outcome.NOT_MET, EvidenceType.OBSERVED, 0.75, page.url,
              f"\"{_snippet(page.text, m)}\"", "founding year", str(year))
    else:
        page, m = _search_pages(pages, YEARS)
        if m:
            n = int(m.group(1))
            b.add("established_years", Outcome.MET if n >= 5 else Outcome.NOT_MET, EvidenceType.INFERRED, 0.6,
                  page.url, f"\"{_snippet(page.text, m)}\" (may describe the owner's experience, not the business).",
                  "years statement", f"{n} years")
        else:
            b.unknown("established_years", home.url, f"No founding year or years-in-business statement on: {checked}.",
                      "founding year")

    profiles = []
    for p in pages:
        for a in p.soup.find_all("a", href=True):
            parsed = urlparse(a["href"])
            host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
            if host in SOCIAL_HOSTS and parsed.path.strip("/") and not SHARE_PATH.search(parsed.path):
                profile = f"{host}/{parsed.path.strip('/').split('/')[0]}"
                if profile not in profiles:
                    profiles.append(profile)
    if profiles:
        b.add("active_social_profiles", Outcome.MET, EvidenceType.OBSERVED, 0.85, home.url,
              f"Links to {', '.join(profiles[:4])}.", "social link search", f"{len(profiles)} profile link(s)")
    else:
        absent("active_social_profiles", Outcome.NOT_MET, EvidenceType.OBSERVED, 0.6,
               f"No links to business social profiles on: {checked}.", "social link search", "none found")


def _lead_form(page: Page) -> tuple[Page, str] | None:
    for form in page.soup.find_all("form"):
        if form.get("role") == "search" or re.search(r"search", " ".join(form.get("class", [])) + form.get("id", ""), re.I):
            continue
        fields = []
        has_password = False
        for el in form.find_all(["input", "textarea", "select"]):
            itype = (el.get("type") or "text").lower()
            name = (el.get("name") or el.get("id") or el.get("placeholder") or itype).lower()
            if itype == "password":
                has_password = True
            if el.name == "textarea" or itype in ("text", "email", "tel", "number"):
                if itype == "search" or name in ("s", "q", "search"):
                    continue
                fields.append(name[:30])
        if fields and not has_password:
            return page, f"Form with fields {', '.join(fields[:5])} on {page.url}."
    for tag, attr, pattern in (("iframe", "src", FORM_IFRAMES), ("script", "src", FORM_SCRIPTS),
                               ("div", "class", FORM_CONTAINERS)):
        for el in page.soup.find_all(tag):
            value = el.get(attr) or ""
            value = " ".join(value) if isinstance(value, list) else value
            m = pattern.search(value)
            if m:
                return page, f"Embedded form ({m.group(0)}) on {page.url}."
    return None


def _jsonld_business(page: Page) -> dict:
    """Name, phone, address and areaServed from schema.org JSON-LD, if present."""
    out: dict = {"@types": []}
    for script in page.soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        items = []
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                items.append(item)
                if isinstance(item.get("@graph"), list):
                    stack.extend(item["@graph"])
        for item in items:
            types = item.get("@type", [])
            types = [types] if isinstance(types, str) else [t for t in types if isinstance(t, str)]
            if not {t.lower() for t in types} & LOCAL_TYPES:
                continue
            out["@types"].extend(types)
            out.setdefault("name", item.get("name"))
            out.setdefault("telephone", item.get("telephone"))
            out.setdefault("email", item.get("email"))
            addr = item.get("address")
            if isinstance(addr, dict):
                out.setdefault("street", addr.get("streetAddress"))
                out.setdefault("city", addr.get("addressLocality"))
                out.setdefault("state", addr.get("addressRegion"))
                out.setdefault("postal_code", addr.get("postalCode"))
            area = item.get("areaServed")
            if area and "areaServed" not in out:
                areas = area if isinstance(area, list) else [area]
                names = [a.get("name") if isinstance(a, dict) else str(a) for a in areas]
                out["areaServed"] = ", ".join(n for n in names if n)
    return out


def extract_identity(home: Page, pages: list[Page]) -> dict[str, str]:
    data = _jsonld_business(home)
    identity: dict[str, str] = {}
    name = data.get("name")
    if not name:
        og = home.soup.find("meta", attrs={"property": "og:site_name"})
        name = og.get("content") if og else None
    if not name and home.soup.title:
        parts = [p.strip() for p in re.split(r"\s[|–—-]\s|:", home.soup.title.get_text(" ", strip=True)) if p.strip()]
        name = next((p for p in parts if ROOFING.search(p)), parts[0] if parts else None)
    if name and isinstance(name, str) and len(name) <= 120:
        identity["name"] = name.strip()
    phone = data.get("telephone")
    if not phone:
        tel = next((a["href"][4:] for a in home.soup.find_all("a", href=True) if a["href"].lower().startswith("tel:")),
                   None)
        phone = tel
    if phone and phone_key(phone):
        identity["phone"] = phone
    email = data.get("email")
    if not email:
        for p in pages:
            mail = next((a["href"][7:].split("?")[0] for a in p.soup.find_all("a", href=True)
                         if a["href"].lower().startswith("mailto:")), None)
            if mail:
                email = mail
                break
    if email and re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email, re.I):
        identity["email"] = email.lower()
    for key in ("street", "city", "state", "postal_code"):
        if isinstance(data.get(key), str) and data[key].strip():
            identity[key] = data[key].strip()
    if isinstance(identity.get("state"), str) and len(identity["state"]) != 2:
        identity.pop("state")
    parsed = urlparse(home.url)
    identity["website"] = f"{parsed.scheme}://{parsed.netloc}/"
    return identity
