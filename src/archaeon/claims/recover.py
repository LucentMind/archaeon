import json
import re
from pathlib import Path

from archaeon.claims.schema import CLAIM_TYPES, Claim, Evidence

SYNTH_SYSTEM = (
    "You recover what-layer requirement claims from C/C++ source. A what-layer "
    "claim is a testable statement about behavior, structure, or a constraint "
    "value that the code itself establishes — a state transition, timing "
    "budget, threshold/limit, conditional rule, interaction sequence, or "
    "invariant. Cite the exact code. Do NOT invent rationale or intent (that "
    "is the why-layer, which code cannot settle). Output only JSON."
)

SYNTH_PROMPT = """Feature: {feature}

Source (line-numbered):
{bundle}

Return a JSON array of what-layer claims. Each object:
{{"type": one of {types},
  "statement": a single testable sentence grounded in the code,
  "symbols": [relevant function/struct names],
  "evidence": [{{"ref": "path:line", "excerpt": "the exact code"}}]}}
In each ref use the FULL repo-relative path exactly as it appears in the "==="
header preceding the cited lines (e.g. "src/foo/bar.cpp:214"), never a bare
filename. Only claims the code directly establishes; no rationale. Output the
JSON array only."""

VERIFY_SYSTEM = (
    "You are an adversarial verifier. Given a claim and the C/C++ source it "
    "cites, try to REFUTE it: does the cited code establish the claim exactly "
    "as stated? Pay special attention to scope: if the claim names multiple "
    "symbols (e.g. a getter and setter, or a horizontal and vertical variant "
    "of the same operation), the citation must cover the behavior of EACH "
    "named symbol, not just one representative case — a citation covering "
    "only a subset of the named symbols does not establish a claim made "
    "about all of them, even if the covered portion is accurate. Also check "
    "whether the claim's mechanism is actually demonstrated by the cited "
    "code itself, not merely asserted by a nearby comment, and whether it "
    "depends on logic outside the cited line range. Default to refuted if "
    "the code is ambiguous or the citation does not support the statement. "
    "Output only JSON."
)

VERIFY_PROMPT = """Claim ({type}): {statement}
Named symbols: {symbols}
Cited refs: {refs}

Source (line-numbered):
{bundle}

Return JSON: {{"supported": true|false, "confidence": 0.0-1.0,
"counter": "why it fails, or empty string"}}. Output the JSON only."""


def _strip_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _confidence(value, default: float) -> float:
    """Coerce a model-supplied confidence, falling back on junk.

    A reply can be valid JSON yet carry a non-numeric confidence
    ("high", null). An unguarded float() would raise past the per-claim
    isolation and discard every already-verified claim.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def synthesize_claims(feature: str, bundle: str, ask) -> list:
    raw = ask(SYNTH_PROMPT.format(feature=feature, bundle=bundle,
                                  types=sorted(CLAIM_TYPES)))
    try:
        items = json.loads(_strip_fence(raw))
    except (ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    claims = []
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            continue
        if it.get("type") not in CLAIM_TYPES or not it.get("statement"):
            continue
        evidence = [Evidence(kind="code", role="primary",
                             ref=e.get("ref", ""), excerpt=e.get("excerpt", ""))
                    for e in it.get("evidence", []) if isinstance(e, dict)]
        claims.append(Claim(
            id=f"CLM-{i:04d}", type=it["type"],
            statement=str(it["statement"]).strip(), feature=feature,
            layer="what", status="recovered", confidence=0.5,
            symbols=list(it.get("symbols", [])), evidence=evidence))
    return claims


def verify_claims(claims: list, bundle: str, ask) -> None:
    """Adversarially verify each claim in place: sets status to
    machine_verified or contested and adjusts confidence.

    One claim's verification call failing outright (e.g. the agent backend
    erroring) shouldn't discard every other claim's result — `save_claims`
    only runs after this whole loop returns, so an uncaught exception here
    used to lose all prior progress. Treat a failed call the same as an
    unparsable reply: contested, with the error recorded as counter-evidence.
    """
    for c in claims:
        refs = ", ".join(e.ref for e in c.evidence)
        symbols = ", ".join(c.symbols) if c.symbols else "(none listed)"
        try:
            raw = ask(VERIFY_PROMPT.format(type=c.type, statement=c.statement,
                                           symbols=symbols, refs=refs,
                                           bundle=bundle))
        except Exception as e:
            c.status = "contested"
            c.confidence = 0.0
            c.counter_evidence = [f"verification call failed: {e}"]
            continue
        try:
            v = json.loads(_strip_fence(raw))
        except (ValueError, TypeError):
            v = {}
        if isinstance(v, dict) and v.get("supported") is True:
            c.status = "machine_verified"
            c.confidence = _confidence(v.get("confidence"), 0.8)
        else:
            c.status = "contested"
            c.confidence = _confidence(
                v.get("confidence"), 0.3) if isinstance(v, dict) else 0.3
            counter = v.get("counter") if isinstance(v, dict) else None
            if counter:
                c.counter_evidence = [counter]


def build_feature_bundle(repo_path: Path, paths: list,
                         max_chars: int = 60000) -> str:
    """Assemble a line-numbered source bundle for the feature's parsed files."""
    parts, total = [], 0
    for rel in paths:
        f = Path(repo_path) / rel
        if not f.is_file():
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        numbered = "\n".join(f"{i}: {ln}" for i, ln in enumerate(lines, 1))
        block = f"=== {rel} ===\n{numbered}\n"
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
    return "\n".join(parts)
