"""Deterministic, explainable lead scoring.

Every number on the score card is computed here from stored findings. Nothing
in this module calls a network service or a language model.

Pipeline (see docs/SCORING.md):
1. For each signal pick one finding: reject findings with missing evidence or
   stale evidence, drop low-confidence ones, prefer manual checks, then the most
   recent observation. Other findings for the same signal are kept for display
   but never counted again.
2. Met signals earn their full weight; not-met earn 0; unknowns are excluded.
3. Superseding signals (e.g. "no working website") zero out the signals they replace.
4. Group caps limit related problems to one shared budget.
5. Coverage, confidence, upper bound, review reasons and tier are derived from
   the per-signal results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from urllib.parse import urlparse

from .models import EvidenceType, Finding, Outcome, utcnow
from .rubric import (
    CATEGORY_LABELS,
    CATEGORY_MAX,
    DEFAULT_POLICY,
    GROUPS,
    SIGNALS,
    ScoringPolicy,
    Signal,
)


class SignalState(str, Enum):
    COUNTED = "counted"                    # met, evidence valid, points awarded (maybe capped)
    NOT_MET = "not_met"                    # determined, condition false, 0 points
    SUPERSEDED = "superseded"              # replaced by a superseding signal, 0 points
    UNKNOWN = "unknown"                    # no usable determination
    STALE = "stale"                        # only stale evidence available
    MISSING_EVIDENCE = "missing_evidence"  # determinate finding lacks required evidence
    LOW_CONFIDENCE = "low_confidence"      # determinate finding below confidence threshold


COVERED_STATES = {SignalState.COUNTED, SignalState.NOT_MET, SignalState.SUPERSEDED}


def _is_http_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def evidence_problems(finding: Finding, policy: ScoringPolicy = DEFAULT_POLICY) -> list[str]:
    """Why a finding cannot be scored. Empty list = evidence is complete.

    Determinate findings (met or not met) need a source URL, an observation and
    a timestamp. Automated tests and confirmed failures must also record the
    test name and result. Unknown findings need no evidence.
    """
    problems = []
    if not 0.0 <= finding.confidence <= 1.0:
        problems.append(f"confidence {finding.confidence} is outside 0-1")
    if finding.outcome == Outcome.UNKNOWN:
        if finding.evidence_type != EvidenceType.UNKNOWN:
            problems.append("unknown outcome must use evidence type 'unknown'")
        return problems
    if finding.evidence_type == EvidenceType.UNKNOWN:
        problems.append("a met / not-met finding cannot have evidence type 'unknown'")
    if not _is_http_url(finding.source_url):
        problems.append("missing source URL")
    if not (finding.observation or "").strip():
        problems.append("missing observation")
    if finding.observed_at is None:
        problems.append("missing timestamp")
    needs_test = (
        finding.evidence_type == EvidenceType.CONFIRMED_FAILURE
        or finding.source_type in policy.automated_sources
    )
    if needs_test and not ((finding.test_name or "").strip() and (finding.test_result or "").strip()):
        problems.append("missing test name/result")
    return problems


def staleness(finding: Finding, now: datetime, policy: ScoringPolicy = DEFAULT_POLICY) -> str | None:
    """Reason the finding is stale, or None if it is fresh."""
    if finding.expires_at is not None and now >= finding.expires_at:
        return f"expired {finding.expires_at:%Y-%m-%d} (source retention limit)"
    if finding.observed_at is None:
        return None
    max_age = policy.max_evidence_age_days.get(finding.source_type, policy.default_max_evidence_age_days)
    age_days = (now - finding.observed_at).total_seconds() / 86400
    if age_days > max_age:
        return f"observed {int(age_days)} days ago; {finding.source_type} evidence is stale after {max_age} days"
    return None


@dataclass
class SignalResult:
    signal: Signal
    state: SignalState
    points: float = 0.0
    finding: Finding | None = None
    # Other findings for this signal and why they were not used.
    set_aside: list[tuple[Finding, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    conflict: bool = False

    @property
    def covered(self) -> bool:
        return self.state in COVERED_STATES

    @property
    def strength(self) -> float:
        return self.points * (self.finding.confidence if self.finding else 0.0)


@dataclass
class CategoryScore:
    key: str
    label: str
    score: float
    max: float
    coverage: float
    upper_bound: float


@dataclass
class ScoreCard:
    total: float
    upper_bound: float
    categories: dict[str, CategoryScore]
    coverage: float
    confidence: float
    tier: str
    provisional_tier: str
    needs_review: bool
    review_reasons: list[str]
    results: list[SignalResult]
    top_findings: list[SignalResult]
    next_step: str
    source_links: list[str]

    def result(self, key: str) -> SignalResult:
        return next(r for r in self.results if r.signal.key == key)


TIER_LABELS = {
    "A": "A - Priority",
    "B": "B - Good",
    "C": "C - Nurture",
    "D": "D - Low",
    "review": "Needs manual review",
    "not_a_fit": "Not a fit",
    "do_not_contact": "Do not contact",
}


def _select(signal: Signal, findings: list[Finding], now: datetime, policy: ScoringPolicy) -> SignalResult:
    if not findings:
        return SignalResult(signal, SignalState.UNKNOWN, notes=["no finding recorded"])

    set_aside: list[tuple[Finding, str]] = []
    unknowns, usable, low_conf = [], [], []
    saw_missing = saw_stale = False
    for f in findings:
        problems = evidence_problems(f, policy)
        if f.outcome == Outcome.UNKNOWN:
            if problems:
                set_aside.append((f, "invalid: " + "; ".join(problems)))
            else:
                unknowns.append(f)
            continue
        if problems:
            saw_missing = True
            set_aside.append((f, "not scored, " + "; ".join(problems)))
            continue
        stale = staleness(f, now, policy)
        if stale:
            saw_stale = True
            set_aside.append((f, "not scored, stale: " + stale))
            continue
        if f.confidence < policy.min_finding_confidence:
            low_conf.append(f)
            set_aside.append(
                (f, f"not scored, confidence {f.confidence:.2f} below {policy.min_finding_confidence:.2f}")
            )
            continue
        usable.append(f)

    def rank(f: Finding):
        return (
            policy.source_priority.get(f.source_type, 1),
            f.observed_at.timestamp() if f.observed_at else 0,
            f.confidence,
        )

    if usable:
        usable.sort(key=rank, reverse=True)
        chosen = usable[0]
        result = SignalResult(signal, SignalState.COUNTED if chosen.outcome == Outcome.MET else SignalState.NOT_MET,
                              finding=chosen)
        for other in usable[1:]:
            if other.outcome == chosen.outcome:
                set_aside.append((other, "duplicate of the chosen finding; not counted again"))
            else:
                set_aside.append((other, f"disagrees with the chosen {chosen.source_type} finding"))
                if other.source_type != chosen.source_type and chosen.source_type != "manual":
                    result.conflict = True
        for f in unknowns:
            set_aside.append((f, "unknown result; a determinate finding is available"))
        result.set_aside = set_aside
        if result.state == SignalState.COUNTED:
            result.points = signal.max_points
        if result.conflict:
            result.notes.append("conflicting findings from different sources")
        return result

    set_aside.extend((f, "unknown result") for f in unknowns)
    if low_conf:
        state = SignalState.LOW_CONFIDENCE
    elif saw_missing:
        state = SignalState.MISSING_EVIDENCE
    elif saw_stale:
        state = SignalState.STALE
    else:
        state = SignalState.UNKNOWN
    result = SignalResult(signal, state, set_aside=set_aside)
    if unknowns and state == SignalState.UNKNOWN:
        latest = max(unknowns, key=lambda f: f.observed_at.timestamp() if f.observed_at else 0)
        result.finding = latest
        if latest.observation:
            result.notes.append(latest.observation)
    return result


def _apply_supersession_and_caps(results: dict[str, SignalResult]) -> None:
    for r in list(results.values()):
        if r.state != SignalState.COUNTED:
            continue
        for key in r.signal.supersedes:
            other = results[key]
            other.notes.append(f"superseded by '{r.signal.label}'")
            other.state = SignalState.SUPERSEDED
            other.points = 0.0

    for group in GROUPS.values():
        members = [r for r in results.values() if r.signal.group == group.key and r.state == SignalState.COUNTED]
        members.sort(key=lambda r: (-r.points, -(r.finding.confidence if r.finding else 0), r.signal.key))
        remaining = group.cap
        for r in members:
            awarded = min(r.points, remaining)
            if awarded < r.points:
                r.notes.append(
                    f"reduced from {r.points:g} to {awarded:g}: shares the '{group.label}' cap of {group.cap:g}"
                )
            r.points = awarded
            remaining -= awarded


def _category_totals(results: dict[str, SignalResult]) -> dict[str, float]:
    totals = {c: 0.0 for c in CATEGORY_MAX}
    for r in results.values():
        totals[r.signal.category] += r.points
    return {c: min(v, CATEGORY_MAX[c]) for c, v in totals.items()}


def _hypothetical_totals(results: dict[str, SignalResult], assume_superseders: bool) -> dict[str, float]:
    """Category totals if every uncovered signal turned out to be met."""
    trial: dict[str, SignalResult] = {}
    for key, r in results.items():
        state, points = r.state, r.points
        if r.state == SignalState.SUPERSEDED:
            state, points = SignalState.UNKNOWN, 0.0  # recomputed below
        if not r.covered or r.state == SignalState.SUPERSEDED:
            if r.signal.supersedes and not assume_superseders:
                state, points = SignalState.NOT_MET, 0.0
            else:
                state, points = SignalState.COUNTED, r.signal.max_points
        elif r.state == SignalState.COUNTED:
            points = r.signal.max_points
        trial[key] = SignalResult(r.signal, state, points, r.finding)
    _apply_supersession_and_caps(trial)
    return _category_totals(trial)


def _round(x: float) -> float:
    return round(x + 1e-9, 1)


def score_findings(
    findings: list[Finding],
    *,
    now: datetime | None = None,
    opted_out: bool = False,
    has_website: bool | None = None,
    website_check_failed: str | None = None,
    policy: ScoringPolicy = DEFAULT_POLICY,
) -> ScoreCard:
    """Score one business from its findings."""
    now = now or utcnow()
    by_signal: dict[str, list[Finding]] = {s.key: [] for s in SIGNALS}
    ignored = []
    for f in findings:
        if f.signal_key in by_signal:
            by_signal[f.signal_key].append(f)
        else:
            ignored.append(f)

    results = {s.key: _select(s, by_signal[s.key], now, policy) for s in SIGNALS}
    _apply_supersession_and_caps(results)
    totals = _category_totals(results)

    upper_a = _hypothetical_totals(results, assume_superseders=False)
    upper_b = _hypothetical_totals(results, assume_superseders=True)
    upper = {c: max(totals[c], upper_a[c], upper_b[c]) for c in CATEGORY_MAX}

    categories: dict[str, CategoryScore] = {}
    for c, cmax in CATEGORY_MAX.items():
        members = [r for r in results.values() if r.signal.category == c]
        weight = sum(r.signal.max_points for r in members)
        covered = sum(r.signal.max_points for r in members if r.covered)
        categories[c] = CategoryScore(
            c, CATEGORY_LABELS[c], _round(totals[c]), cmax, round(covered / weight, 3) if weight else 0.0,
            _round(upper[c]),
        )
    coverage = round(sum(cs.coverage * cs.max for cs in categories.values()) / sum(CATEGORY_MAX.values()), 3)

    conf_pairs = [
        (r.finding.confidence, r.signal.max_points)
        for r in results.values()
        if r.state in (SignalState.COUNTED, SignalState.NOT_MET) and r.finding
    ]
    weight_sum = sum(w for _, w in conf_pairs)
    confidence = round(sum(c * w for c, w in conf_pairs) / weight_sum, 3) if weight_sum else 0.0

    total = _round(sum(totals.values()))
    upper_total = _round(sum(upper.values()))

    review_reasons = _review_reasons(results, categories, coverage, confidence, policy)
    provisional = next(name for name, minimum in policy.tiers if total >= minimum)
    roofing = results["roofing_core_service"]
    if opted_out:
        tier = "do_not_contact"
    elif roofing.state == SignalState.NOT_MET:
        tier = "not_a_fit"
    elif review_reasons:
        tier = "review"
    else:
        tier = provisional

    ordered = list(results.values())
    top = sorted((r for r in ordered if r.state == SignalState.COUNTED and r.points > 0),
                 key=lambda r: (-r.strength, -r.points, r.signal.key))[:3]
    links = []
    for r in sorted(ordered, key=lambda r: -r.points):
        if r.state in (SignalState.COUNTED, SignalState.NOT_MET) and r.finding and r.finding.source_url and r.finding.source_url not in links:
            links.append(r.finding.source_url)

    return ScoreCard(
        total=total,
        upper_bound=upper_total,
        categories=categories,
        coverage=coverage,
        confidence=confidence,
        tier=tier,
        provisional_tier=provisional,
        needs_review=tier == "review",
        review_reasons=review_reasons,
        results=ordered,
        top_findings=top,
        next_step=_next_step(tier, results, has_website,
                             scanned=any(f.source_type == "website_scan" for f in findings),
                             check_failed=website_check_failed),
        source_links=links,
    )


def _review_reasons(results, categories, coverage, confidence, policy: ScoringPolicy) -> list[str]:
    reasons = []
    if coverage < policy.min_coverage:
        reasons.append(f"evidence coverage {coverage:.0%} is below {policy.min_coverage:.0%}")
    for c, minimum in policy.min_category_coverage.items():
        if categories[c].coverage < minimum:
            reasons.append(f"{CATEGORY_LABELS[c]} coverage {categories[c].coverage:.0%} is below {minimum:.0%}")
    if coverage > 0 and confidence < policy.min_confidence:
        reasons.append(f"average confidence {confidence:.2f} is below {policy.min_confidence:.2f}")
    for state, text in (
        (SignalState.MISSING_EVIDENCE, "finding lacks required evidence"),
        (SignalState.LOW_CONFIDENCE, "finding below the confidence threshold"),
        (SignalState.STALE, "evidence is stale"),
    ):
        labels = [r.signal.label for r in results.values() if r.state == state]
        if labels:
            reasons.append(f"{text}: {', '.join(labels)}")
    conflicts = [r.signal.label for r in results.values() if r.conflict]
    if conflicts:
        reasons.append("conflicting findings: " + ", ".join(conflicts))
    return reasons


def _next_step(tier: str, results: dict[str, SignalResult], has_website, scanned: bool = False,
               check_failed: str | None = None) -> str:
    if tier == "do_not_contact":
        return "Do not contact. Keep this business on the suppression list."
    if tier == "not_a_fit":
        return "Confirm the business type; archive the prospect if it is not a roofing contractor."

    def by_state(state: SignalState) -> list[SignalResult]:
        return [r for r in results.values() if r.state == state]

    for r in by_state(SignalState.MISSING_EVIDENCE):
        return f"Add a source URL and observation for '{r.signal.label}', or delete that finding."
    for r in results.values():
        if r.conflict:
            return f"Resolve conflicting findings for '{r.signal.label}': {r.signal.research_step}"
    if check_failed:
        return (f"The last website check failed ({check_failed}). Retry it later; if it keeps failing, open the "
                "site in a browser and record what you see as a manual finding.")
    website_signals = {"no_lead_form", "no_click_to_call", "missing_mobile_viewport", "roofing_core_service"}
    stale = by_state(SignalState.STALE)
    if stale:
        if any(r.signal.key in website_signals for r in stale) and has_website:
            return "Re-run the website scan; the previous scan is older than the freshness limit."
        r = max(stale, key=lambda r: r.signal.max_points)
        return f"Re-check '{r.signal.label}' (evidence is stale): {r.signal.research_step}"
    low = by_state(SignalState.LOW_CONFIDENCE)
    if low:
        r = max(low, key=lambda r: r.signal.max_points)
        return f"Verify '{r.signal.label}' manually: {r.signal.research_step}"
    if results["no_working_website"].state == SignalState.COUNTED:
        return ("Open the website in a browser to confirm it is really down, then check the state license record "
                "or Google profile to confirm the business is still operating.")
    unknown = [r for r in results.values() if r.state == SignalState.UNKNOWN]
    if unknown:
        if has_website and not scanned and any(r.signal.key in website_signals for r in unknown):
            return "Run the website scan to collect conversion, contact and mobile signals."
        if has_website is False and results["no_working_website"].state == SignalState.UNKNOWN:
            return results["no_working_website"].signal.research_step
        r = max(unknown, key=lambda r: r.signal.max_points)
        return f"Research '{r.signal.label}': {r.signal.research_step}"
    top = max((r for r in results.values() if r.state == SignalState.COUNTED), key=lambda r: r.strength,
              default=None)
    if top and top.finding:
        return f"Manually confirm '{top.signal.label}' at {top.finding.source_url} before outreach."
    return "Review the evidence and decide whether to contact this business."
