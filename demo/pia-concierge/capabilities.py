"""Capability detection for PIA Concierge.

The concierge ships in two shapes from the same code:

* **Platform** (bundled with Lightwork) — the real ``maverick`` engine,
  world-model governance, the Ed25519 signed audit chain, the control
  catalog, connected-source doc discovery, and the dashboard.
* **Standalone** (sold on its own) — just the agent: a self-contained
  scorer, in-process OneTrust review/approve, and vendor memory. No audit
  trail, no governance, no learning, no dashboard. Buying Lightwork is what
  unlocks those.

Set ``PIA_STANDALONE=1`` to force standalone even where ``maverick`` is
importable (that's how the demo shows the standalone SKU on one machine).
When ``maverick`` cannot be imported at all, standalone is the only option.
"""
from __future__ import annotations

import importlib.util
import os


def _can_import(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


# maverick present at all? (the platform engine + governance live there)
_PLATFORM_PRESENT = _can_import("maverick.world_model")

# The switch: an explicit standalone build, or simply no platform installed.
STANDALONE = os.environ.get("PIA_STANDALONE") == "1" or not _PLATFORM_PRESENT
PLATFORM = not STANDALONE

# Optional connectors are their own axis even on the platform.
_DISCOVERY_PRESENT = PLATFORM and _can_import("maverick.doc_discovery")

# Feature -> is it active in this build. Standalone forces every
# platform-only capability off.
CAPS: dict[str, bool] = {
    "governed_engine": PLATFORM,        # governed, content-addressed catalog
    "control_catalog": PLATFORM,        # GDPR/ISO/SOC2/NIST control citations
    "signed_audit": PLATFORM,           # Ed25519 tamper-evident chain
    "world_governance": PLATFORM,       # goals + human approval queue
    "assessment_memory": PLATFORM,      # cross-run learning / suggestions
    "privacy_workspace": PLATFORM,      # /privacy program views (Part 2)
    "dpa_clause_review": PLATFORM,      # Art. 28 clause engine on addenda
    "paper_redline": PLATFORM,          # vendor paper vs our template + redline
    "doc_discovery": _DISCOVERY_PRESENT,  # connected-source find-my-documents
    "llm_assist": PLATFORM,             # model-assisted chat interpretation
    # Always-on agent features (work in both builds):
    "chat_voice_intake": True,
    "risk_scoring": True,
    "onetrust_filing": True,
    "onetrust_review": True,
    "append_documents": True,
    "vendor_memory": True,              # local: prior record + carry-forward
    "speed_story": True,
}

# Human-readable labels for the UI capability banner.
_LABELS: dict[str, str] = {
    "governed_engine": "Governed questionnaire catalog (immutable releases)",
    "control_catalog": "Control mapping with GDPR/ISO/SOC 2/NIST citations",
    "signed_audit": "Ed25519 tamper-evident audit chain",
    "world_governance": "Governed goal timeline + approval queue",
    "assessment_memory": "Cross-run learning & suggested answers",
    "privacy_workspace": "Privacy workspace & program insights",
    "dpa_clause_review": "Art. 28 clause-by-clause DPA review",
    "paper_redline": ("Vendor-paper review vs our template, with tracked-"
                      "changes redline and versioned filing"),
    "doc_discovery": "Connected-source document discovery",
    "llm_assist": "Model-assisted conversational understanding",
    "chat_voice_intake": "Chat / voice / guided intake",
    "risk_scoring": "Deterministic risk scoring",
    "onetrust_filing": "Files into OneTrust",
    "onetrust_review": "Review & approve in OneTrust",
    "append_documents": "Append documents to a vendor",
    "vendor_memory": "Vendor memory (prior record + carry-forward)",
    "speed_story": "Speed / value metrics",
}


# The signed license (standalone only — a platform install is licensed by
# its contract). Evaluation mode = fully functional, bounded open-case cap.
if STANDALONE:
    import license_kit
    LICENSE = license_kit.load_license()
else:
    LICENSE = {"mode": "platform", "customer": "", "sku": "platform",
               "expires_at": None, "capabilities": [], "reason": "",
               "open_case_cap": None}


def caps_summary() -> dict:
    """What's active vs. what needs Lightwork, for the UI banner."""
    active = [_LABELS[k] for k, on in CAPS.items() if on]
    gated = [_LABELS[k] for k, on in CAPS.items() if not on]
    return {
        "standalone": STANDALONE,
        "mode": "Standalone agent" if STANDALONE else "Lightwork platform",
        "active": active,
        "gated": gated,
        "license": LICENSE,
    }
