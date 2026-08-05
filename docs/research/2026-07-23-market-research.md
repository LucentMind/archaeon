# Market & Research Landscape (July 2026)

Product idea: reverse-engineer requirements + architecture decisions from code and related
artifacts (git history, Jira, PRs, comments, docs), dual-represent them (human visualization +
machine-readable for AI), then detect gaps/conflicts/duplications and assess impact/effort of
new requirements.

Target user (decided): mid-size product teams (20–200 engineers, 5–10 year old codebases).
Wedge priority (decided): 1) living spec recovery, 2) regression guardrail, 3) onboarding
accelerator, 4) new-requirement impact.

---

# Pillar 1: Recovering requirements & architecture decisions from code + artifacts

## Category 1: AI codebase-understanding / auto-documentation

### DeepWiki (Cognition / Devin)
- Auto-generates wiki-style docs for any GitHub repo (Apr 2025). Free for public repos; private repos require paid Devin account (~$2.25/ACU, Team $500/mo).
- Pros: zero-setup, best free demo of the category; hierarchical pages + diagrams; Q&A chat; MCP server.
- Cons: describes *current structure*, not intent; hallucination risk; documented accuracy backlash (Aug 2025: "confusing, misleading, outright wrong"); no maintainer correction loop.
- Requirements/decision recovery: low-moderate. No ticket/PR mining, no requirements set, no decision records with rationale.

### Swimm
- Pivoted from doc-sync to application-understanding for legacy/mainframe (COBOL etc.), "extracts business rules". Deterministic static analysis + LLM; LOC-based pricing, enterprise/SI motion.
- Pros: closest commercial claim to business-rules recovery; deterministic base reduces hallucination; on-prem.
- Cons: mainframe/legacy focus, not modern polyglot codebases; no git/Jira/PR intent mining; business rules ≠ requirements with rationale.

### Mutable.ai — defunct
- Auto Wiki (repo articles with code citations) acquired by Google Dec 2024 and shut down. Signal: pure auto-wiki wedge alone didn't sustain a company; DeepWiki commoditized it for free.

### Sourcegraph (Cody → Amp, Deep Search)
- Code search + precise code graph (SCIP); agentic codebase research. Q&A-session-shaped, not artifact-shaped. Requirements/decision recovery: low.

### Unblocked — closest competitor overall
- Indexes code + GitHub/Jira/Linear/Slack/Confluence; answers questions about "code, decisions, and history". ~$20M Series A. $19–29/user/mo. MCP feed into Cursor/Claude/Copilot.
- Pros: closest existing product to "mine code + Jira + Slack for the why"; permission-aware; cheap per-seat.
- Cons: reactive Q&A/review context, not a synthesized browsable corpus; returns retrieved snippets of old conversations, not reconstructed/validated decisions; no traceability matrix.

### Others
- CodeSee: acquired by GitKraken 2024, sunset. Auto-derived code maps alone weren't a durable business.
- Komment: CI-regenerated "self-healing wiki"; describes code as-is only.
- Driver AI: YC-backed auto-docs, templated outputs; code-only input, no rationale mining.
- Glean (engineering agents): horizontal retrieval across silos; no synthesis of requirements/ADRs.
- Aider-style repo maps: commodity technique (tree-sitter + graph ranking), table stakes.

## Category 2: Legacy modernization / application understanding
- CAST Imaging: deterministic architecture blueprints, ~$10k/app/yr; structure only, no intent/history.
- Moderne/OpenRewrite: Lossless Semantic Tree, estate-wide accurate code facts; built for transformation, not comprehension; best factual substrate idea.
- AWS Transform (mainframe): most explicit shipping "requirements recovery" pipeline (docs → business rules JSON/HTML → decompose → transform); COBOL-only, AWS-migration-tied, no rationale.
- IBM watsonx Code Assistant for Z: similar, IBM Z only.
- vFunction: "architectural observability" via runtime telemetry; recovers de-facto architecture + drift detection; Java/.NET-centric; no intended-architecture/rationale.

## Category 3: Behavioral code analysis
- CodeScene: mines git history — hotspots, change coupling, knowledge maps, PR delivery-risk scores (~€18–27/author/mo). Proves git history contains recoverable architectural/organizational signal. No NL synthesis, no ticket/PR text mining. Nobody has combined CodeScene-style history mining with LLM narrative synthesis commercially.

## Category 4: Academic research
- Architecture recovery (SAR): classic clustering (ACDC, Bunch, ARC) recovers module structure only, poor ground-truth agreement. LLM-era: ArchAgent (arXiv:2601.13007) multiview business-aligned docs; ECSA 2025 — LLMs decent at patterns/styles, unreliable at scale; benchmarks (SAKE) stage of maturity. Rationale out of scope in nearly all.
- Requirements recovery: user stories from code F1≈0.8 but only ≤200 NLOC snippets (arXiv:2509.19587). **Key negative result: AIRE'26 (arXiv:2606.25550) — LLM+RAG pipeline "not yet viable" for reliable requirements generation from code; human-in-the-loop required.** → Product must triangulate code with tickets/PRs + human review, not pure generation.
- Design-rationale mining: Shahbazian ICSA 2018 (decisions via architecture diffs × issues); DRMiner (arXiv:2405.19623) F1≈0.65 extracting rationale from issue logs, beats GPT-4 by ~7pts. Feasible but hard. ADRs in OSS are rare → rationale must be inferred.

## Category 5: Traceability recovery
- Issue↔commit linking: FRLink → EALink (ASE 2023) → LinkAnchor (LLM agent, ACM 2025/26). Only ~42% of issues linked to commits in practice → the evidence graph must itself be recovered; techniques now exist, zero commercial packaging.
- Requirements↔code: TraceLLM (REJ 2026), R2Code, RAG pipelines ~85% on automotive reqs. Known LLM failure modes: naming bias, phantom links → review UI mandatory.

## Pillar 1 gap analysis
1. Nobody synthesizes decisions-with-rationale as first-class artifacts (retroactive ADR generation with evidence citations = unoccupied).
2. Nobody fuses the three evidence classes: static structure (CAST/Moderne) + temporal git signal (CodeScene) + NL artifacts (Unblocked). Research says fusion is where decisions become recoverable.
3. Requirements recovery for modern stacks is open — only mainframe products exist.
4. Docs-vs-reality drift detection for requirements/docs: nobody ("Confluence says X, code does Y, changed in PR #1234").
5. Missing-link reconstruction (issue↔commit) is research-only.
6. Confidence + provenance UX (claim-level citations, review queues) = the moat vs free DeepWiki.
7. Adjacent threat: "context graph" tools capturing agent sessions prospectively (Git AI, Entire); nobody does it retrospectively — complementary.

---

# Pillar 2: Visualization for humans + machine-readable knowledge for AI

## Architecture visualization
- C4/Structurizr: model-first, multiple views, semantic-zoom insight; diagram drift is #1 complaint; manual authoring; machine-readable DSL but no LLM/agent surface.
- IcePanel: best C4 UX without DSL; manual modeling; no agent story.
- Ilograph: YAML model → perspectives; strongest "one model, many views"; niche; no agent surface.
- D2/Terrastruct: best text-to-diagram language; rendering language, not a model.
- DeepWiki: only mainstream product doing both human wiki + machine MCP — both shallowly, with trust problems, no requirements/decision semantics.

## ADR tooling
- adr-tools (maintenance mode), MADR (most machine-friendly format), log4brains (best OSS publishing, slowed since ~2023), Backstage ADR plugin (colocates ADRs with service catalog).
- No venture-scale ADR platform exists — decision knowledge is the most under-tooled knowledge type.

## Requirements management visualization
- DOORS/DOORS Next: matrix-centric, hated UX, lock-in, expensive.
- Jama Connect: Live Trace Explorer + Trace Score (0–100% expected relationships present) — best pattern for making thousands of requirements digestible: aggregate coverage metrics, not hairballs. Shallow code linkage.
- Polarion: powerful, overwhelming. ReqView: exports traceability graph to Neo4j (admission native viz can't do graph analysis).
- Trace.Space ($4M seed 2025): AI-native RM, living connected graph, auto-detects broken links/gaps; hardware/automotive focus, doesn't recover from code.
- Why all painful: O(n²) matrices, slow at 10k+ items, manual links decay ("suspect link" management is a full-time job), requirements disconnected from code.

## Knowledge graphs for code
- Microsoft GraphRAG: community-detection hierarchy + summaries; answers global questions. Cons: costly indexing, **no incremental updates** (fatal for living codebases), high query latency.
- SCIP / Meta Glean / CodeQL: precise, incremental, scalable ground-truth substrate; symbol-level only, zero human viz.
- Academic: RepoGraph (+32.8% relative on SWE-bench), CodexGraph, LocAgent — replicated evidence graph-structured context measurably improves agents.
- potpie.ai: OSS Neo4j code KG + agents; code-structure only, no human viz.
- jQAssistant+Neo4j: architecture-constraints-as-Cypher in CI; Java-centric, hairball viz.

## Machine-readable docs for agents
- llms.txt: effectively failed (~10% adoption, no provider commitment, Google rejected). Lesson: passive files lose to active protocols.
- CLAUDE.md/AGENTS.md: conventions prompt, not a knowledge base. Negative result: LLM-generated AGENTS.md *reduced* task success in 5/8 settings.
- Context7, DeepWiki MCP: prove "docs behind an MCP tool" is the winning distribution channel.
- Cursor embeddings: hybrid semantic+structural beats either alone (+12.5% avg accuracy); Merkle-tree incremental sync pattern.

## Large-graph visualization at scale
- Fails: raw node-link hairballs beyond low thousands of nodes.
- Works: hierarchical aggregation/supernodes, semantic zoom (different representations per level), WebGL LOD rendering, edge bundling, query-and-drill (never "show everything"), adjacency matrices for dense subgraphs, coverage dashboards for aggregate questions.
- Key insight: GraphRAG's community-summary tree and a semantic-zoom cluster tree are isomorphic — ONE hierarchy can power both human drill-down and multi-level agent retrieval. Nobody does this.

## Pillar 2 gap analysis
1. Nobody serves both audiences from one substrate (clean two-cluster split in the market).
2. The "why" layer absent everywhere: no typed graph requirement → decision → architecture element → code symbol.
3. Shared hierarchy for zoom + retrieval unexploited.
4. Staleness is the universal killer; incremental re-verification on diff is the answer no one ships.
5. Trust/curation loop missing: per-assertion provenance + confidence + human accept/override state, exposed to both audiences.
6. Lead with coverage metrics (Jama Trace Score pattern), graph exploration as drill-down.
7. Distribution channel is MCP with typed query tools, not files.
8. Format vacuum: emit EARS (requirements) / MADR (decisions) compatible artifacts to ride the spec-driven-development wave (Kiro, Spec Kit) — SDD is forward-only, recovery is the complement.

---

# Pillar 3: Gap/conflict/duplication detection + impact & effort analysis

## Requirements quality / NLP tools
- Jama Connect Advisor: per-requirement INCOSE/EARS lint; no cross-requirement conflict detection; no code awareness. Jama now ships MCP server (feeding requirements TO agents — inverted from our approach).
- QVscribe: quality scores + similarity analysis (near-dup); no semantic conflicts, no code.
- IBM RQA (Watson): discontinued — per-requirement lint alone didn't sell even inside the dominant RM platform.
- ScopeMaster: most interesting incumbent — cross-references every story vs all others (duplicates, inconsistencies, omissions via CRUD matrix), certified automated sizing (COSMIC+IFPUG, ~15% of expert count). But input is written stories; "omission" = missing vs the story set, not vs the actual system. **The product to out-flank by grounding in code.**
- Valispace ValiAssistant: LLM inconsistency reports; hardware focus, prose output.
- Research: PassionNet (~13% over SOTA, ensembles win), S3CDA (similarity → pairwise classification). ICSME 2025: LLM ambiguity detection without domain grounding = high recall / low precision (over-flagging killed prior tools). Public datasets small and text-only → data moat opportunity.

## Traceability & impact in RM tools
- Jama/DOORS/Polarion all: hand-made typed links; upstream change → "suspect" flag; impact = graph traversal. Limitations: links decay, suspect flags carry zero semantics (typo = safety-limit change), trace stops at requirement/test boundary, no conflict detection at all.

## Code-side change impact
- Test Impact Analysis: Datadog TIA (coverage-intersect), CloudBees Smart Tests ex-Launchable (ML predictive selection), Sealights (+ Test Gap Analysis = changed code never tested — only code-side gap detector). All commit-granular, requirements-blind.
- Moderne: org-wide semantic query engine; you bring the question.
- CodeScene: change coupling + delta risk scores; empirical, language-agnostic; file-level, no requirements linkage.
- Static reachability: Understand, Lattix DSM, CodeQL; over/under-approximate.

## Regression risk prediction
- Meta DRS/RADAR (arXiv:2605.30208): logistic regression, 12 predictors; auto-approved 331k+ diffs, 50× fewer incidents than human-reviewed baseline. Google: predictive test transition, 65% faster breakage detection.
- JIT defect prediction: 15 yrs of research; poor cross-project generalization, noisy labels, process-metric features.
- All score *existing diffs*. Predicting risk for a *proposed requirement* (pre-code) is unoccupied. Closest proxy: predicted touch set × historical file risk.

## AI effort estimation
- Story-point research: deep models barely beat naive baselines; RAG over historical similar issues beats plain prompting (arXiv:2604.03443). All estimate from issue text only.
- **No shipping product estimates effort from requirement + actual codebase analysis** (verified by explicit search).

## Ticket dedup (Jira/Linear)
- Atlassian Intelligence/Rovo: similarity-based dup flagging, shallow. Linear Triage Intelligence: LLM dup/related detection at triage, good. Both: no notion of *conflicting* tickets; no code grounding (ticket duplicating shipped behavior invisible).

## Pillar 3 gap analysis
1. Conflict detection grounded in implementation ("new requirement contradicts what code actually does") — no tool or paper. Text-only conflict detection has low precision precisely for lack of grounding; the recovered KB is the missing grounding.
2. Gap detection against reality: "requirement with no implementing code" + "code behavior with no owning requirement" — novel.
3. Requirement-level impact analysis (map not-yet-implemented requirement → touched features/modules/risk) — tractable per 2025-26 TLR research, no product ships it.
4. Effort/risk fusion: predicted touch set × hotspot/coupling risk × RAG over similar past tickets with actuals — all ingredients individually validated, combination unoffered.
5. Auto-recovered, continuously re-derived suspect links with semantic flags attack the RM incumbents' core weakness.
6. Data moat: requirement↔code↔outcome triples generated by the recovery pipeline.
Risks: precision/over-flagging is the graveyard (RQA died); Atlassian/Linear one model-upgrade from better dedup (but not conflicts/code grounding).

---

# Cross-cutting synthesis

The market splits into non-overlapping clusters, each holding one piece:
| Piece | Champion | Missing |
|---|---|---|
| Evidence retrieval (code+Jira+Slack) | Unblocked | Synthesis into artifacts |
| Auto wiki + MCP | DeepWiki | Accuracy, curation, requirements semantics |
| Git behavioral signal | CodeScene | NL synthesis |
| Deterministic code facts | Moderne/SCIP/Glean | Requirements layer, human view |
| Requirements graph + AI maintenance | Trace.Space | Code side |
| Cross-requirement conflict/gap/sizing | ScopeMaster | Code grounding |
| Commit risk scoring | Meta DRS (research) | Requirement-level, pre-code |

The whitespace: a verified, incrementally-maintained, typed knowledge graph linking
requirement ↔ decision ↔ architecture element ↔ code symbol, recovered by fusing static
structure + git history + NL artifacts, with per-assertion provenance/confidence/curation;
one shared cluster hierarchy powering (a) a coverage-first semantic-zoom human UI and
(b) an MCP-served hybrid graph+embedding retrieval surface for agents; conflict/gap/impact
analysis grounded in that graph.

Hard-won lessons from the corpses:
- Mutable.ai, CodeSee: auto-generated maps/wikis alone aren't a durable business.
- DeepWiki backlash: auto-recovered knowledge without verification/curation loses trust.
- IBM RQA: per-requirement NLP lint alone doesn't sell.
- llms.txt: passive machine-readable files fail; active queryable protocols (MCP) win.
- AIRE'26: pure LLM requirements-from-code is not viable; triangulation + human-in-loop mandatory.
