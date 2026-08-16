# The knowledge plane

Per-domain document recall (RAG) for the workforce: a specialist agent grounds
its answers in the documents its department is allowed to see, via the
`knowledge_search` tool. Knowledge is an **opt-in extra** — the kernel never
requires `maverick-knowledge`, and RAG is off until you enable it.

```toml
[knowledge]
enable   = true
embedder = "local"     # hosted | cohere | local | deterministic
store    = "sqlite"
```

## Storage: pick a backend per deployment

One `VectorStore` protocol, three interchangeable backends. Choice is a config
line, not a fork — the agent-facing behaviour is identical.

| Backend    | When                                            | Notes |
|------------|-------------------------------------------------|-------|
| **sqlite** (default) | Single box, SMB, air-gapped                | Dependency-free brute-force cosine, sealed by the platform's encryption-at-rest. Comfortable to a few million chunks per tenant. |

We do **not** ship a managed-only vector service as the default: the platform
sells "your data never leaves your boundary," so the knowledge store — the
client's most sensitive documents — must be self-hostable. A hosted backend can
be added later as an option, never the baseline.

## Embedders

`hosted` (Voyage, needs `VOYAGE_API_KEY`), `cohere`, `local` (on-box
sentence-transformers, no key), or `deterministic` (a hashing stub — offline and
keyless, but low recall quality; for tests and smoke runs only). **Meaningful
semantic recall needs `local` or `hosted`.** If you change embedder or model,
the vector dimension changes — re-embed the corpus (the store raises rather than
silently returning garbage on a dim mismatch).

`model` and `dim` default **per embedder** — `local` resolves to
`all-MiniLM-L6-v2` at 384 dimensions, `hosted` to `voyage-3` at 1024 — so
picking an embedder is enough. Set them explicitly only to override.

### The hosted embedders send the documents themselves

This is worth stating separately because it is easy to file under the same
heading as the LLM call, and it is not the same thing. When a role is routed to
a cloud model, what leaves is a *prompt* — and
`[privacy] redact_egress` will minimize it on the way out. When you index a
matter with a hosted embedder, what leaves is **the documents**, in full, one
chunk at a time. That path has no redaction knob, and it never went through one:
`maybe_redact_egress` is wired into the LLM chokepoint only.

So the hosted providers are gated on saying so out loud:

```toml
[knowledge]
embedder                  = "hosted"
allow_external_embedding  = true    # or MAVERICK_KNOWLEDGE_ALLOW_EXTERNAL_EMBEDDING=1
```

Without it `build_embedder` refuses, and names `local` as the alternative that
embeds on-box with no egress at all. `local` and `deterministic` need no
acknowledgement — nothing leaves.

Every batch that does leave writes a `knowledge_egress` row on the signed audit
chain: provider, vendor host, model, chunk count, byte count, and a SHA-256 over
the batch. The chunk text is **not** in the record — the event exists to
document the departure, not to copy it. The row is written *before* the request,
so a batch still appears if the vendor errors or the connection drops; the bytes
were on the wire either way. If the audit subsystem refuses to write, the batch
does not go out.

For privileged client material, `local` is the setting that needs no argument:
it embeds on the box, needs no key, and nothing leaves.

## Governance

Every chunk is provenance-stamped at ingest: `source` (the citation), `subject`
(the data-subject key, when the document belongs to one), `trust_tier`
(3 first-party … 0 external), `sensitivity`, `ingested_by`, `ingested_at`, and a
`doc_sha256` content hash. Ingestion is screened for prompt-injection on the way
in and recorded on the signed audit chain (`evidence_capture`).

Because chunks carry a subject, the knowledge plane participates in the platform's
compliance flows:

- **Right to erasure (GDPR Art. 17).** `maverick erase --channel X --user Y`
  removes the subject's document chunks along with their world-model rows, and
  the erase's signed audit event records the count.
- **Erasure verification.** `maverick erase-verify` folds the subject's residual
  knowledge-chunk count into its verdict. A subject is not "clean" while any of
  their documents survive in the vector store, and an unavailable configured
  knowledge store yields `INDETERMINATE` rather than an assumed zero.
- **Knowledge-only cleanup.** `maverick knowledge residual --channel X --user Y`
  and `maverick knowledge erase-subject ...` for targeted checks and removals.

Retrieval respects the compartment bulkheads: a pack's `knowledge_search` is
bound to its own `knowledge_sources`, so a finance specialist never retrieves
another department's documents.

## Starter corpora

`maverick knowledge seed` ingests a small, licensing-clean grounding corpus —
original plain-language summaries of public-domain frameworks (GDPR, EU AI Act,
NIST AI RMF, CCPA/CPRA), each anchored to the article it summarizes — into the
suite collections the shipped packs already point at. So a privacy/GRC
specialist can cite the governing regulation out of the box; customers layer
their own documents on top. Redistributable-restricted standards (e.g. ISO) are
deliberately excluded.

```
maverick knowledge seed --list          # show the shipped corpora
maverick knowledge seed                  # ingest them (keyless; idempotent)
maverick knowledge collections           # list collections + chunk counts
```
