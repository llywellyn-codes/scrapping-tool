import requests

from prospect_finder.analyzer.fetcher import PoliteFetcher
from prospect_finder.analyzer.website import WEBSITE_SIGNALS, analyze_website
from prospect_finder.models import EvidenceType, Outcome
from prospect_finder.scoring import evidence_problems, score_findings

from .fake_http import FakeClock, FakeResponse, FakeSession
from .helpers import NOW

UA = "RoofingProspectFinder/1.0 (+test)"

GOOD_HOME = """<!doctype html><html><head>
<title>Summit Peak Roofing | Dallas Roof Replacement</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Residential roofing and storm damage repair.">
<script async src="https://www.googletagmanager.com/gtag/js?id=G-ABC1234567"></script>
<script>fbq('init', '123');</script>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"RoofingContractor",
 "name":"Summit Peak Roofing","telephone":"+1-214-555-0142",
 "address":{"@type":"PostalAddress","streetAddress":"1200 Elm St","addressLocality":"Dallas","addressRegion":"TX","postalCode":"75201"},
 "areaServed":["Dallas","Plano","Frisco"]}</script>
</head><body>
<h1>Dallas Roofing Experts</h1>
<a href="tel:+12145550142">Call (214) 555-0142</a>
<a href="/contact">Get a Free Estimate</a>
<a href="https://www.facebook.com/summitpeakroofing">Facebook</a>
<p>Family owned since 2004. Licensed and insured. GAF Master Elite contractor. We offer financing.</p>
<p>Roof replacement, gutters, siding and windows for homeowners across North Texas.</p>
<p>Read our customer reviews and testimonials.</p>
<p>&copy; 2026 Summit Peak Roofing</p>
<script src="https://widgets.leadconnectorhq.com/loader.js"></script>
</body></html>"""

CONTACT = """<html><head><title>Contact</title></head><body><h1>Contact us</h1>
<form action="/send"><input name="name"><input type="email" name="email"><input type="tel" name="phone">
<textarea name="message"></textarea><button>Send</button></form></body></html>"""

WEAK_HOME = """<html><head><title>Bayou Shield Roofing</title></head><body>
<h1>Bayou Shield Roofing</h1>
<p>Quality roofs in New Orleans. Call 504-555-0117 for roofing work. We have done roofing for many years
and take pride in every roof we build. Roofing is our trade and our family business, serving homeowners.</p>
<p>Copyright 2019</p>
<a href="/about">About</a>
</body></html>"""


def fetcher(routes, **kwargs):
    clock = FakeClock()
    f = PoliteFetcher(UA, session=FakeSession(routes), sleep=clock.sleep, clock=clock, min_delay=3.0, **kwargs)
    return f, clock


def by_key(result):
    return {f.signal_key: f for f in result.findings}


def test_well_built_site_produces_observed_findings_with_evidence():
    f, _ = fetcher({
        "https://summitpeak.example/": FakeResponse(200, GOOD_HOME),
        "https://summitpeak.example/contact": FakeResponse(200, CONTACT),
    })
    result = analyze_website("summitpeak.example", f, now=NOW)
    assert result.status == "ok"
    found = by_key(result)
    assert set(found) == set(WEBSITE_SIGNALS)
    expect = {
        "no_working_website": Outcome.NOT_MET, "roofing_core_service": Outcome.MET,
        "residential_services": Outcome.MET, "service_area_stated": Outcome.MET, "active_business": Outcome.MET,
        "social_proof": Outcome.MET, "no_lead_form": Outcome.NOT_MET, "no_click_to_call": Outcome.NOT_MET,
        "no_clear_cta": Outcome.NOT_MET, "missing_mobile_viewport": Outcome.NOT_MET, "no_https": Outcome.NOT_MET,
        "no_ad_conversion_tracking": Outcome.NOT_MET, "no_analytics": Outcome.NOT_MET,
        "no_chat_or_booking": Outcome.NOT_MET, "financing_offered": Outcome.MET,
        "manufacturer_certification": Outcome.MET, "multiple_service_lines": Outcome.MET,
        "licensed_insured": Outcome.MET, "established_years": Outcome.MET, "active_social_profiles": Outcome.MET,
    }
    assert {k: v.outcome for k, v in found.items()} == expect
    assert "contact" in found["no_lead_form"].source_url
    for finding in result.findings:
        assert evidence_problems(finding) == [], finding.signal_key
    assert result.identity["name"] == "Summit Peak Roofing"
    assert result.identity["city"] == "Dallas" and result.identity["state"] == "TX"
    card = score_findings(result.findings, now=NOW)
    assert card.categories["business_fit"].score == 30
    assert card.categories["commercial_readiness"].score == 20
    assert card.categories["marketing_opportunity"].score == 0


def test_weak_site_produces_marketing_opportunities():
    f, _ = fetcher({"https://bayou.example/": FakeResponse(200, WEAK_HOME),
                    "https://bayou.example/about": FakeResponse(200, "<html><body><p>About us.</p></body></html>")})
    found = by_key(analyze_website("https://bayou.example/", f, now=NOW))
    assert found["no_lead_form"].outcome == Outcome.MET
    assert found["no_lead_form"].confidence == 0.65  # no contact page found
    assert found["no_click_to_call"].outcome == Outcome.MET
    assert "504-555-0117 appears as plain text" in found["no_click_to_call"].observation
    assert found["missing_mobile_viewport"].outcome == Outcome.MET
    assert found["no_clear_cta"].outcome == Outcome.MET
    assert found["no_ad_conversion_tracking"].evidence_type == EvidenceType.INFERRED
    assert found["active_business"].outcome == Outcome.UNKNOWN  # old copyright year proves nothing
    assert found["established_years"].outcome == Outcome.UNKNOWN
    card = score_findings(list(found.values()), now=NOW)
    # conversion path capped at 17, mobile 7, tracking 10, chat 6
    assert card.categories["marketing_opportunity"].score == 40


def test_javascript_shell_page_reports_unknown_instead_of_problems():
    shell = ('<html><head><title>Roofing Co</title><meta name="viewport" content="width=device-width">'
             '</head><body><div id="root"></div><script src="/app.js"></script></body></html>')
    f, _ = fetcher({"https://spa.example/": FakeResponse(200, shell)})
    found = by_key(analyze_website("spa.example", f, now=NOW))
    for key in ("no_lead_form", "no_click_to_call", "no_clear_cta", "no_chat_or_booking", "social_proof"):
        assert found[key].outcome == Outcome.UNKNOWN, key
        assert "JavaScript" in found[key].observation
    assert found["roofing_core_service"].outcome == Outcome.MET  # presence is still observable


def test_unreachable_site_is_a_confirmed_failure_after_two_attempts():
    f, _ = fetcher({})
    result = analyze_website("gone-roofing.example", f, now=NOW)
    assert result.status == "unreachable"
    found = by_key(result)
    nw = found["no_working_website"]
    assert nw.outcome == Outcome.MET and nw.evidence_type == EvidenceType.CONFIRMED_FAILURE
    assert "retry" in nw.test_result and evidence_problems(nw) == []
    assert all(found[k].outcome == Outcome.UNKNOWN for k in WEBSITE_SIGNALS if k != "no_working_website")


def test_http_only_site_is_flagged_for_https():
    tls = requests.exceptions.SSLError("certificate verify failed")
    f, _ = fetcher({"https://old.example/robots.txt": tls, "https://old.example/": tls,
                    "http://old.example/": FakeResponse(200, WEAK_HOME)})
    found = by_key(analyze_website("http://old.example/", f, now=NOW))
    assert found["no_https"].outcome == Outcome.MET
    assert found["no_https"].evidence_type == EvidenceType.CONFIRMED_FAILURE
    assert found["no_working_website"].outcome == Outcome.NOT_MET


def test_robots_disallow_is_respected():
    f, _ = fetcher({"https://private.example/robots.txt": FakeResponse(200, "User-agent: *\nDisallow: /\n",
                                                                         {"Content-Type": "text/plain"}),
                    "https://private.example/": FakeResponse(200, GOOD_HOME)})
    result = analyze_website("private.example", f, now=NOW)
    assert result.status == "blocked"
    assert "robots.txt" in result.detail
    assert all(x.outcome == Outcome.UNKNOWN for x in result.findings)
    assert ("https://private.example/", ) not in [(u,) for u, _ in f.session.requests]


def test_robots_server_error_means_do_not_crawl():
    f, _ = fetcher({"https://flaky.example/robots.txt": FakeResponse(503, "down"),
                    "https://flaky.example/": FakeResponse(200, GOOD_HOME)})
    assert analyze_website("flaky.example", f, now=NOW).status == "blocked"


def test_captcha_challenge_is_not_bypassed():
    challenge = "<html><head><title>Just a moment...</title></head><body>cf-browser-verification</body></html>"
    f, _ = fetcher({"https://guarded.example/": FakeResponse(503, challenge)})
    result = analyze_website("guarded.example", f, now=NOW)
    assert result.status == "blocked"
    assert "bot challenge" in result.detail
    # Exactly one page request after robots.txt: no retries against a challenge.
    page_requests = [u for u, _ in f.session.requests if not u.endswith("robots.txt")]
    assert page_requests == ["https://guarded.example/"]


def test_recaptcha_on_a_normal_contact_form_is_not_treated_as_a_block():
    page = GOOD_HOME.replace("</body>", '<div class="g-recaptcha" data-sitekey="x"></div></body>')
    f, _ = fetcher({"https://ok.example/": FakeResponse(200, page)})
    assert analyze_website("ok.example", f, now=NOW).status == "ok"


def test_login_and_forbidden_responses_stop_the_scan():
    for status in (401, 403, 429):
        f, _ = fetcher({"https://locked.example/": FakeResponse(status, "no")})
        assert analyze_website("locked.example", f, now=NOW).status == "blocked"
    f, _ = fetcher({"https://portal.example/": FakeResponse(302, "", {"Location": "/wp-login.php"})})
    result = analyze_website("portal.example", f, now=NOW)
    assert result.status == "blocked" and "login" in result.detail


def test_rate_limit_spaces_requests_to_the_same_host():
    f, clock = fetcher({"https://summitpeak.example/": FakeResponse(200, GOOD_HOME),
                        "https://summitpeak.example/contact": FakeResponse(200, CONTACT)})
    analyze_website("summitpeak.example", f, now=NOW)
    assert len(f.session.requests) == 3  # robots.txt, home, contact
    assert clock.sleeps and all(s <= 3.0 for s in clock.sleeps)
    assert sum(clock.sleeps) >= 6.0  # two waits of 3 s between the three requests


def test_crawl_delay_from_robots_is_honoured():
    robots = FakeResponse(200, "User-agent: *\nCrawl-delay: 10\n", {"Content-Type": "text/plain"})
    f, clock = fetcher({"https://slow.example/robots.txt": robots,
                        "https://slow.example/": FakeResponse(200, GOOD_HOME),
                        "https://slow.example/contact": FakeResponse(200, CONTACT)})
    analyze_website("slow.example", f, now=NOW)
    assert max(clock.sleeps) == 10


def test_user_agent_is_sent_and_no_url_means_no_website():
    f, _ = fetcher({"https://summitpeak.example/": FakeResponse(200, GOOD_HOME)})
    analyze_website("summitpeak.example", f, now=NOW, max_pages=1)
    assert all(h["User-Agent"] == UA for _, h in f.session.requests)
    assert analyze_website(None, f, now=NOW).status == "no_website"


def test_google_tag_manager_makes_ad_tracking_unknown():
    page = WEAK_HOME.replace("</head>", '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-ABCD12"></script></head>')
    f, _ = fetcher({"https://gtm.example/": FakeResponse(200, page)})
    found = by_key(analyze_website("gtm.example", f, now=NOW, max_pages=1))
    assert found["no_ad_conversion_tracking"].outcome == Outcome.UNKNOWN
    assert found["no_analytics"].outcome == Outcome.NOT_MET


def test_service_area_needs_a_place_name():
    from prospect_finder.analyzer.website import SERVICE_AREA
    for text in ("Proudly serving Dallas and Fort Worth", "serving the Greater Tulsa area", "Our Service Areas",
                 "Areas we serve", "We serve North Texas"):
        assert SERVICE_AREA.search(text), text
    for text in ("serving homeowners for years", "we serve our customers", "self-serve portal"):
        assert not SERVICE_AREA.search(text), text
