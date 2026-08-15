"""Feature-access checkboxes: matrix rendering, grant/denial derivation, and the
issue-form round trip a client deployment resolves. See licensing.feature_matrix
/ grants_from_checkboxes and maverick.entitlements.Entitlements.allows."""
from __future__ import annotations

from maverick import entitlements
from vendor_console import licensing, store

GOLD = sorted(f for f, t in entitlements.GATED_FEATURES.items() if t == "gold")
PLATINUM = sorted(f for f, t in entitlements.GATED_FEATURES.items() if t == "platinum")


# ---- pure derivation --------------------------------------------------------

def test_matrix_with_no_license_shows_everything_off():
    m = licensing.feature_matrix(None)
    assert m["tier"] is None
    assert all(not f["enabled"] and f["source"] == "off" for f in m["features"])
    assert all(not s["enabled"] for s in m["suites"])


def test_checkboxes_derive_minimal_grant_and_denial_lists():
    # Gold tier: one platinum feature checked (à la carte), one gold feature
    # unchecked (explicit denial); everything else follows the tier.
    checked = [f for f in GOLD if f != "siem_export"] + ["advanced_evolve"]
    features, denied = licensing.grants_from_checkboxes("gold", checked)
    assert features == ["advanced_evolve"]
    assert denied == ["siem_export"]
    # Platinum with every box checked needs no explicit lists at all.
    features, denied = licensing.grants_from_checkboxes("platinum", GOLD + PLATINUM)
    assert features == [] and denied == []


def test_custom_extra_grants_pass_through():
    features, _ = licensing.grants_from_checkboxes("gold", GOLD, ["bespoke_export"])
    assert "bespoke_export" in features


# ---- end-to-end: checkbox form → signed doc → client-side resolve -----------

def _customer(conn) -> int:
    return store.create_customer(conn, name="Cedar Valley Bank",
                                 serve_token_hash="h")


def test_issue_form_checkboxes_control_resolved_entitlements(admin, app):
    r = admin.post("/customers", data={"name": "Cedar Valley Bank",
                                       "posture": "connected"})
    assert r.status_code == 200
    cid = store.list_customers(app.state.conn, search="Cedar")[0].id
    # Gold, but: siem_export unchecked (deny) + advanced_evolve checked (grant).
    checked = [f for f in GOLD if f != "siem_export"] + ["advanced_evolve"]
    r = admin.post(f"/customers/{cid}/licenses",
                   data={"tier": "gold", "suites_on": ["fleet"],
                         "features_on": checked, "expires": "2099-01-01"})
    assert r.status_code in (200, 303)
    doc = licensing.active_license_doc(app.state.conn, cid)
    assert doc["features_denied"] == ["siem_export"]
    assert doc["features"] == ["advanced_evolve"]
    # What the customer's deployment actually resolves, offline:
    ent = entitlements.resolve(doc, trusted_pubkeys=[licensing.public_key_hex()])
    assert ent.paid_active
    assert ent.allows("fleet_governance")        # gold tier feature, checked
    assert not ent.allows("siem_export")         # in-tier but denied
    assert ent.allows("advanced_evolve")         # platinum, à-la-carte grant
    assert not ent.allows("custom_pack_factory")  # platinum, not granted
    assert ent.suite_enabled("fleet")


def test_matrix_round_trips_the_issued_license(conn):
    cid = _customer(conn)
    licensing.issue_license(conn, customer_id=cid, tier="gold",
                            suites=["fleet"], features=["advanced_evolve"],
                            features_denied=["siem_export"])
    m = licensing.feature_matrix(licensing.active_license_doc(conn, cid))
    by_name = {f["name"]: f for f in m["features"]}
    assert m["tier"] == "gold"
    assert by_name["siem_export"] == {"name": "siem_export", "min_tier": "gold",
                                      "enabled": False, "source": "denied"}
    assert by_name["advanced_evolve"]["enabled"]
    assert by_name["advanced_evolve"]["source"] == "grant"
    assert by_name["fleet_governance"]["source"] == "tier"
    assert {s["name"]: s["enabled"] for s in m["suites"]} == {"fleet": True}
