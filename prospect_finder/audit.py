"""Short, personalized prospect audits built only from verified findings.

The audit is assembled from fixed sentence templates, one per marketing-
opportunity signal, filled with the business name, the checked URL and the
date. It contains no estimates of revenue, ad spend, lead volume, response
times or rankings, because the app never collects those.

An optional rewrite with Claude can smooth the wording. The rewrite receives
only the template text, and its output is rejected (falling back to the
template) if it introduces numbers, money, percentages or claims about
revenue, budgets, leads, rankings or response times that were not in the input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Business
from .rubric import MARKETING_OPPORTUNITY
from .scoring import ScoreCard, SignalResult, SignalState
from .sources import GOOGLE_PLACES

FACTS = {
    "no_working_website": "Your website ({url}) did not load when we tried it twice.",
    "no_lead_form": "We couldn't find a quote or contact form on your site ({url}).",
    "no_click_to_call": "The phone number on your homepage isn't a tap-to-call link.",
    "no_clear_cta": "Your homepage doesn't have a clear button to request an estimate or inspection.",
    "missing_mobile_viewport": "Your homepage isn't set up to fit phone screens (it has no mobile viewport setting).",
    "no_https": "The secure https:// version of your site didn't load, so browsers may label it 'Not secure'.",
    "no_ad_conversion_tracking": "We didn't see ad conversion tracking (such as a Google Ads or Meta tag) on your "
                                 "homepage.",
    "no_analytics": "We didn't see a website analytics tool on your homepage.",
    "no_chat_or_booking": "We didn't find a chat, text-us or online booking option on your site.",
    "review_generation_gap": "Your Google Business Profile showed fewer than 25 reviews when we looked.",
}
OPT_OUT_LINE = "If you'd rather not hear from me, just reply \"no thanks\" and I won't contact you again."
FORBIDDEN = re.compile(
    r"revenue|ad spend|budget|leads? per|lead volume|conversion rate|roi\b|return on|rank(?:ing|ed)?\b|"
    r"position|response time|respond within|competitor|losing|lost (?:jobs|customers|money)|guarantee|"
    r"\$|%|per (?:month|week|year)", re.I)


class AuditRefused(Exception):
    pass


@dataclass
class Audit:
    subject: str
    body: str
    used: list[SignalResult]
    warnings: list[str] = field(default_factory=list)
    generated_by: str = "template"

    @property
    def text(self) -> str:
        return f"Subject: {self.subject}\n\n{self.body}"


def eligible_findings(card: ScoreCard) -> list[SignalResult]:
    """Counted marketing-opportunity findings that may be shown to the prospect."""
    out = []
    for r in card.results:
        if r.signal.category != MARKETING_OPPORTUNITY or r.state != SignalState.COUNTED or r.points <= 0:
            continue
        if not r.finding or r.finding.source_type == GOOGLE_PLACES:
            continue  # Google Places content stays inside the app.
        if r.signal.key in FACTS:
            out.append(r)
    out.sort(key=lambda r: (-r.strength, r.signal.key))
    return out


def build_audit(b: Business, card: ScoreCard, opted_out: bool = False, sender: str = "") -> Audit:
    if opted_out:
        raise AuditRefused("This prospect has opted out; no audit will be generated.")
    findings = eligible_findings(card)[:3]
    if not findings:
        raise AuditRefused("There are no verified marketing findings to report yet. Run the website scan or "
                           "record manual findings with evidence first.")
    warnings = []
    if card.needs_review:
        warnings.append("Draft only - this prospect still needs manual review: " + "; ".join(card.review_reasons))
    inferred = [r.signal.label for r in findings if r.finding.evidence_type.value == "inferred"]
    if inferred:
        warnings.append("Based on inference (verify before sending): " + ", ".join(inferred))

    name = b.name or "there"
    dates = sorted({f"{r.finding.observed_at:%B} {r.finding.observed_at.day}, {r.finding.observed_at.year}"
                    for r in findings})
    site = b.website or findings[0].finding.source_url
    lines = [f"Hi {name} team,", "",
             f"I took a quick look at {site} on {' and '.join(dates)}. A few things stood out that may make it "
             "harder for homeowners to reach you:", ""]
    for i, r in enumerate(findings, 1):
        fact = FACTS[r.signal.key].format(url=r.finding.source_url)
        lines.append(f"{i}. {fact} {r.signal.audit_why}")
    lines += ["",
              "This is based only on what was publicly visible when I checked. Automated checks can miss things that "
              "load after the page opens, so tell me if I've got anything wrong.",
              "",
              "If it's useful, I can send a short plan for fixing these.",
              OPT_OUT_LINE]
    if sender:
        lines += ["", sender]
    return Audit(subject=f"A few quick website notes for {name}", body="\n".join(lines), used=findings,
                 warnings=warnings)


def rewrite_problems(original: str, rewritten: str) -> list[str]:
    """Reasons a model rewrite must be discarded."""
    problems = []
    original_numbers = set(re.findall(r"\d+", original))
    new_numbers = set(re.findall(r"\d+", rewritten)) - original_numbers
    if new_numbers:
        problems.append(f"introduced numbers {sorted(new_numbers)}")
    for m in FORBIDDEN.finditer(rewritten):
        if not re.search(re.escape(m.group(0)), original, re.I):
            problems.append(f"introduced claim term '{m.group(0)}'")
    if "won't contact you again" not in rewritten:
        problems.append("dropped the opt-out sentence")
    if len(rewritten) > len(original) * 1.3:
        problems.append("longer than the original")
    return problems


# Models that accept the server-side refusal fallback parameter.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
SYSTEM_PROMPT = (
    "You edit short outreach emails from a marketing agency to a roofing contractor. Rewrite the email so it reads "
    "naturally and concisely. Keep every factual statement exactly as specific as it is; do not add facts, "
    "numbers, statistics, prices, percentages, comparisons with competitors, or claims about revenue, ad spend, "
    "lead volume, rankings or response times. Keep the numbered list, the URL, the date and the final opt-out "
    "sentence word for word. Return only the email body."
)


def polish_with_claude(audit: Audit, api_key: str, model: str, client=None) -> Audit:
    """Optionally rewrite the body with Claude; keep the template if the rewrite fails validation."""
    try:
        import anthropic
    except ImportError:
        audit.warnings.append("Claude rewrite skipped: install the 'anthropic' package to enable it.")
        return audit
    client = client or anthropic.Anthropic(api_key=api_key)
    request = dict(model=model, max_tokens=16000, system=SYSTEM_PROMPT, output_config={"effort": "low"},
                   messages=[{"role": "user", "content": audit.body}])
    try:
        if model in FALLBACK_MODELS:
            response = client.beta.messages.create(**request, betas=["server-side-fallback-2026-07-01"],
                                                   fallbacks="default")
        else:
            response = client.messages.create(**request)
    except anthropic.APIConnectionError:
        audit.warnings.append("Claude rewrite skipped: could not reach the API.")
        return audit
    except anthropic.APIStatusError as exc:
        audit.warnings.append(f"Claude rewrite skipped: API error {exc.status_code}.")
        return audit
    if response.stop_reason == "refusal":
        audit.warnings.append("Claude rewrite declined; using the template text.")
        return audit
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    problems = rewrite_problems(audit.body, text)
    if problems:
        audit.warnings.append("Claude rewrite rejected (" + "; ".join(problems) + "); using the template text.")
        return audit
    audit.body = text
    audit.generated_by = f"template rewritten by {model}"
    return audit
