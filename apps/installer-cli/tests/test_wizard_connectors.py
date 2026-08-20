"""pick_connectors collects connector credentials into the .env keys.

Connectors are always registered in the kernel; the wizard's only job is to
collect their BASE_URL/TOKEN env vars (merged into ~/.maverick/.env). The
catalog is the single source of truth shared with docs/connectors.md.
"""
from __future__ import annotations


def _fake_catalog():
    return [
        {"name": "clio_read", "label": "Clio (read only)",
         "env": [("CLIO_BASE_URL", False), ("CLIO_TOKEN", True)]},
        {"name": "docusign_read", "label": "DocuSign (read only)",
         "env": [("DOCUSIGN_BASE_URL", False), ("DOCUSIGN_TOKEN", True)]},
    ]


def _drive(monkeypatch, *, enable, names, texts, secrets):
    """Run pick_connectors with scripted prompt answers."""
    # Patch the catalog import target (function lives in maverick-core).
    import maverick.tools.enterprise_connectors as ec
    from maverick_installer import wizard
    monkeypatch.setattr(ec, "connector_catalog", _fake_catalog)

    monkeypatch.setattr(wizard, "_q_confirm", lambda *a, **k: enable)
    text_iter = iter([names, *texts])
    monkeypatch.setattr(wizard, "_q_text", lambda *a, **k: next(text_iter))
    secret_iter = iter(secrets)
    monkeypatch.setattr(wizard, "_q_secret", lambda *a, **k: next(secret_iter))
    return wizard.pick_connectors()


def test_declined_returns_empty(monkeypatch):
    keys = _drive(monkeypatch, enable=False, names="", texts=[], secrets=[])
    assert keys == {}


def test_collects_url_and_secret(monkeypatch):
    keys = _drive(
        monkeypatch,
        enable=True,
        names="clio_read",
        texts=["https://app.clio.com"],
        secrets=["tok-123"],
    )
    assert keys == {
        "CLIO_BASE_URL": "https://app.clio.com",
        "CLIO_TOKEN": "tok-123",
    }


def test_unknown_name_skipped(monkeypatch):
    keys = _drive(
        monkeypatch,
        enable=True,
        names="clio_read, not_a_real_system",
        texts=["https://app.clio.com"],
        secrets=["tok-123"],
    )
    # The bogus name contributes nothing; the valid one is still collected.
    assert keys == {
        "CLIO_BASE_URL": "https://app.clio.com",
        "CLIO_TOKEN": "tok-123",
    }


def test_blank_values_are_not_written(monkeypatch):
    keys = _drive(
        monkeypatch, enable=True, names="docusign_read",
        texts=[""],
        secrets=[""],
    )
    assert keys == {}


def test_catalog_is_the_source_of_truth():
    """The real catalog is well-formed and limited to retained legal systems."""
    from maverick.tools.enterprise_connectors import connector_catalog

    cat = connector_catalog()
    names = [e["name"] for e in cat]
    assert len(names) == len(set(names)), "connector names must be unique"
    assert set(names) == {
        "carta_read", "clio_read", "contractbook_read", "docusign_read",
        "ironclad_read",
    }
    # Shape: each entry has a label and (env_name, is_secret) pairs.
    for e in cat:
        assert e["label"] and isinstance(e["env"], list) and e["env"]
        for pair in e["env"]:
            assert len(pair) == 2 and isinstance(pair[1], bool)
