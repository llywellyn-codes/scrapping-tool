"""The scoring rubric: every signal, its weight, and how it is measured.

This file is the single source of truth for weights. docs/SCORING.md and the
/rubric page are generated from (or checked against) these definitions, and
tests assert that each category's achievable maximum equals its budget.

Points are awarded only when a signal's condition is *met* and backed by valid,
fresh evidence. Marketing-opportunity signals describe problems an agency could
fix, so "met" there means the problem was found.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BUSINESS_FIT = "business_fit"
MARKETING_OPPORTUNITY = "marketing_opportunity"
COMMERCIAL_READINESS = "commercial_readiness"

CATEGORY_MAX: dict[str, float] = {
    BUSINESS_FIT: 30,
    MARKETING_OPPORTUNITY: 50,
    COMMERCIAL_READINESS: 20,
}

CATEGORY_LABELS = {
    BUSINESS_FIT: "Business fit",
    MARKETING_OPPORTUNITY: "Marketing opportunities",
    COMMERCIAL_READINESS: "Commercial readiness",
}


@dataclass(frozen=True)
class ScoringPolicy:
    # A determinate finding below this confidence is not scored; it is treated
    # as unknown for coverage and flags the prospect for manual review.
    min_finding_confidence: float = 0.5
    # Below either of these the prospect needs manual review before it gets a tier.
    min_coverage: float = 0.6
    min_category_coverage: dict = field(default_factory=lambda: {BUSINESS_FIT: 0.5})
    min_confidence: float = 0.6
    # Minimum total score for each tier, checked in order.
    tiers: tuple = (("A", 60.0), ("B", 40.0), ("C", 20.0), ("D", 0.0))
    # A finding is stale once older than this many days for its source type.
    max_evidence_age_days: dict = field(
        default_factory=lambda: {
            "website_scan": 90,
            "manual": 180,
            "csv_import": 365,
            "openstreetmap": 365,
            "google_places": 30,
        }
    )
    default_max_evidence_age_days: int = 90
    # Sources whose findings come from an automated test and therefore must
    # record the test name and result.
    automated_sources: frozenset = frozenset({"website_scan", "google_places", "openstreetmap"})
    # When findings for the same signal disagree, the higher-priority source wins
    # (a human check beats an automated one), then the most recent observation.
    source_priority: dict = field(default_factory=lambda: {"manual": 2})


@dataclass(frozen=True)
class Signal:
    key: str
    category: str
    label: str
    max_points: float
    measured_by: str
    research_step: str
    group: str | None = None
    supersedes: tuple[str, ...] = ()
    # Plain-language explanation for the prospect audit (opportunity signals only).
    audit_why: str | None = None


@dataclass(frozen=True)
class Group:
    """Related problems that share one points budget so they are not double-counted."""

    key: str
    label: str
    cap: float


GROUPS: dict[str, Group] = {
    g.key: g
    for g in (
        Group("conversion_path", "Weak conversion path (form, call button, call-to-action)", 17),
        Group("tracking", "Missing measurement (ad conversion tracking, analytics)", 10),
    )
}

_WEBSITE_DEPENDENT = (
    "no_lead_form",
    "no_click_to_call",
    "no_clear_cta",
    "missing_mobile_viewport",
    "no_https",
    "no_ad_conversion_tracking",
    "no_analytics",
    "no_chat_or_booking",
)

SIGNALS: tuple[Signal, ...] = (
    # ---------------- Business fit (30) ----------------
    Signal(
        "roofing_core_service", BUSINESS_FIT, "Roofing is the primary service", 10,
        "Homepage title, H1, meta description or schema.org type names roofing; or a "
        "manual check of the website / state license record.",
        "Confirm roofing is the main service on their website or state license record.",
    ),
    Signal(
        "residential_services", BUSINESS_FIT, "Serves residential homeowners", 6,
        "Site text mentions residential roofing, homeowners, roof replacement, storm/hail "
        "damage or insurance claims.",
        "Check their services page for residential work (replacement, storm damage, insurance claims).",
    ),
    Signal(
        "service_area_stated", BUSINESS_FIT, "Service area is stated", 6,
        "Site text says 'serving …', 'service area' or 'areas we serve', or schema.org areaServed.",
        "Look for a service-area or locations page listing the cities they cover.",
    ),
    Signal(
        "active_business", BUSINESS_FIT, "Business appears active", 4,
        "Website loads and shows a current copyright year, or business status is operational "
        "in a permitted listing source, or confirmed manually.",
        "Check recent reviews, social posts or the state license status to confirm they are operating.",
    ),
    Signal(
        "social_proof", BUSINESS_FIT, "Reviews or testimonials are visible", 4,
        "Testimonials/reviews section or review widget on the site, or reviews confirmed manually.",
        "Check their website and review profiles for customer reviews or testimonials.",
    ),
    # ---------------- Marketing opportunities (50) ----------------
    Signal(
        "no_working_website", MARKETING_OPPORTUNITY, "No working website", 30,
        "A manual check confirmed the website is missing or broken (opened in a browser, or a search "
        "found no site). Automated request failures are recorded as failed checks, never as evidence; "
        "a missing URL alone is 'unknown'.",
        "Search for the business name + city to find a website, then re-run the scan.",
        supersedes=_WEBSITE_DEPENDENT,
        audit_why="Homeowners who look you up online have nowhere to learn about your work or request a quote.",
    ),
    Signal(
        "no_lead_form", MARKETING_OPPORTUNITY, "No lead-capture form found", 10,
        "No form with name/email/phone fields (or known form embed) on the homepage or contact page.",
        "Open the site on a phone and look for a quote or contact form.",
        group="conversion_path",
        audit_why="Visitors who would rather not call have no quick way to send their details.",
    ),
    Signal(
        "no_click_to_call", MARKETING_OPPORTUNITY, "No tap-to-call phone link", 6,
        "Homepage HTML has no tel: link.",
        "Open the homepage on a phone and check whether the phone number is tappable.",
        group="conversion_path",
        audit_why="Mobile visitors have to copy the number by hand to call you.",
    ),
    Signal(
        "no_clear_cta", MARKETING_OPPORTUNITY, "No estimate/inspection call-to-action", 5,
        "No link, button or heading on the homepage offering a quote, estimate, inspection or booking.",
        "Check the top of the homepage for a clear 'free estimate' or 'schedule inspection' button.",
        group="conversion_path",
        audit_why="Visitors are not given an obvious next step such as requesting an estimate.",
    ),
    Signal(
        "missing_mobile_viewport", MARKETING_OPPORTUNITY, "Not configured for mobile screens", 7,
        "Homepage has no <meta name=viewport content=width=device-width> tag.",
        "Open the site on a phone and check whether text and buttons are readable without zooming.",
        audit_why="Without a mobile viewport setting, phones usually show a shrunken desktop page.",
    ),
    Signal(
        "no_https", MARKETING_OPPORTUNITY, "No working HTTPS", 4,
        "https:// fails (connection or certificate error) while http:// loads.",
        "Open the https:// version of the site and check for a browser security warning.",
        audit_why="Browsers label pages without working HTTPS as 'Not secure', which can deter visitors.",
    ),
    Signal(
        "no_ad_conversion_tracking", MARKETING_OPPORTUNITY, "No ad conversion tracking detected", 8,
        "No Google Ads, Meta, Microsoft Ads, LinkedIn, TikTok or Nextdoor tags in homepage HTML. "
        "If a Google Tag Manager container is present the result is 'unknown'.",
        "Use Google Tag Assistant / Meta Pixel Helper in a browser to check for ad tags.",
        group="tracking",
        audit_why="Without conversion tracking it is hard to tell which ads or channels produce calls and form leads.",
    ),
    Signal(
        "no_analytics", MARKETING_OPPORTUNITY, "No website analytics detected", 4,
        "No Google Analytics, Tag Manager or other analytics script in homepage HTML.",
        "Use a browser tag inspector to confirm whether analytics is installed.",
        group="tracking",
        audit_why="Without analytics there is no record of how many people visit or which pages they use.",
    ),
    Signal(
        "no_chat_or_booking", MARKETING_OPPORTUNITY, "No chat, text or online booking tool", 6,
        "No known chat, text-us, scheduling or booking widget/link on fetched pages.",
        "Check the site for a chat bubble, text-us button or online booking link.",
        audit_why="Visitors browsing after hours can't start a conversation or book a time on their own.",
    ),
    Signal(
        "review_generation_gap", MARKETING_OPPORTUNITY, "Fewer than 25 Google reviews", 6,
        "Review count below 25 from the Google Places API (within its caching terms) or a manual check.",
        "Look up their Google Business Profile and note the review count.",
        audit_why="A larger base of recent reviews helps homeowners choose between local roofers.",
    ),
    # ---------------- Commercial readiness (20) ----------------
    Signal(
        "financing_offered", COMMERCIAL_READINESS, "Offers financing", 4,
        "Site mentions financing, payment plans or a known home-improvement lender.",
        "Check their website for a financing page or lender partner.",
    ),
    Signal(
        "manufacturer_certification", COMMERCIAL_READINESS, "Claims manufacturer certification", 4,
        "Site claims GAF, Owens Corning, CertainTeed, Malarkey, IKO, Atlas or TAMKO certification "
        "(a claim until verified in the manufacturer's directory).",
        "Verify the certification in the manufacturer's contractor directory.",
    ),
    Signal(
        "multiple_service_lines", COMMERCIAL_READINESS, "Offers multiple service lines", 3,
        "Site mentions at least two of: siding, gutters, windows, commercial roofing, solar, "
        "skylights, insulation, painting.",
        "Review their services page for additional trades.",
    ),
    Signal(
        "licensed_insured", COMMERCIAL_READINESS, "States licensed / insured", 3,
        "Site states licensed, insured or bonded, or shows a license number; or a manual state "
        "license lookup.",
        "Look up their license in the state contractor licensing database.",
    ),
    Signal(
        "established_years", COMMERCIAL_READINESS, "In business 5+ years", 3,
        "Site states a founding year ('since 2009', 'established 2004') at least five years ago, "
        "or 'N years in business' with N >= 5. No statement = unknown.",
        "Check the About page, license issue date or state business registry for a founding date.",
    ),
    Signal(
        "active_social_profiles", COMMERCIAL_READINESS, "Links to social profiles", 3,
        "Website links to at least one business profile on Facebook, Instagram, YouTube, "
        "LinkedIn, TikTok, Nextdoor or X.",
        "Search for their business pages on Facebook and Instagram.",
    ),
)

SIGNALS_BY_KEY: dict[str, Signal] = {s.key: s for s in SIGNALS}

DEFAULT_POLICY = ScoringPolicy()


def signals_in(category: str) -> list[Signal]:
    return [s for s in SIGNALS if s.category == category]


def achievable_max(category: str) -> float:
    """Best score a business could reach in ``category`` under group caps and supersession.

    Superseding signals (e.g. "no working website") replace the signals they
    supersede, so we evaluate the scenario with and without each superseder.
    """
    signals = signals_in(category)
    superseders = [s for s in signals if s.supersedes]
    scenarios = [set()] + [{s.key} for s in superseders]
    best = 0.0
    for active_superseders in scenarios:
        excluded = {k for s in superseders if s.key in active_superseders for k in s.supersedes}
        excluded |= {s.key for s in superseders if s.key not in active_superseders}
        group_totals: dict[str, float] = {}
        total = 0.0
        for s in signals:
            if s.key in excluded:
                continue
            if s.group:
                group_totals[s.group] = group_totals.get(s.group, 0.0) + s.max_points
            else:
                total += s.max_points
        total += sum(min(v, GROUPS[g].cap) for g, v in group_totals.items())
        best = max(best, total)
    return best


def validate_rubric() -> list[str]:
    """Return a list of problems with the rubric definition (empty when valid)."""
    problems = []
    keys = [s.key for s in SIGNALS]
    if len(keys) != len(set(keys)):
        problems.append("duplicate signal keys")
    for s in SIGNALS:
        if s.category not in CATEGORY_MAX:
            problems.append(f"{s.key}: unknown category {s.category}")
        if s.group and s.group not in GROUPS:
            problems.append(f"{s.key}: unknown group {s.group}")
        for k in s.supersedes:
            if k not in SIGNALS_BY_KEY:
                problems.append(f"{s.key}: supersedes unknown signal {k}")
        if s.max_points <= 0:
            problems.append(f"{s.key}: max_points must be positive")
    for category, budget in CATEGORY_MAX.items():
        got = achievable_max(category)
        if abs(got - budget) > 1e-9:
            problems.append(f"{category}: achievable max {got} != budget {budget}")
    if abs(sum(CATEGORY_MAX.values()) - 100) > 1e-9:
        problems.append("category budgets must add up to 100")
    return problems


def rubric_markdown() -> str:
    """The signal table used in docs/SCORING.md (a test keeps the two in sync)."""
    lines = ["| Category | Signal (`key`) | Max | How it is measured | Overlap rule |",
             "|---|---|---:|---|---|"]
    for s in SIGNALS:
        overlap = []
        if s.group:
            overlap.append(f"shares the `{s.group}` cap of {GROUPS[s.group].cap:g}")
        if s.supersedes:
            overlap.append("when met, replaces " + ", ".join(f"`{k}`" for k in s.supersedes))
        lines.append(f"| {CATEGORY_LABELS[s.category]} | {s.label} (`{s.key}`) | {s.max_points:g} | "
                     f"{s.measured_by} | {'; '.join(overlap)} |")
    return "\n".join(lines)
