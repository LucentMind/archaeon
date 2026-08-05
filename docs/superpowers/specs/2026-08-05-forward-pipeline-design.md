# P2 — Forward Pipeline: PR-driven living requirements and ADRs

**Status:** design, approved for planning · **Date:** 2026-08-05
**Track:** P2, the first forward-looking pipeline. Archaeology (P0 evidence lake + P1 recovery)
becomes the cold-set half of a two-pipeline coverage story; see §9.
**Depends on:** P0 connectors (`pr_connector`, `git_connector`, `jira_connector`), P1-A retrieval
(`retrieval/embed.py`, `retrieval/cluster.py`), P1-B commit pinning (`claims/pin.py`),
P1 recovery (`claims/recover.py`, `claims/why.py`), review store (`review/store.py`).
**Consumed by:** the MCP surface (design §8) and the review UI (Spec C), both unchanged by this
spec beyond reading the new schema fields.

Related: [design spec](2026-07-23-archaeon-design.md) ·
[Spec A retrieval + clustering](2026-07-24-p1-hardening-a-retrieval-clustering-design.md) ·
[Spec B commit pinning](2026-07-24-p1-hardening-b-commit-pinning-design.md) ·
[Spec D why-layer](2026-07-25-p1-hardening-d-why-layer-design.md) ·
why-layer validation runbook *(internal validation run — not in this repo)*

---

## 1. Problem

Archaeology recovers claims from the weakest evidence the project will ever have: drifted
Confluence, ancient commits, tickets whose authors have left. It pays a large one-time cost for a
snapshot that begins decaying the moment it is produced.

Meanwhile every merged PR carries exactly the evidence archaeology is straining to reconstruct — a
diff, a ticket, a description, a review discussion, and a named human — delivered as a small,
dated delta. Precision per unit of LLM spend is far better forward than backward.

This spec defines the forward pipeline: a PR-triggered system that (a) warns pre-merge when a
change appears to contradict a recovered requirement, and (b) post-merge updates the claim store
so that requirements and ADRs stay live rather than becoming another artifact that drifts.

### 1.1 The premise this rests on, stated explicitly

The claim store is the living document; requirements docs and ADRs are **rendered views** over it,
never hand-edited sources. This premise is not yet demonstrated. §7 defines the spike that tests
it before the rest of the pipeline is built, and the exit question that decides whether the
premise survives.

## 2. Goals / Non-goals

**Goals**
- Pre-merge: flag, at high precision, when a PR contradicts an existing verified requirement.
- Post-merge: absorb the PR into the claim store as an add / amend / supersede / retire delta.
- Detect ADR-worthy decisions from PR discussion, at high precision, never auto-landed.
- Render requirements docs and MADR ADRs from the claim store, with no hand editing.
- Keep exception-based adjudication: routine updates land, exceptions queue for a human.
- Reuse the archaeology extraction path rather than building a second, divergent one.

**Non-goals (v1)**
- Cross-repo or cross-component impact analysis.
- Auto-fixing or auto-amending a PR's code.
- PR hosts beyond what the `gh` CLI covers.
- Retiring archaeology — it remains the only coverage path for unchanged code (§9).
- Recall optimization on the guardrail. Precision first; recall is measured but not gated in v1.

## 3. Decisions taken

| Decision | Choice | Rationale |
|---|---|---|
| Living document substrate | Claim store; docs and ADRs are rendered views | One source of truth; git gives history for free |
| Adjudication | Exception-based (§5.4) | Matches the approved review pillar; keeps the doc live without a review backlog |
| Trigger point | Pre-merge participant from day one, plus post-merge absorb | Best evidence quality; accepted cost is CI integration and org buy-in |
| Bot's primary job | Guardrail — flag contradictions | Serves success criterion #1; fails safe; earns trust before asking for anything |
| Architecture | Two passes over one shared retrieval layer | Guardrail precision tunable independently of doc-update coverage |

**Consequence of the pre-merge choice:** an open PR may never merge, and may change after the bot
speaks. Therefore **`guard` never writes to the claim store.** One classifier, two surfaces: a
pre-merge advisory run that emits only a PR comment, and a post-merge authoritative run that
writes. Whether the comment is a required check is a config knob (`[forward] blocking`), not an
architectural decision.

## 4. Architecture

New package `src/archaeon/forward/`, plus a new `src/archaeon/render/`.

| Unit | Job | Reuses |
|---|---|---|
| `forward/event.py` | Normalize a PR into a `PullRequestEvent`: head/base/merge sha, ticket keys, changed file→hunk ranges, discussion text | `connectors/pr_connector.py`, `analysis/link_heuristics.py` |
| `forward/candidates.py` | PR → ranked candidate claims. **No LLM.** | `codegraph/`, `claims/pin.py`, `retrieval/embed.py`, `analysis/coupling.py` |
| `forward/guard.py` | Pre-merge contradiction detection (propose → ground → refute) | `llm.py`, `cost.py` |
| `forward/delta.py` | Post-merge reconciliation and delta classification | `llm.py`, `claims/recover.py`, `claims/why.py` |
| `forward/adr.py` | ADR candidate detection from PR discussion | `llm.py` |
| `forward/apply.py` | Write claim deltas; route exceptions to the review queue | `claims/schema.py`, `review/store.py` |
| `forward/comment.py` | Render the PR comment | — |
| `render/requirements.py` | Claim store → requirements markdown | `retrieval/cluster.py` |
| `render/adr.py` | `decision` claims → MADR files | — |

Two CLI entry points over one shared front half:

```
PR opened/updated  →  archaeon guard  --pr N  →  event → candidates → guard      →  PR comment
PR merged          →  archaeon absorb --pr N  →  event → candidates → delta+adr  →  claim writes + exception queue
                      archaeon render --out docs/                                →  requirements.md + adr/*.md
```

### 4.1 Invariants

1. **`guard` never writes.** Read-only over the claim store. An unmerged PR is a hypothesis.
2. **`absorb` is the sole forward writer and is idempotent per `(pr, claim_id)`.** Re-running a
   merged PR converges rather than duplicating, matching the P0 connector discipline.
3. **`absorb` pins against the merge commit** via `claims/pin.py`, so every span it writes anchors
   to a real mainline commit. `guard` pins against the PR head for display only and discards it.
4. **The forward pipeline can never write `status: expert_accepted`.** Only a human sets that.
5. **Archaeology never overwrites a witnessed claim** (§9.3).

### 4.2 Candidate retrieval (`candidates.py`)

Hybrid, mirroring the graph + embedding approach already used in `retrieval/cluster.py`:

- **Primary anchor (mechanical):** diff hunks → changed symbols via the code graph → claims whose
  `symbols` list intersects, or whose commit-pinned evidence spans
  (`Evidence.line_start/line_end/blob_sha`) overlap a changed hunk.
- **Secondary sweep (embeddings):** embed the PR title, body and diff summary; retrieve top-k
  nearest claim statements using the Spec A Ollama stack. Catches invariants broken indirectly,
  which the symbol anchor misses by construction.
- **Ranking:** `analysis/coupling.py` scores as an additional signal.

Output: ranked `Candidate(claim, anchor_reason, score)`.

**This is the cost gate.** No symbol intersection, no pinned-span overlap, and no embedding hit
above `embed_threshold` → zero candidates → zero LLM spend and no comment. The large majority of
PRs are expected to exit here.

## 5. Claim schema and store semantics

### 5.1 Schema delta (`claims/schema.py`)

```python
@dataclass
class Origin:
    provenance: str = "recovered"          # recovered | witnessed  (see section 9.2)
    created_by_pr: str | None = None       # "owner/repo#1234"
    created_at_commit: str | None = None   # merge commit sha
    last_amended_by_pr: str | None = None
    last_amended_at_commit: str | None = None
    witnessed_by_pr: str | None = None     # PR that first confirmed a recovered claim

@dataclass
class Claim:
    ...
    modality: str | None = None            # must | should | incidental
    supersedes: list = field(default_factory=list)
    superseded_by: str | None = None
    origin: Origin | None = None
    alternatives: list = field(default_factory=list)   # decision claims only
    consequences: list = field(default_factory=list)   # decision claims only

STATUSES |= {"superseded", "retired"}
WHY_CLAIM_TYPES |= {"decision"}            # ADRs live in the same store
```

### 5.2 Modality is derived, never asserted

A claim may rise above `incidental` only when a why-claim linked through `explains` carries
`corroboration: corroborated`. **Code alone can never justify `must`.**

This is the rule that keeps a rendered document honest. Code is extensional — it shows what the
system does, never whether that behavior is a promise or an accident of how someone wrote a guard.
Only artifacts carry modality. Without this rule the renderer freezes incidental behavior into law,
and an agent consuming the document refuses legitimate refactors.

The rule is mechanically enforceable and requires no model judgment.

### 5.3 Lineage: amend, supersede, retire

- **Amend** — meaning preserved, detail moved (retry limit 3 → 5). Rewrite in place; bump
  `origin.last_amended_by_pr` / `last_amended_at_commit`.
- **Supersede** — meaning changed. Mint a new claim id with `supersedes: [old_id]`; set the old
  claim's `status: superseded` and `superseded_by`.
- **Retire** — no longer applies (feature deleted). `status: retired`. Distinct from `rejected`,
  which means "was never true".
- **Nothing is ever deleted.** Superseded and retired claims stay on disk and render in the
  document's History appendix.

No `valid_from` field: the claim store is a git repo, so "what did the spec say at release X" is
answered by `git show`. A stored temporal column would duplicate that, badly.

No stored section ordering: derived at render time from pinned evidence position.

### 5.4 The exception set

This governs `absorb` deltas. `guard` has no queue — it only posts comments.

Auto-lands without a human:

- `add` of a claim whose why-layer is `corroborated` and which contradicts nothing.
- `amend` of a non-contradicted claim where the change is a detail move with corroborating
  artifact evidence.
- Promotion of `origin.provenance` from `recovered` to `witnessed` (§9.2).

Queues as an exception for adjudication:

- Any proposed `supersede` or `retire` — which is how a contradiction with an existing claim
  surfaces at absorb time.
- Any ADR / `decision` candidate.
- Any `add` or `amend` whose evidence is `code_inferred` only.
- Any modality raise.
- Any delta that hits a `StaleClaimError` (§8).

### 5.5 Store writes

`claims/schema.py::save_claim` generalizes from its current `status`/`statement`-only mutation to
an allowlisted field writer, keeping minimal-diff YAML rewriting, `expected_version`, and
`StaleClaimError`. `absorb` must always pass `expected_version`, so a human editing in the review
UI is never clobbered by a merge landing concurrently.

## 6. The two passes

### 6.1 Guardrail (`guard.py`, pre-merge)

Per candidate above threshold, three stages — deliberately the same shape as `why.py`, whose
grounding pattern has already survived review:

1. **Propose** (expensive model). Input: claim statement, its pinned evidence excerpt (pre-PR
   code), the diff hunks intersecting that span, and the PR description. Output
   `{verdict: satisfied | broken | unclear, argument, cited_hunk}`.
2. **Mechanical grounding** (no LLM). `cited_hunk` must exist in the actual diff. A fabricated
   citation is dropped before the second model call, exactly as `why.py` drops fabricated artifact
   excerpts.
3. **Refute** (separate call, adversarial). Prompted to *defend* the claim as still holding, given
   stage 1's argument. Defaults to refuted when uncertain.

Only `broken` verdicts surviving stage 3 are posted. `unclear` never posts. **Silence is always
the safe output.**

**Who may speak.** Only claims that are `corroborated` **and** (`machine_verified` or
`expert_accepted`) can raise a blocking finding. `recovered`, `contested`, and `code_inferred`
claims are suppressed by default; `[forward] surface_unverified` surfaces them as non-blocking FYI
lines. The pipeline does not cry wolf citing a claim it has not verified itself.

**Caps are never silent.** Candidates are capped at `max_candidates` (default 8) by rank; if
truncation occurred, the comment says so. If the diff exceeds the token budget, the run degrades to
symbol-anchored candidates only and states the reduced coverage.

**The comment.** Per finding: claim id and statement, the ticket/PR it traces to, the specific
hunk, and one line of reasoning. Plus one affordance:

> Reply `@archaeon accept: <reason>` if this change is intentional.

That reply is the highest-value evidence in the system. An author explaining why they deliberately
broke a requirement is precisely the rationale that cannot be recovered from code or reconstructed
later — captured at the moment they know it, as a byproduct of a check they already wanted. On
merge, `absorb` reads these replies as first-class corroborating evidence for the supersession.

Cost is reported through `cost.py`, as `synthesize` and `why` already do.

### 6.2 Absorb (`delta.py` + `adr.py`, post-merge)

`absorb` is **not a new extractor**. It re-runs the existing pipeline on a narrow scope and
reconciles:

```
absorb --pr N:
  1. candidates(PR)                        → existing claims in the blast radius
  2. synthesize(scope = changed symbols)   → proposed claims from post-merge code   [recover.py]
  3. why(proposed, artifacts = this PR + ticket + discussion + @archaeon replies)    [why.py]
  4. reconcile(proposed × candidates)      → {unchanged | amend | supersede | retire | add}   ← new
  5. adr_detect(PR discussion)             → decision-claim candidates                        ← new
  6. apply(deltas)                         → auto-land or queue
```

Steps 2–3 being literally the archaeology path is load-bearing: it guarantees a forward-written
claim and a recovered claim mean the same thing, and prevents the two pipelines from drifting into
two dialects of "claim".

**Reconcile** is the only genuinely new model job, and it is pairwise and small. Given proposed
claim P and existing claim E over the same anchor: same meaning → `unchanged` or `amend`; different
meaning → `supersede`; E's anchor no longer exists → `retire`. Proposed claims matching nothing
become `add`, after an embedding + symbol dedupe against the whole store so archaeology's findings
are not re-added.

`retire` has a free mechanical trigger: the pinned span is gone or the symbol was deleted. Detected
without an LLM, confirmed with one.

**Status of forward-written claims.** An `add` inherits whatever status `recover.py`'s existing
verify path assigns (`machine_verified` or `contested`) — absorb does not assign status itself.
An `amend` preserves the existing claim's status unless the amendment invalidates the prior
verification, in which case it drops to `contested` and queues. Consequence: a `contested`
forward-added claim does not reach the rendered document body (§7) until a human accepts it.

**The why-layer forward is a fundamentally easier problem than the why-layer backward.** Archaeology
guesses which of a thousand old commits explains a span; absorb knows — the PR that just merged,
its ticket, its discussion. Same grounding check, expected far higher corroboration rate. This is
the pipeline's central thesis and §10 gates it.

**ADR detection** is a rare-event classifier and is treated as one. Mechanical prefilter first
(discussion exceeds `adr_min_comments`, or `adr_trigger_paths` touched — build, dependency, or
interface files — or a cross-module diff), then a cheap screen for decision-shaped language
(alternatives raised, disagreement resolved, "we went with X instead of Y"), then expensive
extraction of decision + alternatives + consequences.

ADR candidates **never auto-land**. Missing an ADR is recoverable — archaeology can find it later.
Inventing one poisons the document permanently.

## 7. Rendering (`render/`)

Pure functions over the claim store. No LLM, no judgment.

**Requirements doc.** Sections are clusters (feature areas); ordering within a section derives from
pinned evidence position.

- Only `machine_verified` / `expert_accepted` claims, not superseded or retired, render in the body.
- Modality drives voice: `must` → "shall", `should` → "should", and `incidental` renders in a
  separate **"Observed behavior — not a requirement"** subsection. That separation is the entire
  reason modality exists: an agent reading the body gets constraints it must honor, an agent
  reading the appendix gets behavior it is free to change.
- Every rendered line carries its claim id, evidence link, and traced ticket. Non-negotiable — the
  citation *is* the trust mechanism, and a requirements doc without it is plausible prose.
- `origin.provenance` is rendered: "recovered, unwitnessed" versus "witnessed by PR #1234".
- Superseded and retired claims render in a History appendix pointing at what replaced them.

**ADRs.** One MADR file per `decision` claim: Context (from the why-claim), Decision (statement),
Alternatives, Consequences, Status, plus PR/ticket provenance.

Both outputs carry a `GENERATED — edit claims, not this file` banner, and CI checks that
regeneration produces no diff. That check is what keeps the markdown a view rather than a second
source of truth.

Agents do not read this markdown — they read claims through the MCP surface (design §8). The
rendered document exists for the humans deciding whether to trust the agents.

### 7.1 Premise spike — first task in the plan

Run `render` against the 36 existing `claims_motor_ctrl` claims **before building any of
`forward/`**. Those claims have no modality, no lineage, no origin, so everything lands in the
observed-behavior appendix and nothing renders as a requirement.

That is the informative result, not a failed test: it shows exactly what an un-enriched store
yields and isolates what enrichment buys.

**Exit question, stated now so it cannot be rationalized later:** would a reviewer recognize this
as a requirements document? If no, the claim-store-as-document premise needs revisiting before
absorb is built on top of it.

## 8. Failure handling

**`guard` fails open, always.** Connector down, model overloaded, Ollama unreachable → post
nothing, exit 0, note it in the run record. A bot that blocks merges when it breaks is removed
within a week. `[forward] blocking` can invert this; it defaults off.

Everything else degrades loudly rather than silently:

| Failure | Behavior |
|---|---|
| Ollama unavailable | Symbol-anchored candidates only; run record states reduced coverage |
| Model call errored (overload, turn exhaustion) | Treated as `unclear`, no post; counted in `cost.py` errored-call accounting |
| `StaleClaimError` on apply | A human edited concurrently. Do not clobber, do not retry — route the delta to the exception queue |
| `absorb` interrupted | Idempotent per `(pr, claim_id)` plus a per-PR run record; re-run converges |
| PR merged before `guard` ran | No dependency; `absorb` runs standalone |

### 8.1 Known upstream dependency: merge-commit pinning

The P0 known limitation — `git log --no-merges` means a `pr→commit` link whose `dst_ref` is the
true merge commit does not join to `commits.sha` and can dangle — is harmless for the P0 exit
metric but **not** harmless here, because `absorb` pins against the merge commit. Squash and rebase
merges compound it. This must be fixed or explicitly worked around before absorb can pin correctly,
and belongs early in the implementation plan.

## 9. Archaeology's revised role

Archaeology is not scaffolding to be discarded. It is the other half of a coverage story.

**Forward covers the hot set; archaeology covers the cold set.** The forward pipeline only ever sees
the blast radius of merged PRs. In a 3M-LOC monorepo most code does not change in a given year, and
that code has no PR to witness it. Archaeology is the only path to it. Neither pipeline alone
produces a document; together they do.

### 9.1 The bar drops for hot code, not for cold code

A weak archaeology claim on code that gets touched is self-correcting — absorb re-synthesizes that
span and reconciles. A weak claim on cold code stays wrong indefinitely. The why-layer gate
(≥ 0.80 corroborated precision) therefore still matters, but for a smaller and better-defined
population than it has been treated as. It should stop being the blocker gating all further work.

### 9.2 Provenance and promotion

`Origin.provenance` distinguishes `recovered` (archaeology inferred it) from `witnessed` (a real PR
confirmed it). A recovered claim is **promoted to witnessed** the first time absorb reconciles a PR
against its span and returns `unchanged` or `amend`; `origin.witnessed_by_pr` records which.

This makes the store self-healing along the axis where accuracy matters most, and produces a trust
metric the project currently lacks: **the fraction of the document witnessed by an actual change.**
It climbs on its own as the team works, and it belongs on the dashboard.

### 9.3 The no-overwrite invariant

Two writers now share one store. **Archaeology never overwrites a witnessed claim.** A re-run is
additive over unwitnessed regions only. Without this rule, re-running archaeology on a component
silently reverts everything the forward pipeline learned.

### 9.4 Operating cadence

Run archaeology on component onboarding and periodically for cold code — not continuously.

### 9.5 Migration

The 36 existing `claims_motor_ctrl` claims are stamped with
`origin.provenance = recovered` and `modality = incidental` so they render honestly under the new
schema. One-shot migration script, covered by a test.

## 10. Validation and gates

### 10.1 The PR-holdout backtest

Replay N recent merged PRs through `guard` against the claim store **as it stood before each one**.
Bug-fix PRs are natural positives; pure refactors are natural negatives. This yields a labeled
evaluation set at near-zero labeling cost and allows the gates to be measured before a single live
PR sees a comment.

The same backtest measures the archaeology why-gate more cheaply than hand-labeling ~100 commits:
replay backward recovery blind against the pre-merge tree and check the recovered rationale against
what the PR actually said. **The forward work unblocks the archaeology gate rather than competing
with it.**

### 10.2 Gates

| # | Gate | Threshold |
|---|---|---|
| 1 | Guardrail precision on the backtest — of findings posted, the share a reviewer calls real | ≥ 0.90 |
| 2 | Forward why-corroboration rate on the same component | ≥ 0.85 absolute **and** ≥ 15 points above the archaeology run |
| 3 | Reconcile accuracy on hand-labeled `(proposed, existing)` pairs | ≥ 0.90 |
| 4 | Renderer premise spike (§7.1) | human verdict, no numeric gate |

Gate 2 is the thesis check. If forward corroboration is not materially better than backward, the
premise of this pipeline is wrong and that should surface in week one, not month three.

Guardrail recall is measured and reported but not gated in v1.

### 10.3 Tests

Most of this is LLM-free and fast to test:

- `candidates.py` set-intersection over synthetic diffs and pinned spans.
- Fabricated-hunk rejection, mirroring the existing why-layer grounding tests.
- `absorb` run twice over the same PR produces an identical store (idempotency).
- Concurrent edit → `StaleClaimError` → exception queue, no clobber.
- Archaeology re-run does not modify a witnessed claim (§9.3).
- Golden-file test over a fixture claim store for both renderers.
- `reconcile` table-driven fixtures with recorded model responses.
- Migration script (§9.5) covered by a test.

## 11. Configuration

New `[forward]` block, mirroring `[llm]` and `[retrieval]`:

```toml
[forward]
guard_model = "..."          # expensive: propose + refute
delta_model = "..."          # cheaper: reconcile
adr_model = "..."            # expensive: ADR extraction
max_candidates = 8
embed_threshold = 0.75       # secondary-sweep cutoff; tuned during gate 1 measurement
blocking = false             # is the guard comment a required check
surface_unverified = false   # show non-blocking FYI for unverified claims
adr_min_comments = 5         # mechanical ADR prefilter; tuned during gate 3 measurement
adr_trigger_paths = []       # build / dependency / interface paths, per component
```

`embed_threshold` and `adr_min_comments` start at these values and are re-tuned against the
backtest (§10.1) before the pipeline runs on live PRs. Both are recall/cost knobs, so they are
tuned against measured data rather than guessed once.

## 12. Implementation order and decomposition

This spec is deliberately larger than one implementation plan. It decomposes into three sequenced
tracks, in the same style as the P1 hardening A/B/C/D split. Each track is gated by the previous
one, so they are sequential rather than parallelizable.

**Track 1 — Premise and foundations.** Decides whether the rest is worth building.

1. Renderer (`render/`) + premise spike (§7.1).
2. Schema delta + `save_claim` generalization + migration (§5, §9.5).
3. Merge-commit pinning fix (§8.1).

*Exit:* the §7.1 human verdict. A no here stops Tracks 2 and 3 and sends the claim-store-as-
document premise back for redesign.

**Track 2 — Guardrail.** The trust-earning surface; independently valuable if Track 3 never ships.

4. `event.py` + `candidates.py` — the LLM-free half, fully testable without model calls.
5. Backtest harness (§10.1).
6. `guard.py` + `comment.py`; measure gate 1.
7. CI wiring for the pre-merge surface.

*Exit:* gate 1 (guardrail precision ≥ 0.90).

**Track 3 — Absorb and ADRs.** Makes the document actually live.

8. `delta.py` + `apply.py` + provenance promotion; measure gates 2 and 3.
9. `adr.py`.

*Exit:* gates 2 and 3.

Each track gets its own implementation plan under `docs/superpowers/plans/`.
