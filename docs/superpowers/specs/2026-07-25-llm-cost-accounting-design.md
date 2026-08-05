# LLM Cost Accounting (post-run, from the Claude Agent SDK)

**Date:** 2026-07-25
**Status:** design (approved for planning)
**Related:**
scoped 4b re-run *(internal validation run — not in this repo)* ·
[archaeon design](2026-07-23-archaeon-design.md)

## Goal

Report the **actual** cost of the LLM stages of a pipeline run instead of the
current after-the-fact token heuristics. The Claude Agent SDK already returns
the real per-call cost on its terminal `ResultMessage`
(`total_cost_usd`, `usage`, `model_usage`) — the code just discards it today,
reading only `.result`
([`src/archaeon/llm.py:59-61`](../../../src/archaeon/llm.py)). Capture it,
aggregate per command run, print a summary, and drop a machine-readable
`run_cost.json` beside the synthesized claims.

Applies to the two LLM-driven commands: `synthesize` (sonnet synth + verify)
and `cluster` (haiku labels).

## Non-goals

- **No pre-run estimate / `--dry-run` forecast.** This spec is post-run actual
  accounting only. A token-counting forecast is a possible separate follow-up.
- **No price table.** We use the dollar figure the SDK computes; we do not
  multiply tokens by a maintained per-model rate table.
- **No new DB table.** Reporting is a printed summary plus a JSON file in the
  claims output directory. (DB persistence was considered and deferred.)
- No change to how any command authenticates or which model it uses.

## Honesty caveat (must be reflected in output)

This project authenticates through the Claude CLI's own subscription/OAuth
login, not an API key — `AgentClassifier` deliberately strips
`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` so the spawned CLI uses
subscription auth
([`_cli_auth_env`, llm.py:20-26](../../../src/archaeon/llm.py)). Therefore the
SDK's `total_cost_usd` is normally the **API-equivalent** cost — what the run
*would* cost billed to an API key — not an amount actually charged. Every
surface (the printed summary and the JSON) must label it as such, e.g.
`cost [synthesize]: ~ $5.7600 API-equivalent (68 calls; subscription auth -
not billed)`, and the JSON carries `"billed": false` plus a `"note"`.

**That claim must not be made unconditionally.** `_cli_auth_env` strips two
env vars; it cannot see a CLI routed through `CLAUDE_CODE_USE_BEDROCK`,
`CLAUDE_CODE_USE_VERTEX` or `ANTHROPIC_BASE_URL`, nor a CLI logged into a
Console/API-billed account. Printing "not billed" for a run that *was* billed
is the wrong direction of error for a feature whose whole point is honest
labelling. So `cost.py` probes those three vars at report time
(`billing_route_overridden()`, non-empty value only) and, when any is set,
reports `"billed": null` with a note and a printed line saying the billing
route is **unknown** and the cost may be real. `cli_synthesize` computes the
summary once and hands it to both surfaces, so the printed line and the JSON
note are the same probe result rather than two live reads of `os.environ`.

The printed summary is also pure ASCII (`~`, not `≈`): `click.echo` writes it
to a possibly cp1252 stdout, and it happens after claims are saved but before
`run_cost.json` is written, so a `UnicodeEncodeError` there would lose the
cost record of an otherwise successful run.

## Components

### 1. `CostMeter` + extraction helper — new `src/archaeon/cost.py`

A dependency-free accumulator, no SDK import (it only reads duck-typed fields
off whatever message object it is handed), so it is unit-testable in isolation.

**The module itself is the source of truth:**
[`src/archaeon/cost.py`](../../../src/archaeon/cost.py). This spec deliberately
does not reproduce it — an inlined copy of a live module drifts, and did.
What follows is the contract `tests/test_cost.py` pins.

**Subtype classification** (the single source of truth; `llm.py` imports these
rather than re-deriving them, so the two modules cannot disagree):

- `is_success_result(msg)` — `subtype == "success"`, the only terminal that
  carries a usable `.result`.
- `is_error_result(msg)` — `subtype` starts with `"error"`, matched by prefix
  so a future `error_*` variant stays counted.
- `is_terminal_result(msg)` — either of the above; costed either way.

The SDK can also set `is_error` on a `"success"` subtype. There is no reliable
way here to tell a recovered retry from a hard failure that still landed on
`"success"`, so classification keys off subtype alone and counts such a call as
successful — which makes `failed_calls` a **lower bound** on failures, not an
exact count.

**Billing-route probe:** `billing_route_overridden()` returns true when any of
`ROUTE_VARS` (`CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`,
`ANTHROPIC_BASE_URL`) holds a **non-empty** value. An empty value is not a
route.

**Extraction:** `call_cost_from_message(msg, stage, model)` returns a
`CallCost(stage, model, usd, input_tokens, output_tokens, cache_read_tokens,
cache_creation_tokens, num_turns, failed)`. `model` is caller-supplied — the
SDK's `ResultMessage` has no top-level `model` field, only a per-model
`model_usage` mapping. Every field degrades to `0` when absent, so an errored
or partial turn still yields a valid `CallCost` (with `failed=True`) rather
than being dropped. `usage` may arrive as a dict or an attribute-carrying
object; `_usage_get` handles both and returns `0` for unknown keys.

**Accumulation:** `CostMeter` exposes `record(msg, stage, model)`,
`calls: list[CallCost]`, `total_usd`, `failed_calls`, `by_stage()`,
`by_model()`, `summary_dict(command)` and
`format_summary(command, summary=None)`. The two grouping views bucket
`usd`/`calls`/`input_tokens`/`output_tokens`/`cache_read_tokens`;
`by_model` keys on the *requested* model name.

`summary_dict` rounds `total_usd` to 4 decimal places and returns exactly the
keys listed under "`run_cost.json` shape" below. `format_summary` accepts an
already-computed `summary` so a caller writing both surfaces shares one probe
of `billing_route_overridden()` (see Component 3); omitted, it computes its
own. Its output is pure ASCII (`~`, not `≈`) — see the honesty caveat above.

### 2. `AgentClassifier` — thread an optional meter through (`llm.py`)

Two new optional constructor params; defaults preserve today's behavior
exactly:

```python
def __init__(self, model, system_prompt=SYSTEM_PROMPT, max_turns=1,
             meter=None, stage=""):
    ...
    self._meter = meter
    self._stage = stage
```

In `_ask`, record on **every terminal** message — success *and* the SDK's
error subtypes, which still carry `total_cost_usd`/`usage`/`num_turns` and so
spent real quota — while keeping the result read gated on success:

```python
async for message in query(prompt=prompt, options=options):
    if not is_terminal_result(message):
        continue
    if self._meter is not None:
        self._meter.record(message, self._stage, self._model)
    if is_success_result(message) and hasattr(message, "result"):
        result = message.result or ""
return result.strip()
```

The subtype semantics live only in `cost.py` (`is_terminal_result` /
`is_success_result` / `is_error_result`); `llm.py` never re-derives them, so
the classification cannot drift between the two modules.

The `.result` read stays gated to `success`, so `ask` never returns text for
an errored terminal. That is narrower than saying `ask` returns `""` on an
SDK error: in production the real SDK raises after an `is_error` result
instead of letting the generator just end (the CLI process exits non-zero
and `query()` converts that into an exception — see
`claude_agent_sdk/_internal/query.py`), so `ask` propagates that exception
rather than returning `""`. The cost survives the raise because `record`
already ran. `meter=None` ⇒ no recording, no behavior change — so `link_llm`
and any other `AgentClassifier` caller are untouched.

Why this matters: `verify_claims`
([`claims/recover.py`](../../../src/archaeon/claims/recover.py)) catches
`Exception` per claim and degrades the claim to `contested`, so an overloaded
or turn-exhausted call leaves the run exiting 0. Without recording errored
terminals, a long `--all-clusters` run can burn quota that never appears in
any report.

### 3. CLI wiring (`cli.py`)

**`cli_synthesize`** — one meter shared across every LLM call in the run:

```python
from archaeon.cost import CostMeter
meter = CostMeter()
...
for label, cid in targets:
    ...
    claims = synthesize_claims(
        label, bundle,
        AgentClassifier(model, SYNTH_SYSTEM, max_turns=4,
                        meter=meter, stage="synthesize").ask)
    verify_claims(claims, bundle,
                  AgentClassifier(model, VERIFY_SYSTEM, max_turns=4,
                                  meter=meter, stage="verify").ask)
    ...
# after save_claims(...) — one summary, both surfaces
cost_summary = meter.summary_dict("synthesize")
click.echo(meter.format_summary("synthesize", cost_summary))
(Path(out_dir) / "run_cost.json").write_text(
    json.dumps(cost_summary, indent=2), encoding="utf-8")
```

The summary is computed once and passed to both surfaces, so the printed
billing wording and the JSON `billed` value come from a single probe of
`billing_route_overridden()` and cannot disagree.

(`encoding="utf-8"` explicitly, matching `save_claims` — the platform default
encoding must not decide the file's bytes.)

**`cli_cluster`** — pass a meter into the label classifier and echo the
summary. `cluster` has no claims output directory, so it is **print-only**
(no JSON file). The exact construction site is wherever `cli_cluster` builds
the `AgentClassifier` it hands to `cluster_symbols` as `label_fn`; give it
`stage="cluster-label"`, and after `cluster_symbols` returns,
`click.echo(meter.format_summary("cluster"))`.

## `run_cost.json` shape (written by `synthesize`)

```json
{
  "command": "synthesize",
  "generated_at": "2026-07-25T18:22:04.101Z",
  "total_usd": 5.76,
  "calls": 68,
  "failed_calls": 2,
  "billed": false,
  "note": "API-equivalent cost from the Claude Agent SDK; this run used Claude CLI subscription auth and was not billed to an API key.",
  "by_stage": {
    "synthesize": {"usd": 0.11, "calls": 1,  "input_tokens": 41000, "output_tokens": 10276, "cache_read_tokens": 0},
    "verify":     {"usd": 5.65, "calls": 67, "input_tokens": 2763080, "output_tokens": 4110, "cache_read_tokens": 0}
  },
  "by_model": {
    "claude-sonnet-5": {"usd": 5.76, "calls": 68, "input_tokens": 2804080, "output_tokens": 14386, "cache_read_tokens": 0}
  }
}
```

Numbers are illustrative — the real values come from the SDK at runtime.

`failed_calls` counts the calls whose terminal message had an error subtype;
it is `0` on a clean run. It is a **lower bound** on failures, not an exact
count: the SDK can set `is_error` on a `success`-subtype message too (e.g. an
HTTP failure it recovered from), and there is no reliable way here to tell
that apart from a hard failure, so such a call is still classified by
subtype alone and counted as successful. `billed` is `false` under
subscription auth and `null` when a routing env var makes the billing route
unknowable (in which case `note` says so too, and the printed summary
agrees).

## Error handling / edge cases

- **Errored terminal turn** (`error_max_turns` / `error_during_execution`):
  recorded like a success — those messages carry `total_cost_usd`, `usage` and
  `num_turns`, so the quota is real. The call lands in `calls`, the stage/model
  buckets, and in `failed_calls`; `format_summary` adds a warning line when
  `failed_calls > 0` and stays silent otherwise. The `.result` read stays
  gated to `success`, so no errored turn yields text through it — but in
  production the SDK raises after such a result rather than returning
  normally (see the Component 2 note above), and the recorded cost survives
  that raise because `record` runs first.
- **Errored/partial turn with no `total_cost_usd`**: contributes `usd = 0.0`
  but is still counted in `calls` and token totals — the meter reflects the
  attempt rather than dropping it.
- **Non-terminal messages** (init/assistant/etc.): never recorded.
- **`usage` shape** (dict vs attr-object): handled by `_usage_get`; unknown
  keys → 0.
- **Meter absent** (`meter=None`): no recording; identical to current behavior.
- **Empty run** (no calls, e.g. `--feature` prefix with no symbols raised
  before any LLM call): `total_usd = 0.0`, `calls = 0`; summary still prints
  and JSON still writes for `synthesize` (a valid zero record).
- **`run_cost.json` write**: best-effort within the command; the claims
  themselves are already saved before it is written, so a write failure never
  loses claims.

## Testing (TDD — write first)

**`cost.py` (pure, no SDK):**
- `call_cost_from_message(msg, "verify", "claude-sonnet-5")` on a fake object
  with `total_cost_usd`, `usage` (dict form), `num_turns` → all fields
  extracted, `model == "claude-sonnet-5"` (caller-supplied, not read off msg).
- Missing `total_cost_usd` → `usd == 0.0`; still a valid `CallCost`.
- `usage` as an attr-object (not dict) → extracted via `getattr`.
- `CostMeter.record` twice across two stages/models → `total_usd` sums,
  `by_stage` splits, `by_model` splits, `calls` counts.
- `summary_dict` has the stable keys (`command`, `generated_at`, `total_usd`,
  `calls`, `failed_calls`, `billed`, `note`, `by_stage`, `by_model`);
  `format_summary` contains the API-equivalent label, the command name, and
  each stage line.
- Terminal-subtype classification: `success` → terminal, not error;
  `error_max_turns` / `error_during_execution` → terminal *and* error;
  `init` and a message with no `subtype` → neither.
- An errored message is counted with `failed=True`, shows up in
  `failed_calls`, and adds the warning line to `format_summary`; a clean run's
  summary mentions no failures at all.
- Billing route: with none of the three route vars set → `billed is False`
  plus the subscription wording on both surfaces; with any one set (via
  `monkeypatch.setenv`) → `billed is None` (JSON `null`) plus unknown-route
  wording on both surfaces and no "not billed" anywhere; an empty value is not
  a route. `tests/conftest.py` clears the three vars for every test so the
  developer's own shell cannot flip these assertions.
- `format_summary` and `note` are ASCII-encodable.

**`llm.py`:**
- `AgentClassifier(..., meter=None)` — monkeypatch `query` to yield a fake
  success message; `ask` returns the result string and the (absent) meter is
  untouched (backward-compat).
- `AgentClassifier(..., meter=CostMeter(), stage="verify")` — same monkeypatch;
  after `ask`, the meter has one call recorded with `stage == "verify"` and the
  fake's usd/tokens.
- A fake terminal message with `subtype="error_max_turns"` /
  `"error_during_execution"` (cost fields, no `.result`): `ask` returns `""`
  yet the meter has one call with its usd/tokens and `failed is True`. Also
  with `meter=None`, and with an error followed by a success (2 calls
  recorded, result still returned).

**`cli.py`:**
- `synthesize` CLI test with `synthesize_claims`/`verify_claims` stubbed (no
  real LLM): asserts `<out_dir>/run_cost.json` exists and parses with the
  expected top-level keys.
- `synthesize` with `archaeon.llm.query` faked to yield a successful synth
  result then an errored verify terminal: the run still exits 0 with the claim
  `contested`, and `run_cost.json` shows both calls, `failed_calls == 1`, and
  the errored call's dollars under `by_stage.verify`.
- `cluster`'s print-only asymmetry is asserted inside
  `runner.isolated_filesystem()` — `CliRunner` does not sandbox the filesystem,
  so a stray relative write would land in the repo's cwd, and asserting it is
  absent from `tmp_path` would be vacuously true.

**No test may make a real LLM call**: any path that reaches
`AgentClassifier.ask` monkeypatches `archaeon.llm.query` first.

## Rollout order

1. TDD `cost.py` (`CallCost`, `call_cost_from_message`, `CostMeter`); commit.
2. Thread `meter`/`stage` through `AgentClassifier`; test backward-compat +
   recording; commit.
3. Wire `cli_synthesize` (summary + `run_cost.json`) and `cli_cluster`
   (summary only); CLI test; commit.
