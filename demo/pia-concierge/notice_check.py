"""Privacy-notice cross-check — the assessment vs what the company PROMISED.

Reconciles a completed assessment against the published privacy notice and
finds two kinds of problems, deterministically:

* CONTRADICTIONS — the assessment conflicts with an affirmative promise in
  the notice (canonical: the assessment says data is SOLD while the notice
  says "we do not sell"). Critical governance findings; the notice is quoted
  verbatim, and a contradiction escalates the overall rating.
* GAPS — processing the notice never discloses (GDPR Art. 13/14 territory):
  personal-data categories the assessment records that the notice text never
  mentions.

Brand-first, cost-conscious selection: the contracting entity is the
authoritative brand signal; a unique name match against the notice list is
used deterministically (no model call). Standalone-safe: stdlib only.
"""
from __future__ import annotations

# promise marker in the notice  →  assessment fact that contradicts it
_PROMISES = (
    {"key": "no_sale",
     "notice_markers": ("do not sell", "never sell", "will not sell",
                        "don't sell"),
     "fact": "data_sold",
     "finding": "The assessment records personal data being sold or shared "
                "for value, but the published notice promises otherwise."},
    {"key": "no_third_party",
     "notice_markers": ("never share your", "do not share your",
                        "will not share your"),
     "fact": "shared_with_third_parties",
     "finding": "The assessment records third-party sharing the published "
                "notice rules out."},
)


def pick_notice(entity: str, notices: list[dict],
                default_name: str = "") -> tuple[dict | None, list[dict]]:
    """Brand-first: unique match of the contracting entity against the
    notice brand (organizationName) or title → deterministic pick. Returns
    (picked, siblings-for-manual-review). Falls back to the configured
    default notice; never auto-reviews siblings."""
    want = (entity or "").strip().lower()
    picked = None
    if want:
        hits = [n for n in notices
                if want in str(n.get("organizationName", "")).lower()
                or want in str(n.get("name", "")).lower()]
        if len(hits) == 1:
            picked = hits[0]
    if picked is None and default_name:
        defaults = [n for n in notices
                    if default_name.lower() in str(n.get("name", "")).lower()]
        picked = defaults[0] if defaults else None
    if picked is None and notices:
        picked = notices[0]
    siblings = [n for n in notices
                if picked and n.get("guid") != picked.get("guid")]
    return picked, siblings


def _quote(text: str, marker: str, width: int = 140) -> str:
    low = text.lower()
    at = low.find(marker)
    if at < 0:
        return ""
    lo = max(0, at - 40)
    return text[lo:lo + width].strip()


def cross_check(*, notice_text: str, facts: dict,
                data_categories: list[str]) -> dict:
    """The reconciliation. ``facts`` carries assessment-derived booleans
    (data_sold, shared_with_third_parties); ``data_categories`` are the
    personal-data category names the assessment recorded."""
    low = notice_text.lower()
    contradictions = []
    for p in _PROMISES:
        marker = next((m for m in p["notice_markers"] if m in low), None)
        if marker and facts.get(p["fact"]):
            contradictions.append({
                "key": p["key"], "severity": "critical",
                "finding": p["finding"],
                "notice_quote": _quote(notice_text, marker),
            })
    gaps = []
    for cat in data_categories:
        name = (cat or "").strip()
        if name and name.lower() not in low:
            gaps.append({
                "category": name, "severity": "medium",
                "finding": f"The notice never discloses processing of "
                           f"{name!r} (GDPR Art. 13/14; ISO 27701 "
                           f"A.7.3.2/A.7.3.3).",
            })
    return {"contradictions": contradictions, "gaps": gaps,
            "escalate": bool(contradictions)}


def recommendations_doc(subject: str, picked: dict | None,
                        result: dict, siblings: list[dict]) -> str:
    """The consolidated recommendations text attached to the vendor record
    (plain text stand-in for the .docx redline)."""
    lines = [f"Privacy Notice Cross-Check & Recommendations — {subject}", ""]
    if picked:
        lines.append(f"Notice reviewed: {picked.get('name', '?')} "
                     f"(brand: {picked.get('organizationName', '?')})")
    lines.append("")
    if result["contradictions"]:
        lines.append("CONTRADICTIONS (critical — resolve before approval)")
        lines.append("-" * 60)
        for c in result["contradictions"]:
            lines.append(f"  • {c['finding']}")
            if c["notice_quote"]:
                lines.append(f"    Notice says: \"…{c['notice_quote']}…\"")
    else:
        lines.append("No contradictions with the published notice.")
    lines.append("")
    if result["gaps"]:
        lines.append("DISCLOSURE GAPS (recommended notice additions)")
        lines.append("-" * 60)
        for g in result["gaps"]:
            lines.append(f"  • {g['finding']}")
            lines.append(f"    RECOMMENDED ADDITION: describe the "
                         f"{g['category']} processing, its purpose, and the "
                         f"lawful basis.")
    else:
        lines.append("No disclosure gaps found for the recorded data "
                     "categories.")
    if siblings:
        lines.append("")
        lines.append(f"Flagged for MANUAL review (not auto-reviewed): "
                     f"{len(siblings)} sibling notice(s): "
                     + ", ".join(str(s.get("name", "?"))
                                 for s in siblings[:5]))
    return "\n".join(lines)
