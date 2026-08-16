"""Document-egress controls on the hosted embedders.

Indexing sends the documents themselves to a third-party vendor, in full. That
is a different exposure from the LLM chokepoint (which sends prompts and has
its own redaction knob in ``maverick.privacy_egress``), and nothing covered it:
``maybe_redact_egress`` is wired into ``llm.py`` only, and
``enterprise.egress_permitted`` returns True whenever enterprise mode is off.

Two things now stand in the way, and these tests pin both: the operator has to
acknowledge the vendor, and every batch that leaves lands in the audit record
as a privilege-log entry carrying metadata and a content commitment but never
the text.
"""
from __future__ import annotations

import hashlib
import sys
import types

import pytest
from maverick_knowledge import embed as embed_mod
from maverick_knowledge.embed import (
    CohereEmbedder,
    DeterministicEmbedder,
    HostedEmbedder,
    build_embedder,
    external_embedding_allowed,
)


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    """The env escape hatches must not leak in from the developer's shell."""
    for var in ("MAVERICK_EMBED_PROVIDER",
                "MAVERICK_KNOWLEDGE_ALLOW_EXTERNAL_EMBEDDING",
                "MAVERICK_EMBED_API_KEY", "COHERE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def _fake_httpx(capture: dict, payload: dict):
    mod = types.ModuleType("httpx")

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    def post(url, headers=None, json=None, timeout=None):
        capture["url"] = url
        capture["json"] = json
        return _Resp()

    mod.post = post
    return mod


@pytest.fixture
def recorded(monkeypatch):
    """Capture knowledge_egress events without a real audit backend."""
    events: list[dict] = []
    audit = types.ModuleType("maverick.audit")

    class EventKind:
        KNOWLEDGE_EGRESS = "knowledge_egress"

    def audit_event(kind, **payload):
        events.append({"kind": kind, **payload})
        return True

    audit.EventKind = EventKind
    audit.audit_event = audit_event
    monkeypatch.setitem(sys.modules, "maverick.audit", audit)
    return events


# --- the acknowledgement gate -------------------------------------------


@pytest.mark.parametrize("provider", ["hosted", "cohere"])
def test_external_provider_refused_without_acknowledgement(provider):
    with pytest.raises(RuntimeError, match="sends document text to a third-party"):
        build_embedder({"embedder": provider, "api_key": "k"})


@pytest.mark.parametrize("provider", ["hosted", "cohere"])
def test_acknowledgement_names_the_on_box_alternative(provider):
    """A refusal that only says no teaches the operator to set the flag."""
    with pytest.raises(RuntimeError) as e:
        build_embedder({"embedder": provider, "api_key": "k"})
    assert "allow_external_embedding" in str(e.value)
    assert "'local'" in str(e.value)


def test_gate_precedes_the_api_key_check():
    """Tell the operator about the decision, not about a missing key.

    Both are RuntimeErrors, so ordering is the only thing that decides which
    problem an operator is sent to solve first -- and the key is the lesser one.
    """
    with pytest.raises(RuntimeError) as e:
        build_embedder({"embedder": "hosted"})  # no key AND no acknowledgement
    assert "sends document text" in str(e.value)
    assert "no API key" not in str(e.value)


@pytest.mark.parametrize("provider", ["hosted", "cohere"])
def test_acknowledged_in_config_builds(provider):
    emb = build_embedder({
        "embedder": provider, "api_key": "k", "allow_external_embedding": True,
    })
    assert isinstance(emb, (HostedEmbedder, CohereEmbedder))


@pytest.mark.parametrize("provider", ["hosted", "cohere"])
def test_env_escape_hatch_acknowledges(monkeypatch, provider):
    monkeypatch.setenv("MAVERICK_KNOWLEDGE_ALLOW_EXTERNAL_EMBEDDING", "1")
    assert isinstance(
        build_embedder({"embedder": provider, "api_key": "k"}),
        (HostedEmbedder, CohereEmbedder),
    )


def test_env_off_overrides_config_on(monkeypatch):
    """The env var wins in BOTH directions, like MAVERICK_EMBED_PROVIDER.

    A knob that can only be turned on remotely is not an escape hatch.
    """
    monkeypatch.setenv("MAVERICK_KNOWLEDGE_ALLOW_EXTERNAL_EMBEDDING", "0")
    assert external_embedding_allowed({"allow_external_embedding": True}) is False
    with pytest.raises(RuntimeError, match="sends document text"):
        build_embedder({
            "embedder": "hosted", "api_key": "k",
            "allow_external_embedding": True,
        })


@pytest.mark.parametrize("provider", ["local", "deterministic"])
def test_on_box_providers_need_no_acknowledgement(provider, monkeypatch):
    """Nothing leaves the box, so there is nothing to acknowledge."""
    if provider == "local":
        pytest.importorskip("sentence_transformers")
    emb = build_embedder({"embedder": provider})
    if provider == "deterministic":
        assert isinstance(emb, DeterministicEmbedder)


# --- the privilege-log entry ---------------------------------------------


def test_hosted_embed_records_the_batch(monkeypatch, recorded):
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx(
        {}, {"data": [{"index": 0, "embedding": [0.1]},
                      {"index": 1, "embedding": [0.2]}]}))
    HostedEmbedder(model="voyage-3", base_url="https://api.voyageai.com/v1",
                   api_key="k").embed(["alpha", "beta"])

    assert len(recorded) == 1
    ev = recorded[0]
    assert ev["kind"] == "knowledge_egress"
    assert ev["provider"] == "hosted"
    assert ev["host"] == "api.voyageai.com"
    assert ev["model"] == "voyage-3"
    assert ev["chunks"] == 2
    assert ev["bytes"] == len(b"alphabeta")
    assert ev["content_sha256"] == hashlib.sha256(b"alphabeta").hexdigest()


def test_cohere_embed_records_the_batch(monkeypatch, recorded):
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx(
        {}, {"embeddings": {"float": [[0.1]]}}))
    CohereEmbedder(model="embed-v4.0", api_key="k").embed(["only"])

    assert [e["provider"] for e in recorded] == ["cohere"]
    assert recorded[0]["host"] == "api.cohere.com"
    assert recorded[0]["chunks"] == 1


def test_record_never_carries_the_chunk_text(monkeypatch, recorded):
    """The event documents the departure; it must not copy the exposure."""
    secret = "Bjerken and Day client memo, privileged and confidential"
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx(
        {}, {"data": [{"index": 0, "embedding": [0.0]}]}))
    HostedEmbedder(model="m", base_url="https://vendor.example/v1",
                   api_key="k").embed([secret])

    blob = repr(recorded[0])
    assert secret not in blob
    for word in ("Bjerken", "privileged", "memo"):
        assert word not in blob


def test_batch_is_recorded_even_when_the_vendor_errors(monkeypatch, recorded):
    """The bytes are on the wire before the response comes back.

    A log written only on success would silently omit exactly the batches an
    incident review cares about.
    """
    mod = types.ModuleType("httpx")

    def post(url, headers=None, json=None, timeout=None):
        raise OSError("connection reset")

    mod.post = post
    monkeypatch.setitem(sys.modules, "httpx", mod)

    with pytest.raises(OSError):
        HostedEmbedder(model="m", base_url="https://vendor.example/v1",
                       api_key="k").embed(["sensitive"])
    assert [e["kind"] for e in recorded] == ["knowledge_egress"]


def test_audit_refusal_stops_the_batch(monkeypatch):
    """AuditRefused propagates: no privilege log, no egress.

    audit_event's contract is that a refusal means the caller must not proceed.
    Shipping privileged documents to a vendor with the log knowingly broken is
    the case that contract exists for.
    """
    posted: list = []
    mod = types.ModuleType("httpx")

    def post(url, headers=None, json=None, timeout=None):
        posted.append(url)
        raise AssertionError("must not reach the vendor after a refusal")

    mod.post = post
    monkeypatch.setitem(sys.modules, "httpx", mod)

    class AuditRefused(RuntimeError):
        pass

    audit = types.ModuleType("maverick.audit")
    audit.EventKind = type("EventKind", (), {"KNOWLEDGE_EGRESS": "knowledge_egress"})

    def audit_event(kind, **payload):
        raise AuditRefused("worm store unavailable")

    audit.audit_event = audit_event
    monkeypatch.setitem(sys.modules, "maverick.audit", audit)

    with pytest.raises(RuntimeError, match="worm store unavailable"):
        HostedEmbedder(model="m", base_url="https://vendor.example/v1",
                       api_key="k").embed(["privileged"])
    assert posted == []


def test_missing_kernel_does_not_break_standalone_indexing(monkeypatch):
    """maverick-knowledge declares no dependencies and must still run alone."""
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx(
        {}, {"data": [{"index": 0, "embedding": [0.5]}]}))

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def no_maverick(name, *a, **kw):
        if name == "maverick.audit":
            raise ImportError("no kernel here")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", no_maverick)
    out = HostedEmbedder(model="m", base_url="https://vendor.example/v1",
                         api_key="k").embed(["x"])
    assert out == [[0.5]]


def test_deterministic_embedder_records_nothing(recorded):
    """Nothing left the box, so there is nothing to log."""
    DeterministicEmbedder(dim=8).embed(["alpha", "beta"])
    assert recorded == []


def test_host_of_strips_everything_but_the_host():
    """The audit record names the vendor, not a URL that could carry a key."""
    assert embed_mod._host_of(
        "https://user:tok@api.voyageai.com:443/v1/embeddings?k=v"
    ) == "api.voyageai.com"
    assert embed_mod._host_of("not a url") == ""
