# P1 Hardening — Overview & Decomposition

**Date:** 2026-07-24

The P1 what-layer spike *(internal validation run — not in this repo)*
cleared both gates (pre-verification 0.861 ≥ 0.85, post-verification 1.000 ≥ 0.95 at n=30) and
green-lit hardening P1. The design's hardening scope is four pieces; they were decomposed by
dependency into **three parallelizable specs plus one deferred track**.

## Dependency graph

| Track | Spec | Upstream dependency | Parallelizable now |
|---|---|---|---|
| A — Retrieval + clustering | [spec A](2026-07-24-p1-hardening-a-retrieval-clustering-design.md) | none (reads `symbols`) | ✅ |
| B — Commit-pinned evidence | [spec B](2026-07-24-p1-hardening-b-commit-pinning-design.md) | none (reads git) | ✅ |
| C — Review UI | [spec C](2026-07-24-p1-hardening-c-review-ui-design.md) | none (reads `claims/*.yaml`) | ✅ |
| D — Why-layer (Pass 2) | [spec D](2026-07-25-p1-hardening-d-why-layer-design.md) | A's clustering + B's commit anchors (both landed) | ✅ specced 2026-07-25 |

A, B, and C touch disjoint code (the one shared interface — cluster metadata — is produced by A
and read by C, which degrades gracefully when it is absent). Each gets its own spec → plan →
implementation cycle. **D is specced after A and B land**, so it is built on their clustering and
commit-anchor interfaces rather than against the spike's throwaway inlining + `file:line` refs.
A and B have since landed and D is specced; its scope was narrowed to the core Pass 2 chain
(span archaeology → artifact fetch → why-synthesis → corroboration verification → the ≥0.80
gate), with retro-ADRs, Confluence, and why-layer review-UI grammar split into follow-ups.

## Cross-cutting decisions

- **Embeddings (Spec A):** local Ollama, `qwen3-embedding:4b` primary / `:0.6b` CI fallback, via
  `/api/embed` — no hosted API, no torch dependency. Vectors record `(model, dims)`; changing the
  model re-embeds.
- **Staleness (Spec B):** content/blob-hash anchored — flags semantic edits to a cited span,
  ignores cosmetic reflow, survives line-number drift.
- **Review (Spec C):** local FastAPI app writing accept/edit/reject back into the git-tracked
  claim YAML; prose fallback for every claim type so it ships before all visual grammars exist.

## Carried-over spike finding (already partially done)

The spike recommended the verifier check citation completeness against *every* symbol a claim
names (would have caught 3 of the 5 real defects). That tightening is already in the verify prompt
(`claims/recover.py` `VERIFY_SYSTEM`). No separate spec — noted here so it is not re-litigated.
