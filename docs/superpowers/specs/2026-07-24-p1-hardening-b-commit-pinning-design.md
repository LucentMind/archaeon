# P1 Hardening — Spec B: Commit-Pinned Evidence (content/blob-hash anchored)

**Status:** design, approved for planning · **Date:** 2026-07-24
**Track:** B of 3 parallelizable P1-hardening specs (A retrieval+clustering, B commit-pinning,
C review UI).
**Depends on:** nothing upstream (reads git via the existing `git_connector`).
**Consumed by:** the deferred why-layer (Pass 2) and the phase-2 guardrail, which both need to
know when a claim's evidence has drifted.

Related: [design spec §6/§10](2026-07-23-archaeon-design.md) ·
[P1 spike exit checklist — known limitation "evidence refs are file:line, not commit-pinned"](../../p1-spike-exit-checklist.md)

## 1. Problem

Claim evidence today is a bare `file:line` ref plus an excerpt (`Evidence` in `claims/schema.py`).
Line numbers drift the moment code above them changes, and nothing detects when the cited code has
been *edited out from under a claim*. The design (§6 Pass 2, §10 "no claim without valid,
re-fetched citations") requires evidence anchored to a commit and verifiable against the current
tree. Staleness detection is the enrichment the spike explicitly did not cover.

## 2. Goals / Non-goals

**Goals**
- Anchor each piece of evidence to a **commit SHA + a content hash of the exact cited lines**, so
  staleness is decidable and survives line-number drift.
- Provide `is_stale(evidence, repo)` and a `check-staleness` CLI over a component's claims.
- Be **additive and backward-compatible**: existing `file:line`-only evidence still loads.

**Non-goals**
- Re-verifying or re-synthesizing stale claims (that is the incremental/Pass-2 work).
- Blame-based "who changed it" attribution — we detect *that* it changed, not *who*/*why*.
- Cross-file move tracking (`--follow`) — out of scope; a moved file reads as unpinnable/stale.

## 3. Staleness definition (decided)

**Content/blob-hash anchored.** At synthesis time, for each evidence ref:
- resolve to the current `HEAD` `commit_sha`,
- record the file's `blob_sha`,
- record `line_start` / `line_end`,
- compute `content_hash` = hash of the exact cited lines, **whitespace-normalized** (trailing
  whitespace and blank-line-only reflow do not count as change).

An evidence anchor is **stale** iff, at the current working-tree/`HEAD` state, the content at the
anchored location no longer hash-matches. This flags *semantic* edits to the cited span and
ignores cosmetic reflow above it — the property the spike wanted and blame-anchoring lacks.

## 4. Architecture

### 4.1 Evidence schema additions — `claims/schema.py`
Add optional fields to `Evidence` (all default `None`, so old YAML loads unchanged):
`commit_sha`, `blob_sha`, `line_start`, `line_end`, `content_hash`, and `pin_status`
(`pinned` | `unpinnable` | `dirty`).

### 4.2 Anchor capture — `claims/pin.py` (new)
- `pin_evidence(evidence, repo)` — parse `ref` (`path:line` or `path:start-end`), read the span at
  `HEAD` through `git_connector`, fill the anchor fields, set `pin_status="pinned"`.
- Called on each claim's evidence right after `synthesize_claims`, before `save_claims`.
- Whitespace normalization for `content_hash` lives in one helper reused by the staleness check so
  capture and check can never diverge.

### 4.3 Staleness check — `claims/pin.py`
- `is_stale(evidence, repo) -> bool` — recompute the normalized hash of the anchored lines at the
  current tree; compare to `content_hash`.
- `stale_claims(claims, repo)` — returns the claims with any stale primary evidence.

### 4.4 CLI — `check-staleness`
`uv run archaeon check-staleness --claims <dir>` prints each claim whose evidence is stale or
unpinnable, with the anchored `commit_sha` and the ref, as input to re-verification.

## 5. Data flow

```
synthesize ──► synthesize_claims (builds Claim.evidence)
           ──► pin_evidence per evidence  (4.2)  [fills anchors]
           ──► save_claims                        [anchors persisted in claims/*.yaml]

later:
check-staleness ──► is_stale per evidence (4.3) against current HEAD ──► report
```

## 6. Error handling / degradation

- **Ref does not resolve** to a tracked file/line (hallucinated, or the file moved) → anchor left
  null, `pin_status="unpinnable"`. This is the same signal the verifier uses to drop bad
  citations; a claim with only unpinnable evidence is surfaced, not silently kept.
- **Repo dirty** (uncommitted changes) at pin time → pin against `HEAD` and set
  `pin_status="dirty"` so the anchor is known to be provisional.
- **Line range past EOF / out of bounds** → `unpinnable`, recorded with a reason.
- Pinning failures never abort the run — they degrade per-evidence, consistent with the verify
  loop's resilience fix.

## 7. Testing

- **Hash stability (unit):** inserting lines *above* the cited span leaves `content_hash`
  unchanged (survives line-number drift); the anchor's `line_start/end` update but staleness is
  false.
- **Edit inside span (unit):** a semantic edit within the cited lines → `is_stale` true.
- **Cosmetic reflow (unit):** whitespace-only change to the span → not stale (normalization holds).
- **Unpinnable (unit):** a ref to a nonexistent file/line → `pin_status="unpinnable"`, no crash.
- **Backward compat (unit):** a pre-existing `file:line`-only evidence YAML loads with null anchors
  and `is_stale` treats it as unpinnable, not stale.
- **Integration:** pin the `motor_ctrl_impl` claims, edit exactly one cited region, run
  `check-staleness`, confirm **only** that claim reports stale.

## 8. Coordination with Spec A / the why-layer

- Spec B touches `claims/schema.py` (additive `Evidence` fields) and adds `claims/pin.py`; Spec A
  does not touch these, so they parallelize cleanly.
- The deferred why-layer will read `commit_sha` to seed its `git log -L` walk — B provides that
  anchor but does not implement the walk.

## 9. Open questions

- Hash algorithm and normalization exactness (e.g. whether to normalize interior indentation) —
  start with trailing-whitespace + blank-line normalization; revisit if false-stale shows up.
