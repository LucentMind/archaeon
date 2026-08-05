"""Post-run LLM cost accounting.

Aggregates the real per-call cost the Claude Agent SDK reports on its
terminal ``ResultMessage``. Deliberately SDK-free: everything here reads
duck-typed attributes off whatever object it is handed, so it is testable
without the SDK installed and cannot drift with SDK imports.

The dollar figures are normally **API-equivalent** — this project
authenticates via the Claude CLI's subscription login (see
``archaeon.llm._cli_auth_env``), so nothing was actually billed to an API
key. That claim only holds while nothing re-routes the spawned CLI, so it is
probed at report time by ``billing_route_overridden``: if the environment
routes it elsewhere, the billing route is reported as unknown rather than
asserted to be free.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timezone

NOTE = ("API-equivalent cost from the Claude Agent SDK; this run used "
        "Claude CLI subscription auth and was not billed to an API key.")

UNKNOWN_NOTE = ("Cost as reported by the Claude Agent SDK. The billing "
                "route is unknown: one of CLAUDE_CODE_USE_BEDROCK, "
                "CLAUDE_CODE_USE_VERTEX or ANTHROPIC_BASE_URL is set, so "
                "this cost may have been really billed.")

# Env vars that route the spawned Claude CLI away from the subscription
# login. ``archaeon.llm._cli_auth_env`` strips only ANTHROPIC_API_KEY /
# ANTHROPIC_AUTH_TOKEN, so these routes (and a Console/API-billed CLI login)
# are invisible from here — hence "unknown", never "not billed".
ROUTE_VARS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
              "ANTHROPIC_BASE_URL")

# Terminal ResultMessage subtypes. Only "success" carries a usable
# ``.result``; the SDK's error variants (error_max_turns,
# error_during_execution) still report total_cost_usd / usage / num_turns,
# so they burned real quota and must be counted rather than dropped. Errors
# are matched by prefix so a future ``error_*`` subtype stays counted.
# Note the SDK can also set ``is_error`` on a "success" subtype. There is no
# reliable way here to tell a recovered retry apart from a hard failure that
# still landed on subtype "success", so we key off subtype alone and count
# such a call as successful. That makes ``failed_calls`` a lower bound on
# failures, not an exact count.
SUCCESS_SUBTYPE = "success"
ERROR_SUBTYPE_PREFIX = "error"


def is_success_result(msg) -> bool:
    """True for the terminal ResultMessage that carries a usable result."""
    return getattr(msg, "subtype", None) == SUCCESS_SUBTYPE


def is_error_result(msg) -> bool:
    """True for a terminal ResultMessage reporting an error subtype."""
    subtype = getattr(msg, "subtype", None)
    return isinstance(subtype, str) and \
        subtype.startswith(ERROR_SUBTYPE_PREFIX)


def is_terminal_result(msg) -> bool:
    """True for any terminal ResultMessage — costed either way.

    The single source of truth for this classification: ``archaeon.llm``
    gates both its recording and its result read on these helpers rather
    than re-deriving the subtype semantics.
    """
    return is_success_result(msg) or is_error_result(msg)


def billing_route_overridden() -> bool:
    """True when the environment routes the CLI off subscription auth.

    An empty value does not count as a route. When this is true the Python
    side cannot know whether the run was billed, so ``summary_dict`` reports
    ``billed: None`` and both surfaces say the route is unknown.
    """
    return any(os.environ.get(var) for var in ROUTE_VARS)


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
    failed: bool = False


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
    ``model_usage`` mapping. An errored/partial turn is still counted, so
    calls and tokens reflect the attempt: it is flagged ``failed`` (from its
    subtype), and if it lacks ``total_cost_usd`` it contributes usd=0.
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
        failed=is_error_result(msg),
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

    @property
    def failed_calls(self) -> int:
        """Calls that ended on an error subtype (quota spent, no answer)."""
        return sum(1 for c in self.calls if c.failed)

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
        unknown = billing_route_overridden()
        return {
            "command": command,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_usd": round(self.total_usd, 4),
            "calls": len(self.calls),
            "failed_calls": self.failed_calls,
            # None (JSON null) = the billing route is unknowable from here.
            "billed": None if unknown else False,
            "note": UNKNOWN_NOTE if unknown else NOTE,
            "by_stage": self.by_stage(),
            "by_model": self.by_model(),
        }

    def format_summary(self, command: str,
                       summary: dict | None = None) -> str:
        """Render the printed block.

        ``summary`` lets a caller pass in an already-computed
        ``summary_dict(command)`` so the printed line and the JSON file it
        writes alongside share one probe of ``billing_route_overridden()``
        instead of each taking their own live read of ``os.environ`` — the
        two surfaces then structurally cannot disagree. Pass it from the same
        ``command`` you pass here: the header label comes from ``command``
        while the numbers come from ``summary``. ``cli_synthesize`` is the
        only caller that passes one; everywhere else it is omitted and this
        method computes its own via ``summary_dict``.
        """
        # ASCII only: click.echo writes this to a possibly cp1252 stdout,
        # and it happens after claims are saved but before run_cost.json.
        if summary is None:
            summary = self.summary_dict(command)
        unknown = summary["billed"] is None
        if unknown:
            head = (f"cost [{command}]: ~ ${summary['total_usd']:.4f} as "
                    f"reported by the SDK ({summary['calls']} calls; "
                    f"billing route unknown - may be really billed)")
        else:
            head = (f"cost [{command}]: ~ ${summary['total_usd']:.4f} "
                    f"API-equivalent ({summary['calls']} calls; "
                    f"subscription auth - not billed)")
        lines = [head]
        if summary["failed_calls"]:
            lines.append(f"  warning: {summary['failed_calls']} of "
                         f"{summary['calls']} calls failed (errored turns "
                         f"still spend quota)")
        for stage, b in sorted(summary["by_stage"].items()):
            lines.append(f"  {stage:<16} ${b['usd']:.4f}  "
                         f"{b['calls']} calls  "
                         f"in {b['input_tokens']}  out {b['output_tokens']}")
        return "\n".join(lines)
