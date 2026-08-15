"""Contract-instrument decision — deterministic, from the intake ANSWERS.

The instrument is a function of jurisdiction and processing facts, never of
model prose: EU/UK personal data handled by an external party → a GDPR
Art. 28 Data Processing Agreement; US-only (CCPA/CPRA) → a Privacy Addendum;
both → the DPA covers both; no external party processing personal data →
no instrument (and "no contract" is a remediation, never the headline risk).

A model's comparative GDPR musings must never flip a US-only activity to a
DPA — this guard is the final word. Standalone-safe: stdlib only.
"""
from __future__ import annotations

_EU_UK_MARKERS = (
    "eu", "e.u.", "europe", "european", "eea", "gdpr", "uk", "united kingdom",
    "britain", "germany", "france", "ireland", "netherlands", "spain",
    "italy", "poland", "sweden", "denmark", "portugal", "austria", "belgium",
)
_US_MARKERS = (
    "us", "u.s.", "usa", "united states", "america", "california", "ccpa",
    "cpra", "colorado", "virginia", "texas", "new york", "florida",
)


def _mentions(text: str, markers: tuple[str, ...]) -> bool:
    padded = f" {text.lower().replace(',', ' ').replace('.', ' ')} "
    return any(f" {m} " in padded or f" {m}-" in padded for m in markers)


def decide_instrument(*, regions_text: str, external_processing: bool,
                      personal_data: bool) -> dict:
    """The jurisdiction table. Returns {instrument, reason} where instrument
    is one of DataProcessingAgreement | PrivacyAddendum | None."""
    if not (external_processing and personal_data):
        return {"instrument": None,
                "reason": "no external party processes or receives personal "
                          "data — no instrument is required"}
    eu_uk = _mentions(regions_text, _EU_UK_MARKERS)
    us = _mentions(regions_text, _US_MARKERS)
    if eu_uk:
        return {"instrument": "DataProcessingAgreement",
                "reason": ("EU/UK personal data via a processor — GDPR "
                           "Art. 28 DPA required"
                           + (" (also covers the US exposure)" if us else ""))}
    if us:
        return {"instrument": "PrivacyAddendum",
                "reason": "US-only activity (CCPA/CPRA) — a privacy addendum "
                          "fits; a full GDPR DPA is not the right instrument"}
    return {"instrument": "DataProcessingAgreement",
            "reason": "jurisdiction unclear from intake — defaulting to the "
                      "stronger instrument (DPA); confirm regions with the "
                      "business"}


def guard_instrument(model_pick: str | None, decided: dict) -> dict:
    """The override: when the answers give a clear jurisdiction signal, the
    model's pick loses. Returns the final decision plus whether the guard
    fired (so the narration can say so honestly)."""
    final = dict(decided)
    final["guard_overrode_model"] = bool(
        model_pick and model_pick != decided["instrument"])
    final["model_pick"] = model_pick
    return final
