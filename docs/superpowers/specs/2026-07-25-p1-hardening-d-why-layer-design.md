# Spec D — Why-layer recovery (Pass 2)

**Date:** 2026-07-25
**Track:** P1 hardening D (see [overview](2026-07-24-p1-hardening-overview.md))
**Upstream:** Specs [A](2026-07-24-p1-hardening-a-retrieval-clustering-design.md) (clustering),
[B](2026-07-24-p1-hardening-b-commit-pinning-design.md) (commit anchors) — both landed.

## 1. Problem

Pass 1 recovers the what-layer from code alone and clears its gate. The why-layer —
requirement intent and decision rationale — cannot be settled by code: code shows *that* a
puck animation interpolates from its current position, never *why*. The design's §6 Pass 2
resolves this per feature by walking from the code behind a claim out to the commits, PRs, and
tickets that shaped it, and using those artifacts as corroborating evidence.

Track D was deliberately deferred until A and B landed so it could be built on their
clustering and commit-anchor interfaces rather than the spike's throwaway `file:line` refs.

Feasibility was verified against the real repo before writing this spec. For a 20-line span in
`navigation_status_animator_base.cpp`, `git log -L` returns 3 shaping commits; 2 resolve
through `links` to PRs 1234/5678 and tickets MOT-1282/MOT-608. MOT-608's
description explains that statuses arriving at irregular frequency on slow devices caused puck
jitter — exactly the rationale a what-claim about animation state transitions cannot express.
The third commit is absent from `commits` (outside ingest scope), which §5 handles explicitly.

## 2. Goals / Non-goals

**Goals**

- Recover why-layer claims (intent, rationale, constraint origin, tradeoff) grounded in Jira
  tickets, PR bodies, and PR review comments reached from the code behind existing what-claims.
- Never present an uncorroborated rationale as verified. A why-claim with only its code
  hypothesis is retained as `code_inferred` at a reduced confidence tier and never
  auto-verified.
- Make fabricated citations mechanically detectable, in the spirit of Spec B's content hashes.
- Measure corroborated why-layer precision against the design's ≥ 0.80 gate.

**Non-goals**

- Retro-ADR / MADR decision-record generation (follow-up spec).
- Confluence, live CQL or local export. There is no wiki corpus for the validation component:
  `wiki_pages` is 0 and the configured `export_dir` does not exist. The corpus loader is
  pluggable so a wiki source drops in later without a schema change.
- Why-specific review-UI visual grammar. Spec C's prose fallback renders why-claims adequately.
- Incremental re-verification on merged diffs (design §6 "incremental operation").
- On-demand fetching of commits outside the ingest scope — that would make cost unbounded and
  runs non-reproducible.

## 3. Decisions taken

| Decision | Rationale |
|---|---|
| Separate `archeon why` stage, not a `synthesize` flag | Matches the one-command-per-stage CLI convention; lets a why-run be re-run or improved without re-paying for expensive what-synthesis; can hard-fail on missing artifacts instead of silently degrading |
| Cluster-scoped retrieval, claim-attributed output | Overlapping tickets are fetched and paid for once per cluster, while `explains: [CLM-…]` preserves per-claim traceability |
| Mechanical grounding **then** adversarial verification | The deterministic pass kills fabricated citations for free; letting one model both write and grade a citation lets a plausibly-worded fake survive |
| `corroboration` as a field orthogonal to `status` | The axes are independent — a code-inferred claim can still be contested or expert-accepted. Collapsing them into `STATUSES` would make those states unrepresentable and would erase, on expert acceptance, the fact that a claim was never corroborated |
| `WHY-####` id prefix | No collision with `CLM-`; `synthesize`'s re-id loop cannot clobber why-claims; `load_claims` picks up both layers unchanged |

## 4. Architecture

`archeon why` reads the what-claims in `claims/*.yaml` and writes why-claims back to the same
directory as `WHY-####.yaml`.

| Unit | Purpose | Depends on |
|---|---|---|
| `retrieval/archaeology.py` (new) | pinned span → shaping commits → ticket keys / PR numbers | git, `links` |
| `claims/why_corpus.py` (new) | token-bounded artifact corpus per cluster | `tickets`, `prs`, `pr_comments` |
| `claims/why.py` (new) | synthesize → ground → verify | corpus, `llm.AgentClassifier` |
| `claims/schema.py` | why claim types, `corroboration`, `explains` | — |
| `claims/claim_eval.py` | corroborated-precision metric | — |
| `cli.py` | `why` command | all of the above |

Corpus assembly is split from synthesis so it is independently testable with no LLM.

### 4.1 `retrieval/archaeology.py`

```python
def shaping_commits(repo, path, start, end, rev="HEAD",
                    max_commits=50) -> list[str]
def file_level_commits(repo, path, max_commits=50) -> list[str]
def artifacts_for_commits(conn, shas) -> ArtifactRefs
```

`ArtifactRefs` is a small dataclass of three fields: `tickets: dict[str, set[str]]` and
`prs: dict[int, set[str]]` mapping each artifact to the shaping shas that reached it (the
support counts §4.2 ranks by), plus `unknown: set[str]` for shas absent from `commits`.

`shaping_commits` runs `git log -L<start>,<end>:<path> <rev> --format=%H -s` (`-s` suppresses
the patch; verified to return bare shas). It anchors on the evidence's `commit_sha`, **not**
HEAD: pinned line numbers are only meaningful against the commit they were captured at, which
is precisely what Spec B's anchor provides and what makes this exact rather than approximate.

`git log -L` and `--follow` are mutually exclusive, so `file_level_commits` is a separate
invocation used as the fallback for unpinnable evidence and for `-L` failures.

`artifacts_for_commits` resolves through `links` in both directions: `commit→ticket` directly,
and `pr→commit` (`method='merge_sha'`) to reach PRs, then `pr→ticket`. It consults `pr_commits`
as a secondary path but must not depend on it: measured on the validation repo, `pr_commits`
joins 2 of 8,103 rows because the repo squash-merges (the PR's branch commits never land on
the mainline), while `merge_sha` joins 1,533 of 1,569. Other repos merge differently, so both
paths stay. Shas absent from `commits` are returned as a counted `unknown` set.

### 4.2 `claims/why_corpus.py`

Per cluster: union the pinned spans of the cluster's what-claims, collect shaping commits,
resolve artifacts, then rank by **support** (how many of the cluster's shaping commits reach
that artifact), breaking ties by the artifact's own timestamp, newest first — `tickets.resolved`
falling back to `tickets.created`, `prs.merged_at`, `pr_comments.created` — with a missing
timestamp sorting last. Fill to `why.token_budget`, mirroring `bundle.py`'s existing budgeting.
Each entry carries a stable ref that grounding can resolve: `MOT-608`, `pr:5678`,
`pr_comment:<id>`.

A PR's review comments are included as separate corpus entries under the PR's support score, so
a heavily-discussed PR does not crowd out other artifacts on comment count alone. On the
validation repo PR 5678 carries 1 comment, but `pr_comments` holds 6,663 rows overall, so this
bound matters.

Clusters are derived from the claims' `feature` label, which `synthesize` already sets from the
cluster label — no new join, and it works for `--feature` runs too.

### 4.3 `claims/why.py`

Three functions mirroring `recover.py`'s shape so the codebase stays idiomatic:

- `synthesize_why_claims(label, what_claims, corpus, ask)` — one expensive call per cluster.
  The prompt carries the cluster's what-claim statements as the code hypothesis plus the
  artifact corpus. Each returned claim declares a why type, a statement, `explains`, and
  artifact evidence with a verbatim excerpt.
- `ground_citations(claims, conn)` — **no LLM.** Resolves each artifact ref and checks the
  quoted excerpt actually appears in the stored body, after normalizing whitespace and line
  endings (real PR bodies in the lake contain literal CRLF). Ungrounded evidence is dropped.
- `verify_why_claims(claims, corpus, ask)` — adversarial mid-tier pass over survivors: does the
  artifact *state* this rationale, or merely touch the same topic? Topical drift is the failure
  mode the gate must catch.

`WHY_CLAIM_TYPES = {"intent", "rationale", "constraint_origin", "tradeoff"}`, a separate set
from `CLAIM_TYPES`, validated by layer.

Two rules keep a why-claim's *code* side hallucination-proof:

- **The code hypothesis is copied, never generated.** The model emits only artifact evidence and
  an `explains` list. `synthesize_why_claims` then mechanically copies the primary code evidence
  off each explained what-claim — which Spec B already pinned and Pass 1 already verified. The
  model therefore cannot invent a code ref at all, and every why-claim satisfies the design's
  "a claim with no valid evidence cannot exist" rule even when uncorroborated.
- **`explains` is filtered against real ids.** Entries naming no loaded claim are dropped; a
  claim left with an empty `explains` is discarded, since it would have no code hypothesis and
  no traceability.

## 5. Data flow

```
claims/CLM-*.yaml (pinned, Spec B)
        │  group by feature/cluster label
        ▼
  union pinned spans ──► git log -L @ commit_sha ──► shaping shas
                                                        │
                        links: commit→ticket, pr→commit→ticket
                                                        ▼
                              ranked, token-bounded artifact corpus
                                                        │
      synthesize (expensive) ──► ground_citations (no LLM) ──► verify (mid)
                                                        │
   ├─ grounded + verifier agrees  → machine_verified, corroboration=corroborated
   ├─ grounded + verifier refutes → contested,        corroboration=corroborated
   └─ nothing grounded            → recovered,        corroboration=code_inferred
                                     confidence capped at 0.4, never auto-verified
                                                        ▼
                     pin_claims (already-pinned anchors kept) ──► claims/WHY-*.yaml
```

Only what-claims with status `machine_verified` or `expert_accepted` are enriched. Contested
what-claims are excluded: the synthesis prompt presents them as verified and the code hypothesis
is copied on the premise that Pass 1 already verified them, so feeding a refuted claim in would
spend the expensive model on a false premise.

`pin_claims` already skips non-code evidence, so artifact evidence passes through untouched.

Spec B needed one change after all. `pin_evidence` re-derived an anchor from HEAD unconditionally,
which would have silently re-pointed a why-claim's *copied* hypothesis at whatever now occupies
those line numbers — reporting `pin_status: pinned` over unrelated code whenever a commit shifted
lines between the `synthesize` and `why` runs. It now returns early for evidence already marked
`pinned` or `dirty`. The what-layer is unaffected: `synthesize` builds evidence with
`pin_status` unset, so it still pins normally, and `unpinnable` evidence is still retried (it has
no anchor to corrupt, and a `why` run supplies a fresh `known_paths` that may resolve it).

## 6. Error handling / degradation

A why-run degrades per-unit and never aborts wholesale — the rule inherited from A and B.

| Condition | Behavior |
|---|---|
| `tickets` and `prs` both empty | **Hard fail** before any LLM spend: "run ingest-git, ingest-prs, ingest-jira first". The scoped DB has 0 artifacts today, so a silent degrade would spend on Sonnet to produce nothing but code-inferred claims |
| No `claims/*.yaml` | Hard fail: "run synthesize first" |
| Claim has no pinnable code evidence | Skip its spans; file-level fallback where a path is known |
| `git log -L` fails (rename, path absent from that rev) | File-level fallback for that evidence; counted |
| Shaping sha absent from `commits` | Counted as `unknown` and reported; never fetched on demand |
| Cluster resolves to zero artifacts | Emit no why-claims rather than inventing rationale; counted as uncorroborated |
| Synthesis call raises | Skip that cluster, keep all other clusters' results |
| Verification call raises | That claim becomes `contested` with the error as counter-evidence, matching `verify_claims` |
| Excerpt fails grounding | Drop that evidence; if it was the last artifact, demote to `code_inferred` rather than deleting the claim |

The per-cluster and per-claim exception isolation is deliberate: `verify_claims` carries a
comment recording that an uncaught error there once lost every prior claim's progress, because
`save_claims` runs only after the loop returns. The same hazard applies here.

## 7. Schema changes (additive)

`Claim` gains:

```python
corroboration: str | None = None      # corroborated | code_inferred (why-layer only)
explains: list = field(default_factory=list)   # ids of what-claims this explains
```

`Evidence` needs nothing new: `kind` already carries `ticket` / `pr_comment` (extended with
`pr`), and `role="corroborating"` already exists. `from_dict` defaults both new fields, so every
existing claim file loads unchanged, and `save_claim`'s raw-mapping write already preserves keys
unknown to the dataclass.

No SQL migration. Archaeology reads existing tables; results live in the YAML. `schema.sql` is
untouched.

## 8. Testing (TDD — write first)

Following existing conventions: real temp git repos via the `_git` / `_repo` subprocess helpers
in `test_claims_pin.py`, in-memory sqlite loaded from `schema.sql`, and an injected fake `ask`.

- `test_archaeology.py` — a span edited across three commits returns them newest-first;
  `max_commits` respected; anchoring on a non-HEAD rev; renamed-path fallback.
  `artifacts_for_commits` resolving via `merge_sha` and via the `pr_commits` secondary path,
  and counting unknown shas.
- `test_why_corpus.py` — support-based ranking, token-budget truncation, stable refs. LLM-free.
- `test_why.py` — grounding accepts a verbatim excerpt, rejects a fabricated one, and tolerates
  CRLF/whitespace differences; all three status/corroboration outcomes; confidence capped at
  0.4 for code-inferred; per-cluster and per-claim exception isolation.
- `test_claim_eval.py` — corroborated precision excludes code-inferred claims.
- `test_cli.py` — both hard-fail preconditions, the summary line, and that a `why` run leaves an
  existing `run_cost.json` from `synthesize` intact while writing its own `why_cost.json`.

## 9. The gate

`evaluate_claims` is already layer-keyed. It gains `corroborated_n`, `corroborated_correct`, and
`corroborated_precision` (filtering `corroboration == "corroborated"`) beside the existing
pre-verification and `verified_` numbers.

Exit criterion: **corroborated why-layer precision ≥ 0.80**, reported alongside the
code-inferred count so a thin corroborated slice cannot hide behind a large code-inferred tail.
Per the design's §11 ban on blended numbers, no combined what+why figure is printed.

Validation run: ingest artifacts into the scoped DB (it currently has 0 commits/PRs/tickets),
`archeon why --claims <dir>` (groups are derived from the claims' own `feature` label, so there is
no `--all-clusters` flag; `--feature` scopes a run to one label), hand-label into
`why_labels.csv` (`claim_id,correct`), then
`claims-eval`. No `claims-eval` invocation change is needed: `evaluate_claims` skips unlabeled
claims, so a labels file containing only `WHY-` ids reports the why layer alone even though the
claims directory holds both layers. Labelling must judge the *rationale*, not whether the cited
artifact exists — grounding already guarantees existence, and conflating the two would inflate
the number the gate depends on.

## 10. Config

```toml
[why]
# max_commits_per_span = 50      # cap on git log -L archaeology per span
# token_budget = 40000           # artifact corpus budget per cluster
# model = "claude-sonnet-5"      # falls back to llm.expensive_model
```

## 11. Cost accounting

The [cost spec](2026-07-25-llm-cost-accounting-design.md) has fully landed, so D wires into the
shipped contract rather than shipping an inert seam.

**The meter attaches to the classifier, not to `why.py`.** `AgentClassifier` takes
`meter=` and `stage=` constructor arguments and records inside `_ask`, so `why.py`'s functions
keep taking only an injected `ask` callable and stay LLM-agnostic and meter-agnostic — exactly
as `recover.py` does. `why.py` gets **no** `meter` parameter.

`cli.py`'s `why` command follows `cli_synthesize`: one `CostMeter()` for the whole run, shared by
both classifiers.

```python
meter = CostMeter()
AgentClassifier(model, WHY_SYNTH_SYSTEM,  max_turns=4, meter=meter, stage="why-synth")
AgentClassifier(model, WHY_VERIFY_SYSTEM, max_turns=4, meter=meter, stage="why-verify")
```

Stage names are hyphenated to match the existing `cluster-label` / `synthesize` / `verify`
vocabulary, and both fit the `{stage:<16}` column `format_summary` uses. `ground_citations` is
deterministic, so it contributes no stage — `by_stage` will show only the two LLM stages, which
is the honest picture: grounding is free.

**`why` must not write `run_cost.json`.** `cli_synthesize` writes its cost report to
`<out_dir>/run_cost.json`, and `why` writes into that same claims directory, so reusing the name
would silently destroy the what-layer run's cost record. `why` writes
`<out_dir>/why_cost.json` instead. It reuses the single-probe pattern — one `summary_dict("why")`
feeds both the echoed block and the JSON file, so the two surfaces cannot disagree about the
billing route.

One consequence worth noting for §6: `_ask` records the terminal message *before* the SDK's
exception propagates, so a call that fails and triggers the per-cluster or per-claim isolation
path is still counted in `failed_calls`. Spend from failed why-runs is therefore visible rather
than silently dropped.

## 12. Rollout order

1. Schema fields + `WHY_CLAIM_TYPES` (additive, no behavior change).
2. `archaeology.py` with tests — pure git/SQL, no LLM.
3. `why_corpus.py` with tests — pure SQL, no LLM.
4. `why.py`: grounding first (deterministic, highest-value), then synthesis, then verification.
5. `claim_eval.py` corroborated metric.
6. `cli.py` `why` command with preconditions.
7. Validation run on the scoped component and the ≥ 0.80 gate.
