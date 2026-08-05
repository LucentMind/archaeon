# Archeon — Design Specification

Date: 2026-07-23
Status: draft for review
Related: [Market & research landscape](../../research/2026-07-23-market-research.md)

## 1. Problem

In long-lived software projects, documentation drifts from the product until code is the only
place the truth lives. Understanding expected behavior or the repercussions of a change takes
any engineer a long time, and regression risk stays high. The same failure runs across the
whole chain from product requirement to testing.

Archeon reverse-engineers the requirements and architecture decisions of an existing codebase
from the code and its surrounding artifacts (git history, Jira tickets, PR discussions, code
comments, drifted wikis), represents that recovered knowledge simultaneously for humans
(visual, reviewable) and machines (queryable by AI agents), and uses it to detect gaps,
conflicts, and duplications and to assess the impact and effort of changes.

## 2. Context and constraints

- **First deployment**: internal monorepo, ~3M LOC total, C/C++/embedded, processable as
  individual small-to-medium components. Evidence available: long git history, disciplined
  Jira (ticket keys referenced in commits/PRs), PR review discussions, partially outdated
  Confluence/wiki.
- **Ownership**: built as an internal tool but under the author's IP, designed general-core +
  plugins so a standalone product can grow out of it.
- **LLM strategy**: cloud, Anthropic Agent SDK. Cost-aware from day one via a cheap/expensive
  model split (Haiku for mechanical stages, Sonnet/Opus-class for synthesis). The model
  backend per stage is configuration, so stages can later move to local models.
- **Eventual market**: mid-size product teams (20–200 engineers) with 5–10 year old codebases.

### Real-world ingestion constraints (validated on the production monorepo)

P0 confirmed link recovery on the real component (~58% commit coverage, ~96% agreement with a
manual pass). Running there surfaced four constraints that shape ingestion:

- **One component draws on 11 Jira projects, and those projects span many other components.**
  Fetching Jira by project is intractable and mostly irrelevant. Ingestion is therefore
  **key-driven**: discover ticket keys from the component's own commits, PRs, and branch names,
  then fetch only those tickets.
- **Confluence is too large to export or fetch whole.** It is why-layer/supportive, so it is
  never bulk-ingested; relevant pages are pulled via **targeted CQL search** at enrichment time
  (§6), driven by the component's symbols, terms, and tickets.
- **The component lives in a large monorepo.** git ingestion is **scoped by path prefix** (only
  commits touching the component; the pathspec is pushed into `git log`), and PR ingestion is
  **commit-driven** (discover the PRs that contain the component's commits) rather than
  enumerating every PR in the repo.
- **Bot commits (Dependabot/Renovate/CI, ~10%) are excluded** by author and message pattern —
  they carry no requirement signal and would pollute change-coupling.

The unifying principle: **git is a scoped bulk pass (local, cheap, and needed for
change-coupling); Jira and Confluence are lazy, key/term-driven fetchers.** See §6.

### Success criteria (in priority order)

1. **Catch a regression**: the guardrail flags at least one genuine requirement violation in a
   PR that humans would have missed.
2. **Validated by domain experts**: recovered claims meet the per-layer precision gates
   against expert-written ground truth on a golden component — what-layer ≥95%, corroborated
   why-layer ≥80% (§11).
3. **Trust and adoption follow** from 1 and 2: engineers voluntarily consult the spec or its
   MCP endpoint.
4. **Faster onboarding** is a byproduct, not a target.

### Lessons from the market research that constrain this design

- Code is the ground truth; git history, Jira, PRs, and wikis are supportive artifacts. The
  behavior-and-structure layer (the *what*) is fully code-derivable; AIRE'26's "pure LLM
  requirements-from-code is not viable" applies only to the intent-and-rationale layer (the
  *why*), which must be corroborated by artifacts and human review rather than generated from
  code alone. The system degrades gracefully to code-only (§10.1).
- Auto-generated knowledge without provenance, confidence, and a curation loop loses trust
  (DeepWiki backlash) and doesn't sustain a business (Mutable.ai, CodeSee).
- Per-requirement NLP lint without code grounding doesn't sell (IBM RQA discontinued).
- Passive machine-readable files fail; active queryable protocols win (llms.txt vs MCP).
- Over-flagging kills adoption; precision beats recall for anything that interrupts engineers.

## 3. Core concept

**The claim is the atomic unit of recovered knowledge.** A claim is one testable statement
about expected behavior or structure, typed (state transition, timing budget, threshold,
conditional rule, interaction sequence, invariant), with citations into the evidence that
supports it. Specs, features, and components are aggregations of claims. Decisions (retro-ADRs)
are first-class siblings of claims with the same evidence discipline.

**Two layers: what vs why.** Every claim sits in one of two layers, and the distinction
governs where its truth comes from. *What-layer* claims — behavior, structure, and the values
of constraints (state transitions, timing budgets, thresholds, invariants) — are grounded in
code, which is their sole authority; artifacts are optional corroboration. *Why-layer* claims —
requirement intent and decision rationale — cannot be settled by code: code (e.g. a 50 ms
budget) yields a hypothesis of intent that only artifacts (e.g. the ticket saying "before
winding damage") and human review can confirm. Code is the primary source throughout; git
history, Jira, PRs, and wikis are supportive.

**Review is adjudication, not reading.** A synthesis agent and an independent adversarial
verifier examine every claim. Agreement with strong evidence → auto-verified, never shown to a
human. Disagreement or low confidence on high-impact claims → a triage queue of claim cards,
sorted by impact × uncertainty. Experts arbitrate pre-argued positions in seconds, not read
documents.

**One knowledge store, two surfaces.** The same claim set drives a coverage-first human UI
(treemap → feature → claim cards → evidence) and an MCP server for AI agents, with
verification status and confidence visible on both.

## 4. Architecture

```
┌─ Connectors (plugins) ─────────────────────────────────────┐
│ scoped bulk:   git (path-scoped, bot-filtered) · PRs       │
│ lazy/targeted: Jira (key-driven) · Confluence (CQL search) │
└──────────────┬─────────────────────────────────────────────┘
               ▼
┌─ Evidence Lake (deterministic, no LLM) ────────────────────┐
│ code graph (clang + tree-sitter) · commit/PR/ticket index  │
│ change-coupling stats · issue↔commit candidate links       │
└──────────────┬─────────────────────────────────────────────┘
               ▼
┌─ Recovery Pipeline (Anthropic Agent SDK, cost-tiered) ─────┐
│ link recovery → clustering → synthesis → verification      │
└──────────────┬─────────────────────────────────────────────┘
               ▼
┌─ Knowledge Store ──────────────────────────────────────────┐
│ claims + ADRs as YAML/markdown in a git repo (source of    │
│ truth) · derived index: graph (SQLite) + embeddings        │
└──────┬──────────────┬──────────────┬───────────────────────┘
       ▼              ▼              ▼
   Review/Browse   MCP server     CI guardrail
   web UI          (for agents)   (phase 2)
```

### 4.1 Subsystem responsibilities and interfaces

**Connectors** normalize external sources into evidence records
(`{kind, ref, timestamp, author, text, links}`). They come in two modes. **Scoped-bulk**
connectors ingest everything within the component boundary up front: git (scoped by path prefix,
bot commits excluded) and PRs (discovered from the component's commits, not the whole repo).
**Lazy/targeted** connectors fetch only what a specific claim needs, at enrichment time: Jira
(fetched by the ticket keys found in the component's commits/PRs/branches) and Confluence (CQL
search over the component's symbols/terms/tickets). Fetched artifacts are persisted into the
lake with commit-pinned refs, so evidence stays reproducible.

**Evidence Lake** is a local database (SQLite) of deterministic facts: symbols and their
relations (from clang on `compile_commands.json`, tree-sitter as fallback for unparseable
code), file/commit/ticket/PR records, co-change coupling statistics, and *candidate*
issue↔commit links from heuristics (ticket keys in messages, timestamps, authorship). No LLM
touches this layer; it is cheap to rebuild and is the substrate every later stage cites into.

**Recovery Pipeline** (section 6) turns evidence into claims and decisions.

**Knowledge Store**: claims and ADRs live as YAML/markdown files in a dedicated knowledge git
repo — versioned, diffable, portable, PR-reviewable. A derived index (graph tables in SQLite +
embeddings) is rebuilt from the files at any time; the files are the single source of truth.

**Surfaces**: review/browse web UI (section 7), MCP server (section 8), CI guardrail
(section 9).

### 4.2 Plugin seams

1. **Connectors** — artifact sources (Jira vs Linear vs Azure DevOps, etc.).
2. **Language analyzers** — produce the code-graph part of the evidence lake. C/C++ first.
3. **Claim-type packs** — each pack defines: extraction guidance for the synthesis stage, a
   visual grammar for the UI (how this claim type renders), an optional test compiler
   (claim → executable check), and a verification strategy. MVP packs: state transition,
   timing budget, threshold/limit, conditional rule, interaction sequence, invariant.

Domain-specific capabilities (e.g., ISO 26262 trace exports) are future packs/connectors; the
core never contains domain knowledge.

## 5. Artifact schema

Claim (YAML file in the knowledge repo):

```yaml
id: motor_ctrl/thermal/CLM-0042
type: state_transition              # from a claim-type pack
statement: "When an OVERTEMP fault is raised in RUNNING, the controller
            shall enter SAFE_STOP within 50 ms."      # EARS-leaning phrasing
layer: what          # what (code is the sole authority) | why (artifacts corroborate)
status: contested    # recovered → machine_verified → expert_accepted | rejected
confidence: 0.62
evidence:
  - {kind: code,       role: primary,       ref: "fault_handler.c:214@a3f9c21", excerpt: "..."}
  - {kind: ticket,     role: corroborating, ref: "EMB-1042",                    excerpt: "..."}
  - {kind: pr_comment, role: corroborating, ref: "PR#482",                      excerpt: "..."}
counter_evidence:
  - {kind: code, ref: "fault_handler.c:198@a3f9c21", note: "100 ms debounce"}
links:
  symbols: [enter_state, fault_handler]
  feature: thermal-protection
  decisions: [ADR-0007]
  supersedes: null
test_ref: tests/claims/test_clm_0042.c   # present iff the claim is executable
history: []                              # status changes with who/when/why
```

Decisions are MADR-compatible markdown (decision, context, considered options, rationale)
with the same frontmatter fields (status, confidence, evidence). Using EARS phrasing for
claims and MADR for decisions deliberately rides the spec-driven-development wave — both are
standards current LLMs parse well.

Schema rules:

- Every claim declares a `layer`: `what` (behavior/structure/constraint values — code is the
  sole authority) or `why` (intent/rationale — code is a hypothesis, artifacts corroborate).
- Evidence entries carry a `role`: `primary` or `corroborating`. A what-claim requires primary
  code evidence; its artifacts are corroborating. A why-claim's rationale rests on corroborating
  artifacts; with none, it keeps its code hypothesis as primary evidence and is retained as
  `code-inferred` at a reduced confidence tier (§10.1).
- Evidence refs pin a commit hash; staleness is mechanically detectable.
- A claim with no valid evidence cannot exist (enforced at pipeline stage 3).
- `status` transitions are append-only history; expert overrides record the reviewer.
- The graph (claim → symbol → file → ticket → decision → feature → component) is derived from
  these files — this is what keeps the store git-native without giving up graph queries.

## 6. Recovery pipeline

Runs are **per component** (the monorepo's natural unit), cached, and resumable from the
evidence lake.

**Two passes, code-first.** Ingestion mirrors the two layers. **Pass 1 (code-only)** builds the
code graph and synthesizes the what-layer — behavior, structure, constraint values — from code
alone; it needs no artifacts and is what §10.1 guarantees. **Pass 2 (progressive enrichment)**
resolves the why-layer per feature/claim: for the specific files and lines behind a claim, run
scoped `git log -L` / `--follow` to find the commits that shaped them, follow those to their PRs
and ticket keys, fetch exactly those tickets/PRs (and CQL-search Confluence for the relevant
terms) live, and persist the results into the lake. Change-coupling is the one artifact-side
signal that stays a scoped bulk pass — co-change needs the full (scoped, bot-filtered) commit
history and cannot be reconstructed lazily; but it is git-only and cheap.

The stages below run within each pass: Pass 1 uses stages 0–4 on code evidence; Pass 2 re-enters
stages 0–3 with the freshly-pulled artifacts as corroborating evidence for why-claims.

| Stage | Model tier | Job |
|---|---|---|
| 0 Extract | none | clang/tree-sitter parse; scoped + bot-filtered git log; coupling stats; commit-driven PR discovery; (Pass 2) key-driven Jira + CQL Confluence fetch |
| 1 Link + cluster | cheap (Haiku) | recover missing ticket↔commit links; group evidence into feature areas; detect candidate claim types |
| 2 Synthesize | expensive (Sonnet/Opus) | per feature area: draft claims and retro-ADRs; every claim must cite evidence from the lake |
| 3 Verify | mid (Sonnet) | independent adversarial pass per claim: attempt refutation against the code; re-fetch citations and check they say what is claimed (drops hallucinated refs); compile executable checks where the claim-type pack supports it |
| 4 Review | humans | exception queue only (section 7) |

Verification outcomes: verifier agrees + strong evidence → `machine_verified`; executable
check passes → `machine_verified` (strongest, skips humans permanently); verifier disagrees →
`contested`, queued with the counter-evidence attached; citations invalid → claim dropped.

Verification is layer-specific. What-layer claims are refuted against the code (and, where the
pack supports it, an executable check). Why-layer claims cannot be refuted by code — they are
verified by artifact corroboration and, failing that, routed to human review; a why-claim with
only its code hypothesis is marked `code-inferred` and never auto-verified.

**Incremental operation** after bootstrap: a merged diff maps to touched claims via symbol and
file links plus change coupling; a cheap triage decides which claims need re-verification;
only semantic contradictions surface to humans, with the diff attached. This replaces
RM-style "suspect links" (which flag typos and safety-limit changes identically) with
semantic suspicion.

**Cost controls**: per-component scoping, evidence pruning before synthesis (only the
cluster's evidence enters context), prompt caching, cheap-first cascade. The per-stage model
choice is configuration.

## 7. Review and visualization

The review UI and the browsing UI are the same surface; accepted claims don't move into a
document, they stay as cards.

- **Entry**: component treemap colored by verification state (verified / contested /
  unrecovered). Coverage metrics lead; graph exploration is drill-down only. No full-graph
  hairball renders anywhere in the product.
- **Drill**: component → feature clusters → claim cards → evidence.
- **Claim card**: statement; the claim rendered in its type's visual grammar (state machine
  with the claimed transition highlighted, decision table, parameter table, sequence diagram);
  evidence rows with pre-located excerpts; verifier's counter-evidence when contested;
  accept / edit / reject with single-keystroke bindings.
- **Queue**: sorted by impact × uncertainty; shows estimated review time; a reviewer's daily
  load is minutes.
- **Visual grammar is per claim type** (from the pack), because verifying a highlighted
  transition on a diagram takes seconds while parsing the same fact from prose takes a
  paragraph of concentration. Prose rendering is the fallback for untyped claims.
- Doc-drift findings (wiki says X, code does Y, changed in PR #N) appear in the same queue —
  drift is a product output, not an error.

## 8. Machine surface (MCP)

An MCP server over the knowledge store with typed tools:

- `get_component_spec(component)` — claims + decisions, filtered by status.
- `claims_for_symbol(symbol|file)` — what behavior depends on this code.
- `why_decision(topic|symbol)` — relevant ADRs with evidence.
- `check_change_against_claims(diff)` — guardrail logic exposed interactively.
- `impact_of_requirement(text)` — phase 3+: affected features/claims/symbols for a proposed
  requirement.

Every response carries status and confidence, so a consuming agent can be instructed e.g.
"only rely on expert-accepted claims for safety decisions." Engineers' existing AI tools
(Claude Code, Cursor) consume this without new UI — this is where the onboarding byproduct
comes from. Retrieval is hybrid: graph traversal from anchors (symbols, tickets) plus
embedding search over statements and evidence.

## 9. Regression guardrail (phase 2)

CI job on every PR:

1. Map diff → affected claims (symbol/file links + change coupling).
2. Cheap-model triage: could this diff plausibly violate any affected claim?
3. Expensive-model check only on suspicion; executable claims just run as tests.
4. On violation: PR comment citing the claim, its evidence, and its acceptance history.

Precision rules (non-negotiable, from the research graveyard):

- Only `machine_verified` and `expert_accepted` claims may flag; `contested` stays silent.
- Launch in report-only mode; gating only after measured false-positive rate justifies it.
- Every flag links the full provenance chain so a developer can dispute it in one click;
  disputes feed back as claim reviews.

## 10. Trust and failure handling

- No claim without valid, re-fetched citations (phantom-link defense).
- Synthesis and verification are separate agents with separate prompts; verification is
  adversarial by instruction.
- Confidence is computed per layer, never from model self-report. A what-claim earns high
  confidence from primary code evidence plus verification — an executable check reaches
  `machine_verified` with no artifact required; artifacts are corroborating, not gating. A
  why-claim carries a base `code-inferred` confidence from its code hypothesis, which agreeing
  artifacts (ticket/PR/wiki) raise and which the verifier and human review adjudicate.
- Component runs resume from the evidence lake; nothing regenerates wholesale.
- Contradictions (wiki vs code, claim vs claim) are surfaced findings, not pipeline errors.
- All artifacts and status changes are in git; every automated decision is reconstructable.

### 10.1 Degradation to code-only

Artifacts are supportive, never required. When none are available — or they are sparse or so
drifted they are discarded — the pipeline still produces the complete **what-layer** at full
confidence: behavior, structure, and constraint values, verified by executable checks, with the
MCP surface and guardrail fully operational on those claims. **Why-layer** claims are still
emitted, tagged `code-inferred, unconfirmed` at a reduced confidence tier. What is lost is
rationale certainty and requirement↔ticket traceability — never the behavioral backbone. Every
connector is therefore optional; the code graph and the git tree are the only hard inputs. This
is a designed, tested mode, not a failure path.

## 11. Validation plan

**Golden component protocol** (before any rollout):

1. Choose one medium component with an available domain expert.
2. Expert writes ~30 ground-truth claims first, each labeled `what` or `why` (covering both
   layers, with the rationale recorded for why-claims); sealed until after recovery runs.
3. Run recovery; measure precision/recall **per layer** against the ground truth. Gates:
   what-layer precision ≥95% (code is authoritative — a wrong what-claim is a misread of the
   code, the worst trust failure); corroborated (artifact-backed) why-layer precision ≥80%.
   `code-inferred` why-claims are reported, not gated — their hit-rate calibrates that
   confidence tier and they ship explicitly labeled unconfirmed. Recall is reported per layer
   but not gated — missing claims are a growth path, wrong claims are a trust killer. A single
   blended precision number is banned: it would hide a weak why-layer behind a strong
   what-layer.
4. Also measure: issue↔commit link-recovery quality on a hand-labeled sample (a why-layer /
   traceability metric); expert review time per claim; fraction auto-verified.

If the component is almost all behavioral, the why sample may be small and its precision noisy
— report the sample size alongside the number.

**Guardrail rehearsal**: re-introduce 2–3 known historical bugs on a branch; the guardrail
must catch them (with the relevant claims recovered and verified) before it goes live on real
PRs.

**Pipeline evals**: golden-component fixtures become regression evals for the pipeline itself
(prompt/model changes are tested against them).

## 12. Phasing

- **P0 — Evidence lake** (~weeks): connectors + code graph + link recovery on the golden
  component. Measurable output: link-recovery quality. No synthesis yet.
- **P1 — Recovery + review**: full pipeline + review UI on the golden component. Gate:
  per-layer expert validation — what-layer ≥95%, corroborated why-layer ≥80% (success
  criterion 2).
- **P2 — Guardrail + MCP**: report-only guardrail on the component's PRs; MCP server live.
  Gate: rehearsed regression catch, then a live one (success criterion 1).
- **P3 — Rollout**: component-by-component across the monorepo; onboarding value compounds.
- **P4 — Impact/effort**: `impact_of_requirement` + effort estimation grounded in the
  claim↔code links plus historical ticket actuals. Built last because it depends on
  everything before it.

## 13. Risks

| Risk | Mitigation |
|---|---|
| Synthesis precision below the per-layer gates | P1 gate fails fast; for the what-layer, executable checks and code refutation are the levers; for the why-layer, artifact corroboration and human review, else ship as `code-inferred`; scope claim types down before scoping quality down |
| C/C++ parse coverage on embedded code (compiler extensions, generated code) | clang with the project's own `compile_commands.json`; tree-sitter fallback; unparsed regions are recorded as coverage gaps, not silently skipped |
| Cost blowup on 3M LOC | per-component runs, cheap-first cascade, caching; P0/P1 produce real per-component cost numbers before any rollout decision |
| Expert time is the true bottleneck | exception-only queue, minutes/day budget, executable claims bypass humans |
| Guardrail false positives destroy trust | report-only launch, verified-claims-only flagging, one-click dispute |
| Incumbent adjacency (Unblocked, DeepWiki improving) | the moat is the verified, evidence-cited claim corpus and its guardrail/impact uses — retrieval tools don't accumulate that asset |

## 14. Decisions taken (with rationale)

- **Claims-in-git over graph-DB-first**: portability, diffability, PR-native review, IP
  cleanliness; graph is derivable. Rework cost if wrong: an importer, not a redesign.
- **Anthropic Agent SDK** as the orchestration base per deployment constraint; model tiers
  configurable per stage.
- **EARS + MADR compatibility** to ride the spec-driven-development ecosystem rather than
  invent formats.
- **Precision over recall everywhere user-facing**; recall grows component-by-component.
- **Per-component processing** matches monorepo structure and caps cost/context.
- **Web UI + MCP + CI as separate thin surfaces** over one store, so any surface can be
  rebuilt without touching the knowledge.
