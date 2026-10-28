"""Website check state per prospect, and which prospects a bulk run should check.

States (one per prospect):
  unevaluated - never checked
  no_website  - nothing to check (no URL on record when last tried)
  evaluated   - last check completed (page read, or access restricted) and is still fresh
  stale       - last completed check is older than the website evidence freshness limit
  failed      - the last attempt failed (network error, timeout, server error, rate limit)

"Fresh" uses the same limit the scoring engine uses for website evidence, so a
prospect becomes eligible for re-checking exactly when its findings go stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import Business
from .rubric import DEFAULT_POLICY
from .sources import WEBSITE_SCAN

STALE_AFTER_DAYS = DEFAULT_POLICY.max_evidence_age_days[WEBSITE_SCAN]
CHECK_STATES = ("unevaluated", "no_website", "evaluated", "stale", "failed")


@dataclass(frozen=True)
class CheckRules:
    failed_retry_hours: float = 24      # wait this long before a failed check is retried automatically
    max_failed_attempts: int = 3        # after this many failures in a row, only "Check selected" retries it
    stale_after_days: int = STALE_AFTER_DAYS


def check_state(check: dict | None, now: datetime, rules: CheckRules = CheckRules()) -> str:
    if not check:
        return "unevaluated"
    status = check["status"]
    if status == "failed":
        return "failed"
    if status == "no_website":
        return "no_website" if not check.get("checked_at") else _age_state(check, now, rules)
    return _age_state(check, now, rules)


def _age_state(check: dict, now: datetime, rules: CheckRules) -> str:
    checked = check.get("checked_at")
    if checked is None:
        return "unevaluated"
    return "stale" if now - checked > timedelta(days=rules.stale_after_days) else "evaluated"


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reason: str = ""      # short code: opted_out, no_website, fresh, failed_recently, failed_too_often
    detail: str = ""


def eligibility(b: Business, check: dict | None, opted_out: bool, now: datetime, *, selected: bool = False,
                force: bool = False, rules: CheckRules = CheckRules()) -> Eligibility:
    """Should a bulk run check this prospect's website now?

    ``selected`` means the user picked it explicitly: failed checks are retried without waiting.
    ``force`` (selected only) also re-checks prospects whose last check is still fresh.
    """
    if opted_out:
        return Eligibility(False, "opted_out", "opted out")
    if not b.website:
        return Eligibility(False, "no_website", "no website on record")
    state = check_state(check, now, rules)
    if state in ("unevaluated", "no_website", "stale"):
        return Eligibility(True)
    if state == "evaluated":
        if selected and force:
            return Eligibility(True)
        return Eligibility(False, "fresh", f"checked {check['checked_at']:%Y-%m-%d}; still fresh")
    # failed
    if selected:
        return Eligibility(True)
    if check["consecutive_failures"] >= rules.max_failed_attempts:
        return Eligibility(False, "failed_too_often",
                           f"failed {check['consecutive_failures']} times in a row; select it to retry")
    retry_at = check["last_attempt_at"] + timedelta(hours=rules.failed_retry_hours)
    if now < retry_at:
        return Eligibility(False, "failed_recently", f"failed recently; retried automatically after "
                                                     f"{retry_at:%Y-%m-%d %H:%M} UTC")
    return Eligibility(True)


def priority_key(check: dict | None, now: datetime, rules: CheckRules = CheckRules()):
    """Order for bulk runs: never checked first, then failed retries, then the stalest."""
    state = check_state(check, now, rules)
    order = {"unevaluated": 0, "no_website": 0, "failed": 1, "stale": 2}.get(state, 3)
    when = (check or {}).get("checked_at") or (check or {}).get("last_attempt_at")
    return order, when.timestamp() if when else 0


REASON_LABELS = {
    "opted_out": "opted out",
    "no_website": "no website on record",
    "fresh": "checked recently (still fresh)",
    "failed_recently": "failed recently (waiting before retry)",
    "failed_too_often": "failed repeatedly (select to retry)",
}
