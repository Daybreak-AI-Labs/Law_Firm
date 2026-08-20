"""Air-gapped preflight audit: flag remote providers / egress / sandbox network."""
from __future__ import annotations

from maverick.air_gap import audit

_LOCAL = "ollama:llama3"


def _all_local_models():
    return {"default": _LOCAL}


def test_default_config_is_not_air_gapped():
    # No exact local pin cannot prove a no-egress model boundary.
    rep = audit(config={})
    assert rep["clean"] is False
    assert any("models] default" in v for v in rep["violations"])
    assert any("egress is not deny-all" in v for v in rep["violations"])


def test_fully_local_with_deny_all_is_clean():
    cfg = {"models": _all_local_models(), "egress": {"deny": ["*"]},
           "sandbox": {"backend": "docker"}}
    rep = audit(config=cfg)
    assert rep["clean"] is True and rep["violations"] == []


def test_role_only_model_override_does_not_satisfy_global_pin():
    one = {"orchestrator": _LOCAL}
    rep = audit(config={"models": one, "egress": {"deny": ["*"]},
                        "sandbox": {"backend": "docker"}})
    assert rep["clean"] is False
    assert any("models] default" in v for v in rep["violations"])


def test_flags_allow_all_egress():
    cfg = {"models": _all_local_models(), "egress": {"deny": [], "allow": []},
           "sandbox": {"backend": "docker"}}
    rep = audit(config=cfg)
    assert any("egress is not deny-all" in v for v in rep["violations"])


def test_flags_sandbox_network():
    cfg = {"models": _all_local_models(), "egress": {"deny": ["*"]},
           "sandbox": {"backend": "docker", "allow_network": True}}
    rep = audit(config=cfg)
    assert any("allow_network" in v for v in rep["violations"])


def test_llm_section_does_not_hide_runtime_remote_defaults():
    cfg = {"llm": _all_local_models(), "egress": {"deny": ["*"]},
           "sandbox": {"backend": "docker"}}
    rep = audit(config=cfg)
    assert rep["clean"] is False
    assert any("models] default" in v for v in rep["violations"])


def test_default_local_sandbox_is_not_air_gapped():
    cfg = {"models": _all_local_models(), "egress": {"deny": ["*"]}}
    rep = audit(config=cfg)
    assert rep["clean"] is False
    assert any("backend=local" in v for v in rep["violations"])


def test_retired_sandbox_backends_cannot_certify_air_gap():
    base = {"models": _all_local_models(), "egress": {"deny": ["*"]}}
    for backend in (
        "devcontainer",
        "firecracker",
        "gvisor",
        "kubernetes",
        "modal",
        "podman",
        "ssh",
        "ep:vendor",
    ):
        rep = audit(config={**base, "sandbox": {"backend": backend}})
        assert rep["clean"] is False
        assert any("not retained" in violation for violation in rep["violations"])


def test_local_provider_pointed_offbox_is_flagged():
    # Finding #3: every role -> ollama (a "local" provider name), but
    # [providers.ollama] base_url points at a public host. The name-only check
    # certified this clean while every prompt left the box; endpoint validation
    # must flag it.
    cfg = {"models": _all_local_models(),
           "providers": {"ollama": {"base_url": "https://exfil.example.com/v1"}},
           "egress": {"deny": ["*"]}, "sandbox": {"backend": "docker"}}
    rep = audit(config=cfg)
    assert rep["clean"] is False
    assert any("remote model" in v for v in rep["violations"])


def test_local_provider_loopback_endpoint_stays_clean():
    # A genuinely loopback local endpoint is proven local and stays clean.
    cfg = {"models": _all_local_models(),
           "providers": {"ollama": {"base_url": "http://127.0.0.1:11434/v1"}},
           "egress": {"deny": ["*"]}, "sandbox": {"backend": "docker"}}
    rep = audit(config=cfg)
    assert rep["clean"] is True and rep["violations"] == []


def test_flags_sentry_dsn_telemetry_sink():
    # Finding #6: a telemetry sink outside the [egress] deny list.
    cfg = {"models": _all_local_models(), "egress": {"deny": ["*"]},
           "sandbox": {"backend": "docker"},
           "observability": {"sentry_dsn": "https://k@o0.ingest.sentry.io/1"}}
    rep = audit(config=cfg)
    assert rep["clean"] is False
    assert any("sentry_dsn" in v for v in rep["violations"])


