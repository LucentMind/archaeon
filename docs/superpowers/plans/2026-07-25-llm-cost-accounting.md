# LLM Cost Accounting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture the real per-call cost the Claude Agent SDK already returns on its terminal `ResultMessage`, aggregate it per command run, print a summary, and write `run_cost.json` beside the synthesized claims.

**Architecture:** A dependency-free `CostMeter` accumulator in a new `src/archeon/cost.py` (no SDK import — it duck-types the fields off whatever message object it is handed, so it unit-tests in isolation). `AgentClassifier` gains two optional constructor params (`meter`, `stage`) and records into the meter for every terminal message `_ask` sees — success and the SDK's error subtypes alike — while still reading `.result` only on success; `meter=None` is the default and is byte-for-byte today's behavior, so `link-llm` and every other caller is untouched. The two LLM-driven commands (`synthesize`, `cluster`) each build one meter, share it across every classifier they construct, and report at the end.

**Tech Stack:** Python ≥3.13, stdlib only (`dataclasses`, `datetime`, `json`), `click` for output, `pytest` for tests, `uv` as the runner.

**Spec:** [`docs/superpowers/specs/2026-07-25-llm-cost-accounting-design.md`](../specs/2026-07-25-llm-cost-accounting-design.md)

## Global Constraints

- **No new dependencies.** Everything here is stdlib + what's already in `pyproject.toml`.
- **`src/archeon/cost.py` must not import `claude_agent_sdk`.** It reads duck-typed attributes only. This is what lets `tests/test_cost.py` run without the SDK installed (unlike `tests/test_llm.py`, which starts with `pytest.importorskip("claude_agent_sdk")`).
- **No new DB table, no price table, no pre-run estimate.** Post-run actual accounting only.
- **Honesty labelling is mandatory on every surface.** This project authenticates through the Claude CLI's subscription/OAuth login — `_cli_auth_env` (`src/archeon/llm.py:20-26`) deliberately strips `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`. The SDK's `total_cost_usd` is therefore normally the **API-equivalent** cost, not an amount actually charged.
  - Default (subscription) route — nothing in `ROUTE_VARS` set:
    - Printed summary first line must contain the exact substrings `API-equivalent` and `not billed`.
    - JSON must carry `"billed": false` and this exact `note` string:
      `"API-equivalent cost from the Claude Agent SDK; this run used Claude CLI subscription auth and was not billed to an API key."`
  - Unknown route — any of `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `ANTHROPIC_BASE_URL` set to a non-empty value (`cost.billing_route_overridden()`): the cost may be real, so the JSON carries `"billed": null` and `cost.UNKNOWN_NOTE`, and the printed line says the billing route is `unknown`. Neither surface may say `not billed`. Both derive from the same probe, so they cannot disagree.
  - The printed summary must be pure ASCII (`~`, not `≈`) — `click.echo` runs after `save_claims` but before `run_cost.json`, so a cp1252 `UnicodeEncodeError` there would lose the cost record.
  - The printed summary must name the `command` it was called with, so a transcript containing both `synthesize` and `cluster` blocks is unambiguous.
- **Stable JSON keys** (tests pin these): `command`, `generated_at`, `total_usd`, `calls`, `failed_calls`, `billed`, `note`, `by_stage`, `by_model`. Per-bucket keys: `usd`, `calls`, `input_tokens`, `output_tokens`, `cache_read_tokens`.
- **Terminal error results are costed.** `ResultMessage` subtypes `error_max_turns` / `error_during_execution` still carry `total_cost_usd`, `usage` and `num_turns`, so they are recorded like a success and counted in `failed_calls` (`format_summary` warns when that is non-zero, and is silent when it is zero). The `.result` read stays gated to `success`, so no errored terminal can yield text through that branch. That is *not* the same as saying `ask` returns `""` for a genuine SDK error: in production the real SDK raises after an `is_error` result — the CLI process exits non-zero and `query()` turns that into an exception (see `claude_agent_sdk/_internal/query.py`) — so `ask` propagates that exception instead of returning `""`. The recorded cost survives the raise because `record` runs first; callers such as `verify_claims` catch the exception and degrade the claim to `contested`. The subtype classification lives only in `cost.py` (`is_terminal_result` / `is_success_result` / `is_error_result`) and is never re-derived in `llm.py`.
- **`tests/conftest.py` clears `ROUTE_VARS` for every test** (autouse fixture), so the developer's own shell cannot flip the default-branch billing assertions. Tests wanting the unknown branch set the var themselves.
- **No test may make a real LLM call.** Any path reaching `AgentClassifier.ask` must monkeypatch `archeon.llm.query` before `runner.invoke`.
- **`total_usd` is rounded to 4 decimal places** in `summary_dict`; the printed total uses `:.4f`.
- **Stage names** (string literals, pinned by tests): `"synthesize"`, `"verify"`, `"cluster-label"`.
- **Test command:** `uv run pytest`. Run from the repo root.
- **Line length:** the codebase wraps at 79 columns. Match it.
- **Commit trailer:** every commit ends with
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/archeon/cost.py` | **create** | `CallCost` dataclass, `_usage_get`, `call_cost_from_message`, `CostMeter`. Pure aggregation + formatting. No SDK, no click, no I/O. |
| `tests/test_cost.py` | **create** | Unit tests for the above against hand-rolled fake messages. No SDK import. |
| `src/archeon/llm.py` | modify (`:40-44`, `:49-62`) | `AgentClassifier` accepts `meter`/`stage`; records one `CallCost` per terminal message (success or error subtype), reading `.result` only on success. |
| `tests/test_llm.py` | modify (append) | Backward-compat (no meter) + recording (with meter) + no-success (meter untouched). |
| `src/archeon/cli.py` | modify (`:1-17`, `:147-174`, `:190-270`) | Build one meter per run, thread it into every `AgentClassifier`, echo the summary, write `run_cost.json` (synthesize only, `encoding="utf-8"`). |
| `tests/test_cli.py` | modify (append) | `synthesize` writes a parseable `run_cost.json`; `cluster` prints the summary (asserted inside `runner.isolated_filesystem()`). |
| `tests/conftest.py` | **create** | Autouse fixture clearing `cost.ROUTE_VARS`, so billing assertions do not depend on the developer's shell. |

`cluster` has no claims output directory, so it is **print-only** — no JSON file. That asymmetry is intentional. `CliRunner` does not sandbox the filesystem, so that asymmetry must be asserted from an isolated cwd — a `not (tmp_path / "run_cost.json").exists()` assertion would pass no matter what the command does.

> **Note on the code blocks in Tasks 1-3 below:** they are the pre-review draft. The shipped contract differs in the ways listed under Global Constraints (`failed_calls`, the `billed` tri-state, terminal-error recording, ASCII output, `command` in the printed line, `encoding="utf-8"`, and `format_summary`'s optional `summary=` parameter). Read `src/archeon/cost.py` and the [spec](../specs/2026-07-25-llm-cost-accounting-design.md), which were updated to match, as the source of truth.

---

### Task 1: `cost.py` — the meter

**Files:**
- Create: `src/archeon/cost.py`
- Test: `tests/test_cost.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces, relied on by Tasks 2 and 3:
  - `CallCost(stage: str, model: str, usd: float, input_tokens: int, output_tokens: int, cache_read_tokens: int, cache_creation_tokens: int, num_turns: int)` — a `@dataclass`.
  - `call_cost_from_message(msg, stage: str, model: str) -> CallCost`
  - `CostMeter()` with `.calls: list[CallCost]`, `.record(msg, stage: str, model: str) -> None`, `.total_usd: float` (property), `.by_stage() -> dict`, `.by_model() -> dict`, `.summary_dict(command: str) -> dict`, `.format_summary(command: str) -> str`.

**Background for the implementer:** the Claude Agent SDK's terminal message (`ResultMessage`) carries `total_cost_usd`, `usage`, and `num_turns`. It has **no** top-level `model` field — only a per-model `model_usage` mapping — which is why `model` is a caller-supplied argument here rather than something read off the message. `usage` may arrive as a plain `dict` or as an attribute-carrying object depending on SDK version, so both shapes must work. An errored or partial turn may be missing `total_cost_usd` entirely; that contributes `usd=0.0` but is still counted in `calls` and token totals, because the meter should reflect the attempt rather than silently drop it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cost.py`:

```python
import json

import pytest

from archeon.cost import CallCost, CostMeter, call_cost_from_message


class _Msg:
    """Minimal stand-in for the SDK's terminal ResultMessage.

    Only the attributes passed in exist, so a test can model an errored
    turn simply by omitting ``total_cost_usd``.
    """

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


def test_call_cost_from_message_extracts_dict_usage():
    msg = _Msg(total_cost_usd=0.25, num_turns=3, usage={
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 10,
    })
    assert call_cost_from_message(msg, "verify", "claude-sonnet-5") == \
        CallCost(stage="verify", model="claude-sonnet-5", usd=0.25,
                 input_tokens=1000, output_tokens=200,
                 cache_read_tokens=50, cache_creation_tokens=10,
                 num_turns=3)


def test_call_cost_is_zero_usd_when_total_cost_missing():
    # An errored/partial turn: no total_cost_usd, but the attempt counts.
    msg = _Msg(usage={"input_tokens": 10, "output_tokens": 2})
    c = call_cost_from_message(msg, "synthesize", "claude-sonnet-5")
    assert c.usd == 0.0
    assert c.input_tokens == 10
    assert c.cache_read_tokens == 0  # absent usage keys -> 0
    assert c.num_turns == 0


def test_call_cost_reads_attr_style_usage():
    class _Usage:
        input_tokens = 7
        output_tokens = 1
        cache_read_input_tokens = 4

    msg = _Msg(total_cost_usd=0.01, usage=_Usage(), num_turns=1)
    c = call_cost_from_message(msg, "cluster-label", "claude-haiku-4-5")
    assert (c.input_tokens, c.output_tokens, c.cache_read_tokens) == (7, 1, 4)
    assert c.cache_creation_tokens == 0


def test_call_cost_tolerates_missing_usage_entirely():
    c = call_cost_from_message(_Msg(total_cost_usd=0.5), "verify", "m")
    assert c.usd == 0.5
    assert (c.input_tokens, c.output_tokens) == (0, 0)


def test_meter_aggregates_by_stage_and_by_model():
    meter = CostMeter()
    meter.record(_Msg(total_cost_usd=0.10, num_turns=1,
                      usage={"input_tokens": 100, "output_tokens": 10}),
                 "synthesize", "claude-sonnet-5")
    meter.record(_Msg(total_cost_usd=0.05, num_turns=1,
                      usage={"input_tokens": 40, "output_tokens": 4}),
                 "verify", "claude-sonnet-5")
    meter.record(_Msg(total_cost_usd=0.01, num_turns=1,
                      usage={"input_tokens": 20, "output_tokens": 2}),
                 "cluster-label", "claude-haiku-4-5")

    assert len(meter.calls) == 3
    assert meter.total_usd == pytest.approx(0.16)

    by_stage = meter.by_stage()
    assert set(by_stage) == {"synthesize", "verify", "cluster-label"}
    assert by_stage["synthesize"] == {"usd": 0.10, "calls": 1,
                                      "input_tokens": 100,
                                      "output_tokens": 10,
                                      "cache_read_tokens": 0}

    by_model = meter.by_model()
    assert by_model["claude-sonnet-5"]["calls"] == 2
    assert by_model["claude-sonnet-5"]["usd"] == pytest.approx(0.15)
    assert by_model["claude-haiku-4-5"]["calls"] == 1


def test_summary_dict_has_stable_keys_and_flags_unbilled():
    meter = CostMeter()
    meter.record(_Msg(total_cost_usd=0.123456789, num_turns=1,
                      usage={"input_tokens": 10, "output_tokens": 1}),
                 "verify", "claude-sonnet-5")
    d = meter.summary_dict("synthesize")

    assert set(d) == {"command", "generated_at", "total_usd", "calls",
                      "billed", "note", "by_stage", "by_model"}
    assert d["command"] == "synthesize"
    assert d["total_usd"] == 0.1235  # rounded to 4dp
    assert d["calls"] == 1
    assert d["billed"] is False
    assert "subscription auth" in d["note"]
    # cli.py json.dumps() this straight to disk, so it must round-trip.
    assert json.loads(json.dumps(d))["total_usd"] == 0.1235


def test_format_summary_labels_cost_as_api_equivalent():
    meter = CostMeter()
    meter.record(_Msg(total_cost_usd=1.5, num_turns=1,
                      usage={"input_tokens": 900, "output_tokens": 90}),
                 "verify", "claude-sonnet-5")
    text = meter.format_summary("synthesize")
    assert "API-equivalent" in text
    assert "not billed" in text
    assert "$1.5000" in text
    assert "verify" in text
    assert "in 900" in text and "out 90" in text


def test_empty_meter_is_a_valid_zero_record():
    meter = CostMeter()
    assert meter.total_usd == 0.0
    assert meter.summary_dict("synthesize")["calls"] == 0
    assert meter.summary_dict("synthesize")["by_stage"] == {}
    assert "$0.0000" in meter.format_summary("synthesize")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_cost.py -v
```

Expected: collection error — `ModuleNotFoundError: No module named 'archeon.cost'`.

- [ ] **Step 3: Write the implementation**

Create `src/archeon/cost.py`:

```python
"""Post-run LLM cost accounting.

Aggregates the real per-call cost the Claude Agent SDK reports on its
terminal ``ResultMessage``. Deliberately SDK-free: everything here reads
duck-typed attributes off whatever object it is handed, so it is testable
without the SDK installed and cannot drift with SDK imports.

The dollar figures are **API-equivalent** — this project authenticates via
the Claude CLI's subscription login (see ``archeon.llm._cli_auth_env``), so
nothing here was actually billed to an API key.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

NOTE = ("API-equivalent cost from the Claude Agent SDK; this run used "
        "Claude CLI subscription auth and was not billed to an API key.")


@dataclass
class CallCost:
    stage: str
    model: str
    usd: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    num_turns: int


def _usage_get(usage, key: str) -> int:
    """Read a token count off a usage payload that may be dict or object."""
    if usage is None:
        return 0
    if isinstance(usage, dict):
        return int(usage.get(key) or 0)
    return int(getattr(usage, key, 0) or 0)


def call_cost_from_message(msg, stage: str, model: str) -> CallCost:
    """Extract one CallCost from a Claude Agent SDK ResultMessage.

    ``model`` is supplied by the caller (AgentClassifier knows it) — the
    SDK's ResultMessage has no top-level ``model`` field, only a per-model
    ``model_usage`` mapping. An errored/partial turn with no
    ``total_cost_usd`` contributes usd=0 but is still counted, so calls and
    tokens reflect the attempt.
    """
    usage = getattr(msg, "usage", None)
    return CallCost(
        stage=stage,
        model=model,
        usd=float(getattr(msg, "total_cost_usd", 0.0) or 0.0),
        input_tokens=_usage_get(usage, "input_tokens"),
        output_tokens=_usage_get(usage, "output_tokens"),
        cache_read_tokens=_usage_get(usage, "cache_read_input_tokens"),
        cache_creation_tokens=_usage_get(usage,
                                         "cache_creation_input_tokens"),
        num_turns=int(getattr(msg, "num_turns", 0) or 0),
    )


class CostMeter:
    """Accumulates CallCosts across every LLM call in one command run."""

    def __init__(self) -> None:
        self.calls: list[CallCost] = []

    def record(self, msg, stage: str, model: str) -> None:
        self.calls.append(call_cost_from_message(msg, stage, model))

    @property
    def total_usd(self) -> float:
        return sum(c.usd for c in self.calls)

    def _group(self, key) -> dict:
        out: dict[str, dict] = {}
        for c in self.calls:
            b = out.setdefault(key(c), {"usd": 0.0, "calls": 0,
                                        "input_tokens": 0,
                                        "output_tokens": 0,
                                        "cache_read_tokens": 0})
            b["usd"] += c.usd
            b["calls"] += 1
            b["input_tokens"] += c.input_tokens
            b["output_tokens"] += c.output_tokens
            b["cache_read_tokens"] += c.cache_read_tokens
        return out

    def by_stage(self) -> dict:
        return self._group(lambda c: c.stage)

    def by_model(self) -> dict:
        return self._group(lambda c: c.model or "unknown")

    def summary_dict(self, command: str) -> dict:
        return {
            "command": command,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_usd": round(self.total_usd, 4),
            "calls": len(self.calls),
            "billed": False,
            "note": NOTE,
            "by_stage": self.by_stage(),
            "by_model": self.by_model(),
        }

    def format_summary(self, command: str) -> str:
        lines = [f"cost: ≈ ${self.total_usd:.4f} API-equivalent "
                 f"({len(self.calls)} calls; subscription auth — "
                 f"not billed)"]
        for stage, b in sorted(self.by_stage().items()):
            lines.append(f"  {stage:<16} ${b['usd']:.4f}  "
                         f"{b['calls']} calls  "
                         f"in {b['input_tokens']}  out {b['output_tokens']}")
        return "\n".join(lines)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_cost.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Run the full suite to confirm nothing regressed**

```bash
uv run pytest
```

Expected: all pass (this task adds a leaf module nothing imports yet).

- [ ] **Step 6: Commit**

```bash
git add src/archeon/cost.py tests/test_cost.py && git commit -m "$(cat <<'EOF'
feat(cost): CostMeter for post-run LLM cost accounting

SDK-free accumulator over the Claude Agent SDK's per-call total_cost_usd
and usage. Labels totals API-equivalent: this project authenticates with
Claude CLI subscription auth, so nothing is billed to an API key.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Thread the meter through `AgentClassifier`

**Files:**
- Modify: `src/archeon/llm.py:40-44` (constructor), `src/archeon/llm.py:49-62` (`_ask`)
- Test: `tests/test_llm.py` (append)

**Interfaces:**
- Consumes: `archeon.cost.CostMeter` from Task 1 — specifically `meter.record(msg, stage, model)`.
- Produces, relied on by Task 3:
  `AgentClassifier(model: str, system_prompt: str = SYSTEM_PROMPT, max_turns: int = 1, meter=None, stage: str = "")`.
  `ask(prompt) -> str` is unchanged. With `meter=None` (the default) nothing is recorded and behavior is identical to today.

**Background for the implementer:** `_ask` currently loops the async `query(...)` generator and keeps the `.result` off the terminal success message, discarding everything else on that message — including the cost fields. The shipped implementation records on *every* terminal message, not just inside that branch — the SDK's error subtypes still carry the cost fields and burned real quota — while the `.result` read itself stays gated to success only. Do **not** drop the `with _cli_auth_env():` context manager that wraps the loop; it is what makes the spawned CLI use subscription auth, and losing it would silently switch the project to API-key billing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_llm.py`:

```python
class _FakeCostedResult:
    """Terminal ResultMessage carrying the SDK's real cost fields."""

    subtype = "success"

    def __init__(self, result, usd=0.42):
        self.result = result
        self.total_cost_usd = usd
        self.num_turns = 2
        self.usage = {"input_tokens": 1200, "output_tokens": 80,
                      "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}


def test_ask_without_a_meter_is_unchanged(monkeypatch):
    fake = _fake_query_factory([_FakeCostedResult("  YES  ")])
    monkeypatch.setattr(llm, "query", fake)
    classifier = llm.AgentClassifier("cheap-model")
    assert classifier.ask("q") == "YES"
    assert classifier._meter is None


def test_ask_records_one_call_into_the_meter(monkeypatch):
    from archeon.cost import CostMeter

    monkeypatch.setattr(llm, "query",
                        _fake_query_factory([_FakeCostedResult("YES")]))
    meter = CostMeter()
    classifier = llm.AgentClassifier("sonnet-model", max_turns=4,
                                     meter=meter, stage="verify")
    assert classifier.ask("q") == "YES"

    assert len(meter.calls) == 1
    call = meter.calls[0]
    assert call.stage == "verify"
    # model is caller-supplied; the SDK message has no top-level model field
    assert call.model == "sonnet-model"
    assert call.usd == 0.42
    assert call.input_tokens == 1200
    assert call.num_turns == 2


def test_meter_untouched_when_no_success_message(monkeypatch):
    from archeon.cost import CostMeter

    monkeypatch.setattr(llm, "query", _fake_query_factory([_FakeNonResult()]))
    meter = CostMeter()
    classifier = llm.AgentClassifier("m", meter=meter, stage="verify")
    assert classifier.ask("q") == ""
    assert meter.calls == []
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_llm.py -v
```

Expected: `test_ask_without_a_meter_is_unchanged` fails with `AttributeError: 'AgentClassifier' object has no attribute '_meter'`; the other two fail with `TypeError: __init__() got an unexpected keyword argument 'meter'`.

- [ ] **Step 3: Write the implementation**

In `src/archeon/llm.py`, replace the constructor (currently lines 40-44):

```python
    def __init__(self, model: str, system_prompt: str = SYSTEM_PROMPT,
                 max_turns: int = 1, meter=None, stage: str = "") -> None:
        self._model = model
        self._system_prompt = system_prompt
        self._max_turns = max_turns
        # Optional archeon.cost.CostMeter. None (the default) means no
        # recording and byte-for-byte the pre-cost-accounting behavior.
        self._meter = meter
        self._stage = stage
```

Then replace `_ask` (currently lines 49-62):

```python
    async def _ask(self, prompt: str) -> str:
        options = ClaudeAgentOptions(
            model=self._model,
            system_prompt=self._system_prompt,
            allowed_tools=[],
            max_turns=self._max_turns,
        )
        result = ""
        with _cli_auth_env():
            async for message in query(prompt=prompt, options=options):
                if getattr(message, "subtype", None) == "success" and \
                        hasattr(message, "result"):
                    if self._meter is not None:
                        self._meter.record(message, self._stage, self._model)
                    result = message.result or ""
        return result.strip()
```

Also extend the class docstring's final paragraph so the meter is discoverable:

```python
    Authenticates via the Claude CLI's own login (subscription/OAuth), not a
    raw API key — see ``_cli_auth_env``. Because of that, any cost recorded
    into an injected ``meter`` is API-equivalent, not actually billed.
    """
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_llm.py -v
```

Expected: all pass, including the four pre-existing tests (`test_ask_extracts_final_result`, `test_max_turns_is_configurable`, `test_ask_returns_empty_when_no_success`, and both `_cli_auth_env` tests) — those are the backward-compatibility guarantee for `link_llm`.

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest
```

Expected: all pass. `tests/test_link_llm.py` exercises the other `AgentClassifier` caller and must be unaffected.

- [ ] **Step 6: Commit**

```bash
git add src/archeon/llm.py tests/test_llm.py && git commit -m "$(cat <<'EOF'
feat(llm): optional cost meter on AgentClassifier

Record the SDK's terminal ResultMessage into an injected CostMeter at the
existing success branch. meter=None is the default, so link-llm and every
other caller keep today's behavior exactly.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Wire `synthesize` and `cluster`

**Files:**
- Modify: `src/archeon/cli.py:1-17` (add `import json`), `src/archeon/cli.py:147-174` (`cli_cluster`), `src/archeon/cli.py:190-270` (`cli_synthesize`)
- Test: `tests/test_cli.py` (append; also add `import json` at the top)

**Interfaces:**
- Consumes: `CostMeter` from Task 1 (`.format_summary(command)`, `.summary_dict(command)`) and the `meter=` / `stage=` params on `AgentClassifier` from Task 2.
- Produces: `<out_dir>/run_cost.json` from `synthesize`; a printed cost block from both commands.

**Background for the implementer:**

- `cli_synthesize` constructs **two** `AgentClassifier` instances per target inside the `for label, cid in targets:` loop (`src/archeon/cli.py:249-253`). Both must receive the *same* meter — build it once before the loop — with stages `"synthesize"` and `"verify"` respectively.
- Write `run_cost.json` **after** `save_claims(...)` (`src/archeon/cli.py:267`). The ordering is the safety property: claims are already durable on disk before the cost file is attempted, so a failure writing it can never cost you a run's claims. No `try`/`except` is needed — `save_claims` creates `out_dir`, so if the directory were unwritable `save_claims` would already have failed.
- `cli_cluster` builds one `labeller` (`src/archeon/cli.py:164-165`) that `cluster_symbols` invokes once per cluster via `label_fn`. Stage is `"cluster-label"`. `cluster` has no output directory, so it prints the summary and writes no file.
- **Zero-call runs are valid records.** If a run reaches the end having made no LLM calls (e.g. the CLI tests stub out `synthesize_claims`/`verify_claims`), the summary still prints and the JSON still writes with `total_usd: 0.0, calls: 0`. Note this is distinct from the `--feature`-with-no-bundle path at `src/archeon/cli.py:246-248`, which raises `ClickException` and aborts before either claims or cost are written — that stays as it is.

- [ ] **Step 1: Write the failing tests**

Add `import json` to the top of `tests/test_cli.py` (it currently begins `import subprocess`):

```python
import json
import subprocess
from pathlib import Path
```

Then append these two tests:

```python
def test_synthesize_writes_run_cost_json(tmp_path, monkeypatch):
    config = _setup(tmp_path)
    runner = CliRunner()
    assert runner.invoke(main, ["scan", "--config", str(config)]).exit_code == 0

    import archeon.claims.recover as recover_mod
    from archeon.claims.schema import Claim

    def fake_synth(feature, bundle, ask):
        return [Claim(id="CLM-0001", type="threshold", statement="s",
                      feature=feature, layer="what", status="recovered")]

    def fake_verify(claims, bundle, ask):
        for c in claims:
            c.status = "machine_verified"

    monkeypatch.setattr(recover_mod, "synthesize_claims", fake_synth)
    monkeypatch.setattr(recover_mod, "verify_claims", fake_verify)

    out = tmp_path / "claims_out"
    r = runner.invoke(main, ["synthesize", "--config", str(config),
                             "--feature", "src/", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "API-equivalent" in r.output
    assert "not billed" in r.output

    data = json.loads((out / "run_cost.json").read_text())
    assert data["command"] == "synthesize"
    assert data["billed"] is False
    assert "subscription auth" in data["note"]
    assert set(data) >= {"generated_at", "total_usd", "calls",
                         "by_stage", "by_model"}
    # The stubs never reach the LLM, so this is a valid zero record.
    assert data["calls"] == 0
    assert data["total_usd"] == 0.0


def test_cluster_prints_cost_summary(tmp_path, monkeypatch):
    config = _setup(tmp_path)
    runner = CliRunner()
    assert runner.invoke(main, ["scan", "--config", str(config)]).exit_code == 0

    import requests as _rq

    import archeon.retrieval.embed as embed_mod
    monkeypatch.setattr(embed_mod, "embed_texts",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _rq.RequestException("refused")))
    import archeon.retrieval.cluster as cluster_mod
    monkeypatch.setattr(cluster_mod, "label_cluster",
                        lambda rows, ask: ("", ""))

    r = runner.invoke(main, ["cluster", "--config", str(config)])
    assert r.exit_code == 0, r.output
    assert "clusters:" in r.output
    assert "API-equivalent" in r.output
    # cluster has no output directory, so it is print-only.
    assert not (tmp_path / "run_cost.json").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_cli.py -v -k "run_cost or cost_summary"
```

Expected: `test_synthesize_writes_run_cost_json` fails on `assert "API-equivalent" in r.output`; `test_cluster_prints_cost_summary` fails on the same assertion.

- [ ] **Step 3: Add the `json` import to `cli.py`**

`src/archeon/cli.py` currently starts:

```python
import os
from pathlib import Path
```

Make it:

```python
import json
import os
from pathlib import Path
```

- [ ] **Step 4: Wire `cli_synthesize`**

In `src/archeon/cli.py`, add `CostMeter` to the lazy import block at the top of `cli_synthesize` (currently `src/archeon/cli.py:207-212`):

```python
    from archeon.claims.recover import (
        SYNTH_SYSTEM, VERIFY_SYSTEM, synthesize_claims, verify_claims)
    from archeon.claims.pin import pin_claims
    from archeon.claims.schema import save_claims
    from archeon.cost import CostMeter
    from archeon.llm import AgentClassifier
    from archeon.retrieval.bundle import bundle_for_cluster, bundle_for_prefix
```

Replace the model/loop setup (currently `src/archeon/cli.py:239-241`) so one meter is created before the loop:

```python
    model = cfg["llm"].get("expensive_model", cfg["llm"]["cheap_model"])
    # One meter for the whole run: every classifier below records into it.
    meter = CostMeter()
    all_claims = []
    for label, cid in targets:
```

Replace the two classifier constructions inside the loop (currently `src/archeon/cli.py:249-253`):

```python
        claims = synthesize_claims(
            label, bundle,
            AgentClassifier(model, SYNTH_SYSTEM, max_turns=4,
                            meter=meter, stage="synthesize").ask)
        verify_claims(claims, bundle,
                      AgentClassifier(model, VERIFY_SYSTEM, max_turns=4,
                                      meter=meter, stage="verify").ask)
```

Finally, replace the closing echo (currently `src/archeon/cli.py:267-270`) with the claims summary plus the cost report. The cost file is written *after* `save_claims`, so claims are already durable if the write fails:

```python
    save_claims(all_claims, Path(out_dir))
    verified = sum(1 for c in all_claims if c.status == "machine_verified")
    click.echo(f"claims: {len(all_claims)}  machine_verified: {verified}  "
               f"contested: {len(all_claims) - verified}  -> {out_dir}/")
    click.echo(meter.format_summary("synthesize"))
    (Path(out_dir) / "run_cost.json").write_text(
        json.dumps(meter.summary_dict("synthesize"), indent=2))
```

- [ ] **Step 5: Wire `cli_cluster`**

In `src/archeon/cli.py`, extend the lazy import block in `cli_cluster` (currently `src/archeon/cli.py:151-154`):

```python
    from archeon.cost import CostMeter
    from archeon.llm import AgentClassifier
    from archeon.retrieval.cluster import (
        LABEL_SYSTEM, cluster_symbols, label_cluster)
    from archeon.retrieval.embed import build_embedding_index
```

Replace the `labeller` construction (currently `src/archeon/cli.py:164-165`):

```python
    meter = CostMeter()
    labeller = AgentClassifier(cfg["llm"]["cheap_model"], LABEL_SYSTEM,
                               max_turns=1, meter=meter,
                               stage="cluster-label")
```

And append the cost line after the per-cluster listing (currently ends at `src/archeon/cli.py:174`). `cluster` has no output directory, so this is print-only:

```python
    for c in clusters:
        click.echo(f"  [{c['id']}] {c['label'] or '(unlabelled)'}  "
                   f"({len(c['members'])} symbols)")
    click.echo(meter.format_summary("cluster"))
```

- [ ] **Step 6: Run the new tests to verify they pass**

```bash
uv run pytest tests/test_cli.py -v -k "run_cost or cost_summary"
```

Expected: 2 passed.

- [ ] **Step 7: Run the full suite**

```bash
uv run pytest
```

Expected: all pass. Watch `test_synthesize_feature_without_clusters_uses_fallback` and `test_cluster_runs_graph_only_without_ollama` in particular — they assert on the same commands' output and must still pass with the extra cost lines appended.

- [ ] **Step 8: Commit**

```bash
git add src/archeon/cli.py tests/test_cli.py && git commit -m "$(cat <<'EOF'
feat(cli): report actual LLM cost for synthesize and cluster

One CostMeter per run threaded into every AgentClassifier. synthesize
prints the summary and writes run_cost.json beside the claims (after
save_claims, so a write failure can never cost a run's claims); cluster
has no output dir and is print-only.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Manual verification (after Task 3)

The unit and CLI tests never touch the network, so the real SDK fields are only exercised by a live run. Against a scanned component:

```bash
uv run archeon synthesize --config archeon.example.scoped.toml --feature src/ --out /tmp/cost-check
```

Confirm, in `/tmp/cost-check/run_cost.json` and the printed block:

1. A non-zero `~ $N.NNNN API-equivalent` total, `total_usd > 0`, and both `synthesize` and `verify` present under `by_stage`. If `total_usd` comes back `0.0` on a run that clearly made calls, the SDK's `total_cost_usd` field name has drifted — check the terminal message before assuming the meter is broken.
2. **Non-zero `input_tokens` and `output_tokens` for every stage bucket.** This is the field-name check that actually matters, and no test can do it. `total_usd` is read off the message's own `total_cost_usd`, but every token count is read out of the passthrough `usage` payload by literal key: `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`. The sibling `ModelUsage` TypedDict in the same SDK file spells the cache keys in **camelCase** (`cacheReadInputTokens`, `cacheCreationInputTokens`). If `usage` ever arrives in that shape, `_usage_get` returns `0` for every key and the report still looks healthy because `total_usd` is unaffected — silent, total loss of the token columns. Zero tokens on a run that spent dollars means the key names drifted, not that the model read nothing.
3. `by_model` has exactly one key and it equals the model the run was configured with (`[llm] expensive_model`, else `cheap_model`). `model` is caller-supplied rather than read off the message, so a wrong key here means the CLI threaded the wrong model string, not an SDK change.
4. `billed` is `false` and `note` mentions subscription auth — unless you have `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX` / `ANTHROPIC_BASE_URL` set, in which case it must be `null` with the unknown-route note. Check your own env before reading this field: `null` here is correct behavior, not a bug.
5. `failed_calls` — `0` on a clean run (and then no warning line in the printed block). To exercise the other side deliberately, run a cluster large enough to hit `max_turns` and confirm the count and the warning line appear while the command still exits 0.

## Out of scope (per the spec's non-goals)

- No pre-run estimate or `--dry-run` forecast.
- No maintained per-model price table — the dollar figure comes from the SDK.
- No new DB table; reporting is the printed summary plus the JSON file.
- No change to authentication or model selection.
- `link-llm` gets no meter. It could take one trivially (`meter=`/`stage=` are already there), but the spec scopes this to `synthesize` and `cluster`.
