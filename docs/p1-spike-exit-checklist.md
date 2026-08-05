# P1 What-Layer Spike — Exit Checklist (Runbook)

This validates the product's **core bet**: can Archaeon extract *correct* what-layer claims —
behavior, structure, and constraint values — directly from C/C++ code? The what-layer stands on
code alone, so this needs no artifact links and no P0 exit numbers; only that `scan` has run.

- **Gate:** what-layer claim **precision ≥ 0.95**. Code is authoritative for the what-layer, so a
  wrong what-claim is a *misread of the code* — the worst trust failure. The bar is high on
  purpose.
- Recall is reported (informally, against a small sealed set) but **not** gated — missing claims
  are a growth path; wrong claims are the killer.

Related: [design spec §5/§6/§11](superpowers/specs/2026-07-23-archaeon-design.md) ·
[P0 exit checklist](p0-exit-checklist.md)

---

## Prerequisites

- P0 evidence lake built for the golden component, and **`scan` has run** (the `symbols` table
  is populated — `synthesize` reads it to find the feature's files).
- Claude CLI auth set up (`claude login`, or `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`)
  — `synthesize` uses the Claude Agent SDK, same as `link-llm`.
- A stronger synthesis model is recommended: set `[llm] expensive_model` in `archaeon.toml`
  (e.g. a Sonnet/Opus-class model); it falls back to `cheap_model` if unset.

## Step 0 — Pick a feature area and an expert

Choose **one small, coherent slice** — a single file or a small subdirectory — that actually
contains what-layer substance: a state machine, a fault/thermal path, a timing-critical routine,
a set of thresholds. Avoid pure glue/config code (nothing to extract). Have the domain expert for
that slice available for Step 3.

Keep it small: the spike inlines the source into the prompt, so a feature area of a few files is
right. Very large slices get truncated at a character cap.

## Step 1 — (Optional but recommended) seal a small ground truth

For a recall read, have the expert write ~10 what-layer claims for the slice **first**, sealed
until after Step 2. This is the golden-component protocol at feature scale. If you skip it, you
still get the precision gate from Step 4; you just won't have a recall number.

## Step 2 — Synthesize and verify

```bash
uv run archaeon synthesize --feature src/motor_ctrl/thermal/ --out claims
```

`--feature` is a path prefix (matched against the parsed file paths in `symbols`). This
synthesizes typed what-layer claims from the code, then runs an independent **adversarial
verifier** on each. It prints, e.g., `claims: 14  machine_verified: 11  contested: 3` and writes
one YAML file per claim to `claims/`, plus `claims/run_cost.json` — the run's actual LLM cost
broken down by stage and model.

Cost note: it makes 1 synthesis call plus 1 verification call per claim, so ~N+1 model calls for
N claims — small for one feature area. You don't have to estimate the spend: the printed cost
block and `claims/run_cost.json` carry the SDK's own figure. Check `failed_calls` there too — a
non-zero count means some calls errored (turn exhaustion or overload) and were silently degraded
to `contested`, so the precision read below is based on fewer real verifications than it looks.
`failed_calls` is a lower bound, not an exact count — the SDK can mark a call `is_error` while
still landing on a `success` subtype (e.g. a recovered HTTP failure), and there's no reliable way
to tell that apart from a hard failure here, so such calls are still counted as successful.

## Step 3 — Review the claims

Each `claims/CLM-*.yaml` has the statement, its `type`, `status`
(`machine_verified` / `contested`), `confidence`, and code evidence (`file:line` + excerpt).

For each claim, the expert judges one thing: **does the cited code actually establish this claim,
exactly as stated?** Open the cited `file:line`, read it, decide. This is the precision judgment —
it does not require the sealed set.

- A `contested` claim should have a real problem in its `counter_evidence`; sanity-check that the
  verifier contested for a good reason (and didn't wrongly contest a correct claim).
- A `machine_verified` claim should hold up to the expert's own read.

## Step 4 — Label precision

Scaffold a labels CSV from the claim ids, then fill in `yes`/`no`:

```bash
echo "claim_id,correct" > claim_labels.csv
for f in claims/*.yaml; do echo "$(basename "$f" .yaml)," >> claim_labels.csv; done
```

Then edit `claim_labels.csv` so each row's `correct` is `yes` (the code establishes the claim as
stated) or `no` (wrong, overstated, or misattributed).

## Step 5 — Run the eval

```bash
uv run archaeon claims-eval --claims claims --labels claim_labels.csv
```

It prints two numbers per layer, each against its own gate:

- **Pre-verification** — precision over *every* labeled claim, including ones the adversarial
  verifier itself contested. Gate: **≥ 0.85**.
- **Post-verification** — precision over only the claims that reached `machine_verified`. Gate:
  **≥ 0.95**. This is the number that maps to the design's actual trust boundary: only
  `machine_verified`/`expert_accepted` claims are ever shown to a user or allowed to flag
  (contested claims stay silent), so this is what a user or the guardrail would actually see.

A synthesis batch can have real defects that verification reliably catches — that shows up as a
lower pre-verification number with a clean post-verification one, and is a *good* sign for the
pipeline (the trust boundary is holding), not a failure to iterate on. Report both; don't collapse
them into one number.

## Step 6 — Read the numbers and decide

- **Both gates clear (pre ≥ 0.85, post ≥ 0.95)** → the core bet holds for the what-layer.
  Green-light hardening P1 (clustering + retrieval so it scales past one small feature,
  commit-pinned evidence, then the why-layer and review UI).
- **Post-verification clears but pre-verification doesn't** → the trust boundary is holding, but
  synthesis itself is noisier than ideal; still green-light, but look at *why* pre missed (Step 7)
  as input to tightening synthesis before scaling volume.
- **Post-verification misses the gate** → do not scale yet regardless of the pre number — this
  means claims are reaching `machine_verified` (i.e., surfacing as trusted) incorrectly. Go to
  Step 7.

Sample-size caveat: one small feature yields only ~10–20 claims, so each wrong claim swings the
percentage hard. For a *stable* read, accumulate ≥20 what-claims — run the spike over 2–3 feature
areas and pool the labels. Report the claim count alongside the precision.

## Step 7 — If below the gate, diagnose before scaling

Look at *which* claims were labeled `no` and *why*:

- **Wrong on a specific type** (e.g. timing budgets right, state transitions wrong) → the
  claim-type pack / synthesis prompt for that type needs work; scope claim types down before
  scoping quality down.
- **Overstated / hallucinated citations** → the verifier should have contested these; if it
  didn't, tighten the verification prompt (it is meant to default to refuted when the citation
  doesn't clearly support the statement).
- **Truncated context** → the feature area was too big and got cut at the char cap; pick a smaller
  slice, or wait for the retrieval-based P1 (this spike inlines source deliberately).

Fix, re-run Step 2, re-label only the changed claims, re-eval.

## Step 8 — Record it

Write into a dated note under `docs/research/`: what-layer precision and claim count, the
per-type breakdown of any failures, the machine_verified vs contested split, the model used, and
the cost — copy the exact figures from `claims/run_cost.json` (`total_usd`, `calls`,
`failed_calls`, and the `by_stage` split) rather than estimating; note its `billed` field, which
is `false` under subscription auth and `null` if the billing route could not be determined. If
you did Step 1, add the informal recall (sealed claims the synthesis found). This
is the P1-spike deliverable and the baseline the hardened P1 is compared against.

---

## Decision

| Outcome | Meaning | Next |
|---|---|---|
| what-layer precision ≥ 0.95 | code→claims extraction is trustworthy | harden P1: retrieval/clustering, commit-pinning, why-layer, review UI |
| precision < 0.95 | claims can't yet be trusted | iterate synthesis/verification prompts and claim-type packs, re-measure |

## Known limitations of the spike (won't change this run's outcome, but be aware)

- **Source is inlined into the prompt** (no tool-use file reading), so keep the feature area small;
  large slices truncate at a char cap. Real P1 will retrieve/cluster instead.
- **Evidence refs are `file:line`, not commit-pinned** — staleness detection is the enrichment
  pass (Pass 2), not tested here.
- **What-layer only** — no why-layer claims, no clustering, no review UI. This gate is purely the
  what-layer synthesis+verify core.
- **Precision comes from expert labels**; recall against a sealed set is manual (Step 1).
