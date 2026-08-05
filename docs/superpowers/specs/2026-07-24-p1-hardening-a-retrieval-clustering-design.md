# P1 Hardening — Spec A: Retrieval + Clustering (hybrid graph + embeddings)

**Status:** design, approved for planning · **Date:** 2026-07-24
**Track:** A of 3 parallelizable P1-hardening specs (A retrieval+clustering, B commit-pinning,
C review UI). The why-layer (Pass 2) is deferred until A and B land.
**Depends on:** nothing upstream (reads the existing `symbols` table).
**Consumed by:** Spec C (reads cluster metadata; degrades gracefully if absent);
the deferred why-layer (scopes enrichment per feature area).

Related: [design spec §6 recovery pipeline](2026-07-23-archeon-design.md) ·
P1 spike note *(internal validation run — not in this repo)* ·
[P1 spike exit checklist](../../p1-spike-exit-checklist.md)

## 1. Problem

The P1 spike inlines an entire feature file into the synthesis prompt, capped at 60k characters
(`build_feature_bundle` in `claims/recover.py`). This is a deliberate spike shortcut and the
explicit blocker to scaling: a whole component's source does not fit, large slices truncate
silently at the char cap, and there is no notion of *which* code is relevant to a given claim.

We need retrieval that assembles a **token-bounded, relevance-ranked** evidence bundle per
**feature area**, so synthesis runs over a whole component instead of one hand-picked file, and
so each synthesis call sees the code that matters rather than a truncated prefix.

## 2. Goals / Non-goals

**Goals**
- Cluster a component's symbols into coherent **feature areas** using both code-graph proximity
  and embedding similarity.
- For a target feature area, retrieve and pack a line-numbered source bundle up to a **token**
  budget (not a blind char cap), preferring whole-symbol spans.
- Keep the downstream synthesize/verify path unchanged — this track only changes how the bundle
  and the feature-area list are produced.
- Local-first: embeddings via the local Ollama server, no hosted API, no key.

**Non-goals**
- Why-layer / artifact retrieval (deferred Pass 2).
- The MCP retrieval surface (design §8) — that reuses this index later but is out of scope here.
- Reranker models — the embedding + graph score is the ranking signal for now.

## 3. Embedding stack (decided)

- **Primary model:** `qwen3-embedding:4b` via Ollama `POST /api/embed`. SOTA on both code
  retrieval and clustering (the two uses here), instruction/prompt-aware (lets code spans and
  claim statements share one space), 40K context (whole-symbol embedding), Matryoshka dims.
- **CI / low-resource fallback:** `qwen3-embedding:0.6b` — same family and space, 639 MB, fast,
  CPU-viable, so CI does not pull a large model.
- **Transport:** HTTP to the local Ollama endpoint — no in-process `sentence-transformers`/torch
  dependency.
- Vectors are **not** interchangeable across model sizes: `symbol_vectors` records the producing
  `model` and `dims`; a model change triggers re-embedding. Matryoshka truncation is stored so
  clustering can use short vectors (e.g. 256) and ranking can use full ones.

Config: `[retrieval] embed_model`, `embed_endpoint`, `embed_dims`, `token_budget` in
`archeon.toml`, mirroring the existing `[llm]` block.

## 4. Architecture

Four new/changed units, each independently testable:

### 4.1 Edge extraction — `codegraph/edges.py` (new)
The code graph currently stores **only symbol nodes** (`schema.sql` has `symbols`, `coupling`,
`links` — no edge table). Add edge extraction to the scan:
- `references` — symbol→symbol, from identifier uses within a symbol's `source` resolving to
  another known symbol name (clang xrefs where available, tree-sitter identifier match as
  fallback).
- `includes` — file→file, from `#include` directives.
- Persist to new table `symbol_edges(src_id, dst_id, kind, weight)`.
- The existing `coupling` table (file co-change) is reused as a file-level edge signal.

### 4.2 Embedding index — `retrieval/embed.py` (new)
- For each symbol, embed `signature + source` with the code task prompt; store in new
  `symbol_vectors(symbol_id, model, dims, vec BLOB)`.
- Batched calls to Ollama `/api/embed`; idempotent — skip symbols already embedded under the
  current `(model, dims)`.
- Cosine similarity computed in-process with numpy over the stored vectors (corpus is one
  component: thousands of symbols, not millions — no vector DB needed).

### 4.3 Clusterer — `retrieval/cluster.py` (new)
- Build a weighted symbol graph combining: `symbol_edges` (references/includes), file-level
  `coupling`, and embedding cosine similarity (normalized, weighted sum; weights in config).
- Run greedy-modularity / community detection to partition symbols into feature areas.
- A cheap-model pass (design stage 1) labels each cluster and guesses candidate claim types.
- Persist `clusters(id, component, label, candidate_types)` and
  `cluster_members(cluster_id, symbol_id)`.

### 4.4 Bundle builder — rework `build_feature_bundle`
- Input: a target cluster (or a `--feature` path prefix, mapped to overlapping clusters).
- Rank member symbols by combined graph+embedding relevance to the cluster centroid.
- Pack line-numbered source up to `token_budget`, preferring whole-symbol spans; count tokens
  (tokenizer estimate), not characters.
- Return the bundle plus a manifest of included symbols (for provenance / coverage reporting).

## 5. Data flow

```
scan  ──► symbols (exists) + symbol_edges (4.1) + symbol_vectors (4.2)
cluster ──► clusters + cluster_members (4.3)           [cheap-model labels]
synthesize --feature <area>
        ──► bundle builder (4.4) ranks + packs member source under token budget
        ──► existing synthesize_claims / verify_claims  (UNCHANGED)
```

`synthesize` gains the ability to iterate all clusters in a component (`--all-clusters`) instead
of requiring a single `--feature` path.

## 6. Error handling / degradation

- **Ollama unreachable or model missing** → fall back to **graph-only** retrieval (edges +
  coupling), logged as a coverage note on the run; clustering still runs on the graph signal
  alone. This is a real degradation path, tested, not a crash.
- **Cluster bundle still exceeds budget** after ranking → split into sub-bundles, synthesize each,
  merge the resulting claims under the cluster.
- **Singleton / empty clusters** → kept and synthesized (or explicitly skipped with a reason),
  never silently dropped.
- **Unresolvable reference during edge extraction** → skipped; recorded as a scan gap, not fatal.

## 7. Testing

- **Edge extraction (unit):** on a fixture C++ file, `references` and `includes` edges match the
  known call/include structure; unresolved identifiers do not create edges.
- **Embedding (unit):** identical input → identical stored vector under a fixed model; re-embed is
  skipped when `(model, dims)` unchanged and triggered when changed.
- **Ranking (unit):** cosine ranking is deterministic; the bundle never exceeds `token_budget` and
  prefers whole symbols over partial spans.
- **Clustering (unit):** on a two-feature fixture, the two features land in distinct clusters.
- **Integration:** re-run the `motor_ctrl_impl` slice through cluster→bundle→synthesize;
  claim yield is ≥ the spike's 36 with **no whole-file inlining**; then add a second file to the
  component and confirm the clusterer separates the two feature areas.
- **Degradation:** with Ollama disabled, the graph-only path still produces clusters and a bundle.

## 8. Schema changes (all additive)

```sql
CREATE TABLE IF NOT EXISTS symbol_edges(
  src_id INTEGER, dst_id INTEGER, kind TEXT, weight REAL,
  PRIMARY KEY (src_id, dst_id, kind)
);
CREATE TABLE IF NOT EXISTS symbol_vectors(
  symbol_id INTEGER, model TEXT, dims INTEGER, vec BLOB,
  PRIMARY KEY (symbol_id, model, dims)
);
CREATE TABLE IF NOT EXISTS clusters(
  id INTEGER PRIMARY KEY AUTOINCREMENT, component TEXT, label TEXT, candidate_types TEXT
);
CREATE TABLE IF NOT EXISTS cluster_members(
  cluster_id INTEGER, symbol_id INTEGER, PRIMARY KEY (cluster_id, symbol_id)
);
```

## 9. Open questions

- Clustering weight defaults (graph vs embedding) — tune on the golden component; ship defaults in
  config.
- Token counting: exact tokenizer vs. a cheap heuristic — start with a heuristic, refine if
  bundles come out over budget in practice.
