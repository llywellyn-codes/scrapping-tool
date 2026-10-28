from datetime import timedelta

import pytest

from prospect_finder.models import EvidenceType, Outcome
from prospect_finder.rubric import CATEGORY_MAX, SIGNALS, achievable_max, validate_rubric
from prospect_finder.scoring import SignalState, evidence_problems, score_findings

from .helpers import NOW, all_signals, finding


def score(findings, **kwargs):
    return score_findings(findings, now=NOW, **kwargs)


# ---------------------------------------------------------------- rubric


def test_rubric_is_internally_consistent():
    assert validate_rubric() == []
    assert CATEGORY_MAX == {"business_fit": 30, "marketing_opportunity": 50, "commercial_readiness": 20}
    for category, budget in CATEGORY_MAX.items():
        assert achievable_max(category) == budget


# ---------------------------------------------------------------- totals and categories


def test_every_signal_met_scores_exactly_100():
    card = score(all_signals())
    assert card.total == 100
    assert {k: c.score for k, c in card.categories.items()} == CATEGORY_MAX
    assert card.coverage == 1.0
    assert card.tier == "A"
    assert not card.review_reasons


def test_nothing_met_scores_zero_but_is_fully_covered():
    findings = all_signals({s.key: Outcome.NOT_MET for s in SIGNALS})
    card = score(findings)
    assert card.total == 0
    assert card.coverage == 1.0
    assert card.tier == "not_a_fit"  # roofing_core_service is not met


def test_category_scores_add_up_to_total():
    findings = [
        finding("roofing_core_service"),
        finding("residential_services", Outcome.NOT_MET),
        finding("no_lead_form"),
        finding("financing_offered"),
    ]
    card = score(findings)
    assert card.categories["business_fit"].score == 10
    assert card.categories["marketing_opportunity"].score == 10
    assert card.categories["commercial_readiness"].score == 4
    assert card.total == 24


# ---------------------------------------------------------------- unknown values


def test_no_findings_means_zero_score_zero_coverage_and_review():
    card = score([])
    assert card.total == 0
    assert card.coverage == 0
    assert card.confidence == 0
    assert card.tier == "review"
    assert card.upper_bound == 100
    assert all(r.state == SignalState.UNKNOWN for r in card.results)


def test_unknown_findings_are_excluded_from_score_and_coverage():
    findings = all_signals()
    findings = [f if f.signal_key != "no_lead_form" else
                finding("no_lead_form", Outcome.UNKNOWN, observation="robots.txt disallows /contact")
                for f in findings]
    card = score(findings)
    result = card.result("no_lead_form")
    assert result.state == SignalState.UNKNOWN
    assert result.points == 0
    assert not result.covered
    # Conversion path still reaches its 17-point cap through the other two signals? 6 + 5 = 11.
    assert card.categories["marketing_opportunity"].score == 50 - 17 + 11
    assert card.categories["marketing_opportunity"].coverage == pytest.approx(1 - 10 / 86, abs=1e-3)
    assert "robots.txt disallows /contact" in result.notes


def test_coverage_is_weighted_by_category_budget():
    # Only business fit is fully determined: 30% of the 100-point model.
    findings = [finding(s.key) for s in SIGNALS if s.category == "business_fit"]
    card = score(findings)
    assert card.categories["business_fit"].coverage == 1.0
    assert card.coverage == 0.3
    assert card.tier == "review"
    assert any("coverage 30%" in r for r in card.review_reasons)


def test_upper_bound_assumes_unknowns_are_met():
    findings = [finding("roofing_core_service"), finding("no_working_website", Outcome.NOT_MET)]
    card = score(findings)
    assert card.total == 10
    assert card.upper_bound == 100
    # Website unknown: best case is the working-website path (50), not the no-website path (36).
    assert score([finding("roofing_core_service")]).categories["marketing_opportunity"].upper_bound == 50


# ---------------------------------------------------------------- confidence


def test_low_confidence_finding_is_not_scored_and_flags_review():
    findings = all_signals()
    findings = [f.with_(confidence=0.4) if f.signal_key == "no_lead_form" else f for f in findings]
    card = score(findings)
    result = card.result("no_lead_form")
    assert result.state == SignalState.LOW_CONFIDENCE
    assert result.points == 0
    assert card.tier == "review"
    assert card.provisional_tier == "A"
    assert any("confidence threshold" in r for r in card.review_reasons)
    assert "Verify 'No lead-capture form found' manually" in card.next_step


def test_overall_confidence_is_weighted_average():
    findings = [finding("roofing_core_service", confidence=1.0), finding("social_proof", confidence=0.5)]
    card = score(findings)
    assert card.confidence == pytest.approx((1.0 * 10 + 0.5 * 4) / 14, abs=1e-3)


def test_out_of_range_confidence_is_rejected():
    assert "outside 0-1" in evidence_problems(finding("social_proof", confidence=1.5))[0]


# ---------------------------------------------------------------- stale evidence


def test_stale_website_evidence_is_not_scored():
    findings = all_signals(age_days=120)  # website_scan evidence is stale after 90 days
    card = score(findings, has_website=True)
    assert card.total == 0
    assert all(r.state == SignalState.STALE for r in card.results)
    assert card.tier == "review"
    assert any("stale" in r for r in card.review_reasons)
    assert card.next_step.startswith("Re-run the website scan")


def test_fresh_finding_wins_over_stale_duplicate():
    card = score([finding("no_lead_form", age_days=200), finding("no_lead_form", age_days=3)])
    result = card.result("no_lead_form")
    assert result.state == SignalState.COUNTED
    assert result.points == 10
    assert any("stale" in why for _, why in result.set_aside)


def test_expired_restricted_source_finding_is_stale():
    expired = finding("review_generation_gap", source="google_places", age_days=2,
                      expires_at=NOW - timedelta(hours=1))
    result = score([expired]).result("review_generation_gap")
    assert result.state == SignalState.STALE
    assert result.points == 0


def test_manual_evidence_has_longer_freshness_window():
    result = score([finding("licensed_insured", source="manual", age_days=120)]).result("licensed_insured")
    assert result.state == SignalState.COUNTED


# ---------------------------------------------------------------- duplicates and double counting


def test_duplicate_findings_for_one_signal_are_counted_once():
    card = score([finding("no_lead_form"), finding("no_lead_form", age_days=2), finding("no_lead_form", age_days=5)])
    result = card.result("no_lead_form")
    assert result.points == 10
    assert card.categories["marketing_opportunity"].score == 10
    assert sum("duplicate" in why for _, why in result.set_aside) == 2


def test_related_conversion_problems_share_one_cap():
    card = score([finding("no_lead_form"), finding("no_click_to_call"), finding("no_clear_cta")])
    assert card.categories["marketing_opportunity"].score == 17  # not 10 + 6 + 5 = 21
    capped = card.result("no_clear_cta")
    assert capped.points == 1
    assert any("cap of 17" in n for n in capped.notes)


def test_tracking_problems_share_one_cap():
    card = score([finding("no_ad_conversion_tracking"), finding("no_analytics")])
    assert card.categories["marketing_opportunity"].score == 10  # not 8 + 4


def test_no_working_website_supersedes_website_problems():
    findings = [
        finding("no_working_website", evidence=EvidenceType.CONFIRMED_FAILURE),
        finding("no_lead_form"),
        finding("missing_mobile_viewport"),
        finding("no_https"),
        finding("review_generation_gap", source="manual"),
    ]
    card = score(findings)
    assert card.categories["marketing_opportunity"].score == 36  # 30 + 6, website problems not added
    for key in ("no_lead_form", "missing_mobile_viewport", "no_https", "no_analytics"):
        r = card.result(key)
        assert r.state == SignalState.SUPERSEDED and r.points == 0 and r.covered


def test_manual_check_beats_newer_automated_finding():
    manual = finding("no_lead_form", Outcome.NOT_MET, source="manual", age_days=10,
                     observation="Form is loaded by JavaScript; visible in browser")
    scan = finding("no_lead_form", Outcome.MET, age_days=1)
    result = score([scan, manual]).result("no_lead_form")
    assert result.state == SignalState.NOT_MET
    assert result.finding.source_type == "manual"
    assert not result.conflict


def test_disagreeing_automated_sources_flag_a_conflict():
    a = finding("review_generation_gap", Outcome.MET, source="google_places", age_days=1)
    b = finding("review_generation_gap", Outcome.NOT_MET, source="csv_import", age_days=2)
    card = score([a, b])
    assert card.result("review_generation_gap").conflict
    assert any("conflicting" in r for r in card.review_reasons)


def test_newer_scan_replaces_older_scan_without_conflict():
    old = finding("no_lead_form", Outcome.MET, age_days=30)
    new = finding("no_lead_form", Outcome.NOT_MET, age_days=1)
    result = score([old, new]).result("no_lead_form")
    assert result.state == SignalState.NOT_MET
    assert not result.conflict


# ---------------------------------------------------------------- missing sources / evidence


@pytest.mark.parametrize(
    "overrides, problem",
    [
        ({"url": None}, "missing source URL"),
        ({"url": "not a url"}, "missing source URL"),
        ({"observation": "  "}, "missing observation"),
        ({"test_result": None}, "missing test name/result"),
    ],
)
def test_met_finding_without_required_evidence_scores_zero(overrides, problem):
    f = finding("no_lead_form", **overrides)
    assert problem in evidence_problems(f)
    card = score([f])
    result = card.result("no_lead_form")
    assert result.state == SignalState.MISSING_EVIDENCE
    assert result.points == 0
    assert any("lacks required evidence" in r for r in card.review_reasons)
    assert "Add a source URL" in card.next_step


def test_missing_timestamp_is_rejected():
    f = finding("no_lead_form").with_(observed_at=None)
    assert "missing timestamp" in evidence_problems(f)
    assert score([f]).result("no_lead_form").points == 0


def test_confirmed_failure_requires_test_result_even_for_manual_source():
    f = finding("no_working_website", evidence=EvidenceType.CONFIRMED_FAILURE, source="manual", test_result=None)
    assert "missing test name/result" in evidence_problems(f)


def test_manual_observation_does_not_need_test_result():
    f = finding("licensed_insured", source="manual", test_name=None, test_result=None)
    assert evidence_problems(f) == []


def test_not_met_findings_also_need_evidence_to_count_toward_coverage():
    f = finding("no_lead_form", Outcome.NOT_MET, url=None)
    result = score([f]).result("no_lead_form")
    assert result.state == SignalState.MISSING_EVIDENCE
    assert not result.covered


def test_met_finding_with_unknown_evidence_type_is_invalid():
    f = finding("no_lead_form", evidence=EvidenceType.UNKNOWN)
    assert any("cannot have evidence type 'unknown'" in p for p in evidence_problems(f))


# ---------------------------------------------------------------- tiers and outputs


@pytest.mark.parametrize(
    "unmet, expected_tier",
    [
        (set(), "A"),
        ({"no_lead_form", "no_click_to_call", "no_clear_cta", "missing_mobile_viewport",
          "no_https", "no_ad_conversion_tracking", "no_analytics"}, "A"),  # 100 - 38 = 62
        ({"no_lead_form", "no_click_to_call", "no_clear_cta", "missing_mobile_viewport", "no_https",
          "no_ad_conversion_tracking", "no_analytics", "no_chat_or_booking", "review_generation_gap"}, "B"),  # 50
        ({s.key for s in SIGNALS if s.key not in ("roofing_core_service", "residential_services",
                                                    "no_lead_form")}, "C"),  # 26
        ({s.key for s in SIGNALS if s.key != "roofing_core_service"}, "D"),  # 10
    ],
)
def test_priority_tiers(unmet, expected_tier):
    card = score(all_signals({k: Outcome.NOT_MET for k in unmet}))
    assert card.tier == expected_tier


def test_opted_out_prospect_is_do_not_contact():
    card = score(all_signals(), opted_out=True)
    assert card.tier == "do_not_contact"
    assert card.next_step.startswith("Do not contact")


def test_top_three_findings_are_strongest_counted():
    card = score(all_signals())
    assert len(card.top_findings) == 3
    assert [r.signal.key for r in card.top_findings][0] == "no_lead_form"
    assert all(r.state == SignalState.COUNTED and r.points > 0 for r in card.top_findings)


def test_source_links_are_unique_and_from_used_findings():
    findings = [finding("roofing_core_service", url="https://a.test/"),
                finding("no_lead_form", url="https://a.test/contact"),
                finding("social_proof", url="https://a.test/"),
                finding("financing_offered", url=None)]  # rejected, so its (missing) URL is not listed
    card = score(findings)
    assert card.source_links == ["https://a.test/", "https://a.test/contact"]


def test_findings_for_unknown_signal_keys_are_ignored():
    card = score([finding("made_up_signal")])
    assert card.total == 0


def test_next_step_for_unreachable_site_is_a_manual_check():
    card = score([finding("no_working_website", evidence=EvidenceType.CONFIRMED_FAILURE)], has_website=True)
    assert card.next_step.startswith("Open the website in a browser")


def test_next_step_suggests_scan_only_when_not_yet_scanned():
    assert score([], has_website=True).next_step.startswith("Run the website scan")
    scanned = [finding("no_lead_form", Outcome.UNKNOWN, observation="Access restricted")]
    assert not score(scanned, has_website=True).next_step.startswith("Run the website scan")


def test_scoring_doc_matches_rubric():
    from pathlib import Path

    from prospect_finder.rubric import rubric_markdown

    doc = (Path(__file__).parent.parent / "docs" / "SCORING.md").read_text()
    assert rubric_markdown() in doc, "docs/SCORING.md is out of date; regenerate the signal table"
