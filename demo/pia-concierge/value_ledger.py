"""Per-work-product value ledger — the partner-calibrated baselines.

Hours saved are keyed to the WORK PRODUCT (baseline human hours × automation
saving), split by audience: the privacy team's analysis hours and the
business respondent's time. The per-assessment figures already subsume the
analysis auto-fill, so the only per-answer credit is the business side's
6 minutes per question the agent fills or skips (no double-count).

Standalone-safe: stdlib only. Rates and baselines are env-overridable so a
client can put their OWN numbers in front of their CFO.
"""
from __future__ import annotations

import os


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, "") or default)
    except ValueError:
        return default


HOURLY_RATE = _f("VALUE_HOURLY_RATE", 60.0)

# Privacy-team hours saved per work product: baseline × automation share.
WORK_PRODUCT_HOURS = {
    "privacy_assessment": _f("VALUE_PRIVACY_ASSESSMENT_HOURS", 11.25),  # 25h × 45%
    "ai_assessment": _f("VALUE_AI_ASSESSMENT_HOURS", 10.0),             # 20h × 50%
    "contract_review": _f("VALUE_CONTRACT_HOURS", 7.0),                 # 10h × 70%
    "notice_review": _f("VALUE_NOTICE_HOURS", 3.0),                     # per cross-check
}
BUSINESS_FLAT_HOURS = 0.5          # fewer touchpoints per assessment
BUSINESS_PER_ANSWER_HOURS = 0.1    # 6 min per answer filled/skipped/ingested


def case_value(*, kind: str = "privacy_assessment",
               answers_autofilled: int = 0,
               contract_drafted: bool = False,
               notice_checked: bool = False) -> dict:
    """The dollars-and-hours story for one completed case, split by
    audience. Everything here is a counted event, not an estimate of one."""
    privacy_hours = WORK_PRODUCT_HOURS.get(
        kind, WORK_PRODUCT_HOURS["privacy_assessment"])
    breakdown = [{"item": kind.replace("_", " "), "audience": "privacy team",
                  "hours": privacy_hours}]
    if contract_drafted:
        privacy_hours += WORK_PRODUCT_HOURS["contract_review"]
        breakdown.append({"item": "contract instrument",
                          "audience": "privacy team",
                          "hours": WORK_PRODUCT_HOURS["contract_review"]})
    if notice_checked:
        privacy_hours += WORK_PRODUCT_HOURS["notice_review"]
        breakdown.append({"item": "notice cross-check",
                          "audience": "privacy team",
                          "hours": WORK_PRODUCT_HOURS["notice_review"]})
    business_hours = (BUSINESS_FLAT_HOURS
                      + BUSINESS_PER_ANSWER_HOURS * max(0, answers_autofilled))
    breakdown.append({"item": f"respondent time ({answers_autofilled} "
                              f"answers auto-filled)",
                      "audience": "business", "hours": round(business_hours, 2)})
    total_hours = privacy_hours + business_hours
    return {"privacy_hours": round(privacy_hours, 2),
            "business_hours": round(business_hours, 2),
            "hours": round(total_hours, 2),
            "dollars": round(total_hours * HOURLY_RATE, 2),
            "rate": HOURLY_RATE,
            "breakdown": breakdown}
