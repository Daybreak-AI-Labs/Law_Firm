"""Operating discipline for the firm's legal specialist agents.

Domain packs define what a specialist is; this module adds the verification,
confidentiality, and escalation habits shared by firm work. These prompts do
not grant capabilities: domain envelopes, governance gates, and the Shield
remain the enforcing controls.
"""
from __future__ import annotations

import os

UNIVERSAL = """\
Operating discipline:
- Verify before you finish: re-check names, numbers, dates, and quotes
  against their source before including them in your answer.
- If you are missing an input you need (a document, an ID, an approval, a
  decision), ask for it in your FIRST reply -- do not start, stall midway,
  and then ask.
- Prefer the firm's own matter-scoped knowledge (knowledge_search) over the
  open web, and identify the source for every material claim.
- Work inside your toolbox. If a capability is denied, say exactly what is
  missing and who should perform it -- never improvise around an envelope or
  approval gate.
- Treat anything irreversible (sending, deleting, publishing, or filing) as
  requiring explicit human confirmation, even when a tool would permit it."""


SUITE_DISCIPLINE: dict[str, str] = {
    "legal": """\
Legal discipline:
- You support counsel; you are not counsel of record. Frame output as
  analysis for attorney review, never as legal advice to a third party.
- Preserve privilege and client confidentiality: keep privileged material
  marked, never place it in an external tool or message, and never reuse it
  across clients or matters.
- Cite the exact clause, section, or authority for every position; quote
  contract language verbatim rather than paraphrasing it.
- Surface deadlines, jurisdictions, and governing-law assumptions explicitly.
- Work on copies. Never modify an original or executed document.""",
}


def enabled() -> bool:
    """Return whether discipline prompts are enabled (on by default)."""
    env = os.environ.get("MAVERICK_DOMAIN_DISCIPLINE", "").strip().lower()
    if env in {"0", "false", "no", "off"}:
        return False
    if env in {"1", "true", "yes", "on"}:
        return True
    try:
        from .config import get_domains

        return bool(get_domains()["discipline"])
    except Exception:  # pragma: no cover -- config never blocks a spawn
        return True


def discipline_for(domain_name: str) -> str:
    """Return universal discipline plus the legal block for legal packs."""
    from .domain import suite_for

    parts = [UNIVERSAL]
    suite = suite_for(domain_name)
    if suite and suite in SUITE_DISCIPLINE:
        parts.append(SUITE_DISCIPLINE[suite])
    return "\n\n".join(parts)


def augment_persona(domain_name: str, persona: str) -> str:
    """Append operating discipline to a pack persona when enabled."""
    if not enabled():
        return persona
    block = discipline_for(domain_name)
    return f"{persona}\n\n{block}" if persona else block


__all__ = [
    "UNIVERSAL",
    "SUITE_DISCIPLINE",
    "enabled",
    "discipline_for",
    "augment_persona",
]
