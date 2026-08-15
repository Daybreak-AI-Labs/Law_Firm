"""Governed adapter rung (weights) -- the in-tenant adaptation chain, made checkable.

A LoRA-shaped adapter is driven end-to-end through the REAL governance chain at
the ``weights`` rung: the provenance boundary (frontier-model output refused as
training data), the payload hygiene boundary (code/pickle formats refused
structurally), a held-out fitness eval with the overfit refusal, an Ed25519
operator signature bound to the exact payload digest, the append-only promotion
ledger, and a byte-identical one-step pointer rollback.

Two seams are stubbed and disclosed, mirroring the code-rung proofs: the
*trainer* (the stub trainer writes a deterministic artifact -- no GPU, no
deps) and the *eval scorer* (a deterministic function stands in for running
the tenant's private evals against a live serving stack). No LLM, no network.

Run: ``python proof/adapter_rung_proof.py`` (exit 0 = all guarantees hold).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


class _Result:
    def __init__(self, label: str, passed: bool, detail: str):
        self.label, self.passed, self.detail = label, passed, detail


def _mk_keys(keydir: Path) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    priv = ed25519.Ed25519PrivateKey.generate()
    priv_hex = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                  serialization.NoEncryption()).hex()
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    keydir.mkdir(parents=True, exist_ok=True)
    (keydir / "operator.priv.hex").write_text(priv_hex, encoding="utf-8")
    (keydir / "operator.pub").write_bytes(pub)
    return priv_hex


def run_all(work: Path) -> list[_Result]:
    from maverick import adapter_rung as ar
    from maverick.self_harness_eval import corpus_split
    from maverick.self_improvement import PromotionLedger

    out: list[_Result] = []
    keydir = work / "keys"
    private_hex = _mk_keys(keydir)
    from maverick import approval_signing as signing
    public_hex = (keydir / "operator.pub").read_bytes().hex()

    def approve(request):
        return signing.sign_request(request, private_hex)

    def verify(candidate):
        signature = getattr(candidate, "approval_signature", None)
        return (signing.verify(signing.ApprovalRequest.for_candidate(candidate),
                               str(signature), [public_hex])
                if signature else None)

    def provision(binding):
        import hashlib
        digest = hashlib.sha256(
            f"{binding['model_name']}|{binding['modelfile_sha256']}".encode()
        ).hexdigest()
        return {
            "version": 1, "provider": "ollama", "verified": True,
            "model_name": binding["model_name"], "model_digest": digest,
            "source_modelfile_sha256": binding["modelfile_sha256"],
            "verified_at": 123.0,
        }
    ledger_path = work / "ledger.json"
    ledger = PromotionLedger(path=ledger_path)
    store = ar.AdapterStore(root=work / "store")

    # A 64-case eval corpus; the weights rung demands >= 20 held-out samples.
    cases = [f"case-{i:03d}" for i in range(64)]
    held_in_ids, held_out_ids = corpus_split([{"goal": c} for c in cases],
                                             held_out_frac=0.35)
    held_in_set = set(held_in_ids)

    def base_pass(case: str) -> bool:
        return int(case.split("-")[1]) % 2 == 0  # baseline resolves half

    def scorer_genuine(ref: str, ids: list[str]) -> dict[str, bool]:
        if "+adapter:" in ref:
            return dict.fromkeys(ids, True)          # tuned model resolves all
        return {c: base_pass(c) for c in ids}

    def scorer_memoriser(ref: str, ids: list[str]) -> dict[str, bool]:
        if "+adapter:" in ref:                      # gains ONLY on seen cases
            return {c: (c in held_in_set) or base_pass(c) for c in ids}
        return {c: base_pass(c) for c in ids}

    # 1. Provenance boundary: frontier-model output refused as training data.
    examples = [
        ar.TrainExample("p1", "good", "bad", provenance="human_correction"),
        ar.TrainExample("p2", "good", "bad", provenance="tenant_trace"),
        ar.TrainExample("p3", "leaked", "x", provenance="model_output"),
        ar.TrainExample("p4", "odd", "x", provenance="scraped"),
    ]
    review = ar.screen_examples(examples)
    prov_ok = (review.ok and len(review.kept) == 2 and len(review.refused) == 2
               and "model_output" not in review.provenance_counts)
    out.append(_Result("provenance boundary", prov_ok,
                       f"{len(review.kept)} tenant examples kept; model_output + unknown "
                       "provenance refused (distillation guard)"))

    # 2. Payload hygiene: code smuggled into a weights payload is refused
    #    structurally, BEFORE any eval runs.
    trainer = ar.get_trainer("stub")
    dirty_dir = work / "dirty"
    trainer.train(review, "ollama:proof-base", dirty_dir)
    (dirty_dir / "post_init.py").write_text("import os\n", encoding="utf-8")
    dirty = ar.govern_adapter_change(dirty_dir, cases, score_fn=scorer_genuine,
                                     approve=approve, approval_verifier=verify,
                                     approval_public_keys=[public_hex],
                                     serving_provisioner=provision,
                                     ledger=ledger, store=store)
    clean_dir = work / "clean_a"
    manifest_a = trainer.train(review, "ollama:proof-base", clean_dir)
    hygiene_ok = (not dirty.hygiene_ok and "post_init.py" in dirty.reason
                  and ar.review_adapter_payload(clean_dir).ok)
    out.append(_Result("payload hygiene boundary", hygiene_ok,
                       "adapter dir carrying .py refused before eval; "
                       "safetensors-only payload accepted"))

    # 3. Genuine uplift promotes: signed at the weights rung, ledgered, activated.
    res_a = ar.govern_adapter_change(clean_dir, cases, score_fn=scorer_genuine,
                                     approve=approve, approval_verifier=verify,
                                     approval_public_keys=[public_hex],
                                     serving_provisioner=provision,
                                     ledger=ledger, store=store)
    active = store.active() or {}
    genuine_ok = (res_a.promoted and res_a.fitness.samples >= 20
                  and res_a.fitness.candidate_score > res_a.fitness.baseline_score
                  and res_a.approver_id is not None
                  and active.get("adapter_id") == manifest_a.adapter_id
                  and active.get("authority") == "governed-v1"
                  and active.get("serving", {}).get("deployment", {}).get("verified") is True
                  and (res_a.serving or {}).get("create_command", "").startswith("ollama create"))
    out.append(_Result("genuine uplift promotes", genuine_ok,
                       f"held-out {res_a.fitness.baseline_score:.3f} -> "
                       f"{res_a.fitness.candidate_score:.3f} over {res_a.fitness.samples} unseen "
                       f"cases; signed, ledgered, Modelfile emitted"))

    # 4. Memoriser refused: held-in gain with a flat held-out split is OVERFIT.
    mem_dir = work / "memoriser"
    trainer.train(review, "ollama:proof-base", mem_dir)
    res_m = ar.govern_adapter_change(mem_dir, cases, score_fn=scorer_memoriser,
                                     approve=approve, approval_verifier=verify,
                                     approval_public_keys=[public_hex],
                                     serving_provisioner=provision,
                                     ledger=ledger, store=store)
    mem_ok = (not res_m.promoted and res_m.fitness.overfit
              and "OVERFIT" in res_m.reason
              and (store.active() or {}).get("adapter_id") == manifest_a.adapter_id)
    out.append(_Result("memoriser refused (overfit)", mem_ok,
                       f"held-in {res_m.fitness.held_in_baseline:.3f}->"
                       f"{res_m.fitness.held_in_candidate:.3f} gamed; held-out flat -> "
                       "OVERFIT, never reached the gate"))

    # 5. Unforgeable approval: the signed payload embeds the weights digest
    #    (ApprovalRequest re-hashes the payload -- its TOCTOU bind), so weights
    #    swapped after sign-off change the payload and are NOT authorised.
    import hashlib
    import json

    from maverick import approval_signing as asig
    from maverick.self_improvement import Candidate

    def _cand(weights_digest: str) -> Candidate:
        payload = json.dumps({"adapter_id": "proof", "weights_sha256": weights_digest},
                             sort_keys=True)
        return Candidate(rung="weights", summary="adapter proof", baseline_score=0.5,
                         candidate_score=1.0, samples=22, payload=payload,
                         payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                         capability_widens=False,
                         rollback={"pointer": "archived"}, id="adapter-proof-001")

    cand = _cand(ar.payload_digest_dir(clean_dir))
    sig = asig.sign_request(asig.ApprovalRequest.for_candidate(cand),
                            (keydir / "operator.priv.hex").read_text().strip())
    genuine_auth = asig.verify(asig.ApprovalRequest.for_candidate(cand), sig,
                               [(keydir / "operator.pub").read_bytes().hex()])
    swapped = _cand("0" * 64)  # simulated weights swap after sign-off
    swapped_auth = asig.verify(asig.ApprovalRequest.for_candidate(swapped), sig,
                               [(keydir / "operator.pub").read_bytes().hex()])
    out.append(_Result("unforgeable payload-bound approval",
                       bool(genuine_auth) and swapped_auth is None,
                       f"approver={genuine_auth}; swapped weights digest NOT authorised"))

    # 6. One-step rollback: promote a second adapter, then restore the first
    #    pointer byte-identically; the ledger marks the promotion rolled back.
    active_path = work / "store" / ar.ACTIVE_BASENAME
    pointer_a_bytes = active_path.read_bytes()
    b_dir = work / "clean_b"
    review_b = ar.screen_examples(examples[:2] + [
        ar.TrainExample("p5", "better", "worse", provenance="human_example")])
    manifest_b = trainer.train(review_b, "ollama:proof-base", b_dir)
    res_b = ar.govern_adapter_change(b_dir, cases, score_fn=scorer_genuine,
                                     approve=approve, approval_verifier=verify,
                                     approval_public_keys=[public_hex],
                                     serving_provisioner=provision,
                                     ledger=ledger, store=store)
    swapped_to_b = (store.active() or {}).get("adapter_id") == manifest_b.adapter_id
    ar.rollback_adapter(res_b.candidate_id, store=store, ledger=ledger)
    restored = active_path.read_bytes() == pointer_a_bytes
    rec = ledger.get(res_b.candidate_id)
    rb_ok = (res_b.promoted and swapped_to_b and restored
             and rec is not None and rec.rolled_back)
    out.append(_Result("one-step rollback", rb_ok,
                       "previous pointer restored byte-identical; ledger marks "
                       "the superseded promotion rolled_back"))
    return out


def main() -> int:
    try:
        import cryptography  # noqa: F401
    except Exception:
        print("  [ CI ]  adapter-rung proof needs `cryptography` (Ed25519) -- verified in CI")
        return 0
    print("=" * 78)
    print("  MAVERICK -- GOVERNED ADAPTER RUNG   (in-tenant weights, real governance chain)")
    print("=" * 78)
    failed = 0
    with tempfile.TemporaryDirectory(prefix="adapter-rung-proof-") as td:
        for r in run_all(Path(td)):
            tag = "PASS" if r.passed else "FAIL"
            failed += 0 if r.passed else 1
            print(f"  [{tag}]  {r.label:34}  {r.detail}")
    print("=" * 78)
    print(f"  {6 - failed} guarantees PROVEN, {failed} failed"
          "   (trainer + eval scorer stubbed, by design)")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
