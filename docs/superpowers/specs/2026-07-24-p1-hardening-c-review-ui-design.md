# P1 Hardening — Spec C: Review UI (local web app, writes back to YAML)

**Status:** design, approved for planning · **Date:** 2026-07-24
**Track:** C of 3 parallelizable P1-hardening specs (A retrieval+clustering, B commit-pinning,
C review UI).
**Depends on:** nothing upstream — reads the `claims/*.yaml` artifacts that already exist.
Reads Spec A's cluster metadata *if present*; degrades to flat per-file grouping if not.

Related: [design spec §7 review and visualization](2026-07-23-archaeon-design.md) ·
P1 spike note *(internal validation run — not in this repo)*

## 1. Problem

Review today is a hand-edited `claim_labels.csv` (the spike's Step 3–4). The design (§7) calls for
the review and browse surface to be the same thing: a component treemap → feature clusters → claim
cards, where accepted claims stay as cards and every accept/edit/reject is a git-visible change.
We need that surface, replacing the CSV, with review actions persisting back into the git-tracked
`claims/*.yaml`.

## 2. Goals / Non-goals

**Goals**
- Browse claims: component → cluster → claim card → evidence, colored by verification state.
- Review actions — **accept / edit / reject** with single-keystroke bindings — persisted into the
  claim YAML, producing a normal git diff (design §10: all status changes in git).
- Per-claim-type **visual grammar** with a **prose fallback** so the UI works before every grammar
  exists.
- A **queue** view sorted by impact × uncertainty, surfacing contested claims with their
  counter-evidence.

**Non-goals**
- Authentication / multi-user server — this is a local, single-reviewer tool.
- The MCP surface (§8) — separate track.
- Editing evidence/citations by hand — reviewers accept/edit the *statement* and set status;
  citations come from synthesis.

## 3. Architecture

### 3.1 Backend — `review/server.py` (new)
A small FastAPI app over the claim store (a `claims/` directory of YAML):
- `GET /components` — components with verified/contested/unrecovered counts (treemap data).
- `GET /clusters?component=` — clusters (from Spec A metadata) or a flat per-file grouping if no
  cluster metadata is present.
- `GET /claims?cluster=|component=` — claim cards with statement, type, status, confidence,
  evidence rows (pre-located excerpts), and counter-evidence when contested.
- `POST /claims/{id}` — set status (`expert_accepted` | `rejected`), optionally an edited
  statement; writes back to that claim's YAML file.
- Every write goes through one `save_claim` path that preserves field order and untouched fields,
  so diffs stay minimal and reviewable.
- **Status enum delta:** the claim schema today allows `recovered | machine_verified | contested`
  (`claims/schema.py`). This track adds the review terminal states `expert_accepted` and
  `rejected` (already used in design §9/§10), so this spec owns extending that enum.

### 3.2 Frontend — static assets served by the backend
- **Entry:** component treemap colored by verification state (verified / contested /
  unrecovered). Coverage metrics lead; no full-graph hairball (design §7).
- **Drill:** component → clusters → claim cards → evidence.
- **Claim card:** statement; the claim rendered in its type's visual grammar; evidence rows with
  excerpts; verifier counter-evidence when contested; accept / edit / reject with single-keystroke
  bindings.
- Vanilla JS + a lightweight rendering lib for diagrams (e.g. Mermaid for sequence/state) — no
  heavy SPA framework; keep it a static bundle the FastAPI app serves.

### 3.3 Per-type visual grammar (fallback-first)
Renderer registry keyed by claim `type`, prose fallback for any unmapped type:
- `state_transition` → state diagram with the claimed transition highlighted.
- `threshold` → parameter/range table.
- `interaction_sequence` → sequence diagram.
- `conditional_rule` / `invariant` / `timing_budget` → decision/parameter table or prose.
- **Every type falls back to prose**, so the UI ships before all grammars are built and never
  blocks on an unmapped type.

### 3.4 Queue view
Claims sorted by **impact × uncertainty**; contested claims surfaced with counter-evidence
attached; shows the reviewer's remaining load (design §7: "a reviewer's daily load is minutes").

## 4. Data flow

```
claims/*.yaml (+ Spec A cluster metadata, optional)
   ──► backend reads/parses ──► REST ──► frontend renders treemap/cards/queue
   ◄── POST /claims/{id} (accept/edit/reject) ──► save_claim ──► claims/*.yaml (git diff)
```

## 5. Error handling / degradation

- **No cluster metadata (Spec A not landed)** → group claims flat, per file/feature; the UI is
  fully functional without clustering.
- **Concurrent edit / file changed on disk** → the backend carries a read-version token; a `POST`
  against a stale version is rejected with a reload prompt (last-writer-wins is *not* allowed to
  silently clobber a hand edit).
- **Malformed claim YAML** → rendered as a broken-card with the parse error, not a server crash;
  the rest of the store still loads.
- **Write failure** (permission / disk) → surfaced in the UI; the in-memory state is not marked
  accepted so the reviewer can retry.

## 6. Testing

- **Backend round-trip (unit):** `POST` a status change → the YAML file reflects it and reloads
  with the new status; untouched fields and field order are preserved (minimal diff).
- **Stale-write rejection (unit):** a `POST` with an out-of-date version token is rejected.
- **Degradation (unit):** with no cluster metadata, `GET /clusters` returns a flat grouping.
- **Renderers (snapshot):** one claim of each type renders in its grammar; an unmapped type renders
  as prose.
- **Frontend smoke (headless):** treemap renders; an accept action issues the `POST` and the file
  changes on disk.

## 7. Relationship to the spike's CSV flow

The `claims-eval` / `claim_labels.csv` path (spike Steps 4–5) stays as the **precision-measurement
harness**; this UI is the **review** surface. A follow-up can export accepted/rejected decisions
from the YAML into the eval labels format, but that export is out of scope for this spec.

## 8. Open questions

- Impact and uncertainty scoring for the queue — start with `uncertainty = 1 - confidence` and a
  simple impact proxy (symbol fan-in / cluster size); refine later.
- Diagram library choice (Mermaid vs. hand-rolled SVG) for the per-type grammars — decide during
  planning; prose fallback de-risks it either way.
