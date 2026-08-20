# The knowledge plane

Matter-scoped document recall (RAG) for legal work: an agent grounds its
answers in public reference material plus documents from the one active matter,
via the `knowledge_search` tool. The knowledge package is an optional install,
but a legal profile that declares matter knowledge fails closed before model
work when the package or its exact-matter collection is unavailable.

```toml
[knowledge]
enable   = true
embedder = "local"     # local | deterministic (tests only)
store    = "sqlite"
model    = "D:/firm-models/legal-embedder" # absolute existing directory
model_digest = "sha256:<canonical model-tree digest>"
```

## Storage: pick a backend per deployment

The firm build deliberately ships one file-backed store.

| Backend    | When                                            | Notes |
|------------|-------------------------------------------------|-------|
| **sqlite** (default) | Single box or encrypted local volume | Dependency-free brute-force cosine. Chunk text, vectors, and provenance metadata are sealed before SQLite writes them. Plaintext mode is refused. |

Collection identifiers remain visible for lookup, so they must be opaque keys,
never client names. File-backed collections are accepted only as
`matter:<numeric-id>:<source>` or `public:<source>`.

## Embedders

`local` uses on-box sentence-transformers and sends no document content to an
embedding vendor. `deterministic` is an offline hashing stub for tests and smoke
runs only. Hosted Voyage and Cohere embedding code and configuration were
removed from the firm build. Stale hosted configuration is rejected rather than
silently reinterpreted.

`local` has no downloadable model default. An operator must provision an
absolute local directory, verify it out of band, and configure the canonical
SHA-256 tree digest returned by
`maverick_knowledge.local_embed.model_tree_digest(path)`. The loader forces
Hugging Face and Transformers offline mode, uses `local_files_only`, disables
remote code, and admits safetensors weights only; repository ids, symlinks,
pickle weights, native libraries, and custom-code metadata are rejected. If the
model tree changes after admission, the run stops before embedding client text.

The default configured vector dimension is 384. If the pinned model uses a
different dimension, set `dim` and re-embed the corpus; the store raises on a
dimension mismatch rather than returning arbitrary results.

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

Retrieval respects ethical walls: the tool is registered only when a run has a
durable matter id, and it searches only the exact matter namespace plus shipped
public reference namespaces. A caller cannot supply a collection name.

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
