"""Why-layer claim recovery (Pass 2).

Mirrors ``claims/recover.py``: prompts as module constants, an injected
``ask`` callable, and in-place mutation of Claim objects. The cost meter is
attached to the AgentClassifier by the CLI, so nothing here knows about it.
"""

import json
import re
from dataclasses import replace

from archaeon.claims.recover import _confidence, _strip_fence
from archaeon.claims.schema import (
    CODE_INFERRED_MAX_CONFIDENCE, WHY_CLAIM_TYPES, Claim, Evidence)

_WS = re.compile(r"\s+")

# Artifact ref forms the synthesizer may cite (see why_corpus manifest).
_PR_RE = re.compile(r"^pr:(\d+)$")
_PR_COMMENT_RE = re.compile(r"^pr_comment:(.+)$")
_TICKET_RE = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")


def normalize_text(s: str) -> str:
    """Fold a body and a quoted excerpt into one comparable form.

    Real PR bodies in the lake contain literal CRLF, and models reflow
    whitespace when quoting, so comparing raw strings would reject genuine
    citations. Casefolds too: quote capitalisation is not evidence.
    """
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    return _WS.sub(" ", s).strip().casefold()


def artifact_body(conn, ref: str) -> str | None:
    """Full searchable text of the artifact a ref names, or None if absent."""
    ref = (ref or "").strip()
    m = _PR_RE.match(ref)
    if m:
        row = conn.execute("SELECT title, body FROM prs WHERE number=?",
                           (int(m.group(1)),)).fetchone()
        return None if row is None else \
            f"{row['title'] or ''}\n{row['body'] or ''}"
    m = _PR_COMMENT_RE.match(ref)
    if m:
        row = conn.execute("SELECT body FROM pr_comments WHERE id=?",
                           (m.group(1),)).fetchone()
        return None if row is None else (row["body"] or "")
    if _TICKET_RE.match(ref):
        row = conn.execute(
            "SELECT summary, description FROM tickets WHERE key=?",
            (ref,)).fetchone()
        return None if row is None else \
            f"{row['summary'] or ''}\n{row['description'] or ''}"
    return None


def _is_artifact(e) -> bool:
    return e.kind in ("ticket", "pr", "pr_comment")


def ground_citations(claims, conn) -> dict:
    """Drop artifact citations that are not literally in the lake.

    Deterministic and LLM-free: the excerpt must actually occur in the cited
    artifact's stored text after normalization. This is where fabricated
    quotes die, before any model is asked to judge them.

    A claim left with no artifact evidence keeps its code hypothesis and
    becomes `code_inferred`: confidence capped, status untouched, and (by
    `verify_why_claims` skipping it) never auto-verified.
    """
    stats = {"grounded": 0, "dropped": 0, "code_inferred": 0}
    for c in claims:
        if c.layer != "why":
            continue
        kept = []
        for e in c.evidence:
            if not _is_artifact(e):
                kept.append(e)          # code hypothesis always survives
                continue
            body = artifact_body(conn, e.ref)
            excerpt = normalize_text(e.excerpt)
            if body is not None and excerpt and \
                    excerpt in normalize_text(body):
                kept.append(e)
                stats["grounded"] += 1
            else:
                stats["dropped"] += 1
        c.evidence = kept
        if any(_is_artifact(e) for e in kept):
            c.corroboration = "corroborated"
        else:
            c.corroboration = "code_inferred"
            c.confidence = min(c.confidence, CODE_INFERRED_MAX_CONFIDENCE)
            stats["code_inferred"] += 1
    return stats


WHY_SYNTH_SYSTEM = (
    "You recover why-layer requirement claims: the intent behind a "
    "requirement, the rationale for a design decision, the origin of a "
    "constraint value, or an accepted tradeoff. Code cannot settle these — "
    "they must come from the supplied artifacts (tickets, PR descriptions, "
    "review comments). Quote the artifact you rely on EXACTLY as it appears; "
    "a quote that is not verbatim will be discarded automatically. Never "
    "guess a rationale that the artifacts do not state. If the artifacts do "
    "not explain a claim, return nothing for it. Output only JSON."
)

WHY_SYNTH_PROMPT = """Feature: {feature}

Verified what-layer claims (these describe WHAT the code does):
{what_claims}

Artifacts recovered from the history of the code behind those claims:
{corpus}

Return a JSON array of why-layer claims. Each object:
{{"type": one of {types},
  "statement": a single sentence stating the intent, rationale, constraint
               origin, or tradeoff,
  "explains": [ids of the what-layer claims above that this explains],
  "evidence": [{{"ref": artifact ref exactly as shown in the "===" header
                        (e.g. "EMB-1", "pr:42", "pr_comment:c1"),
                 "excerpt": a VERBATIM quote from that artifact}}]}}
Do NOT cite code refs — the code behind each claim is attached
automatically. Every claim must explain at least one listed claim id and
cite at least one artifact. Output the JSON array only."""


def _format_what_claims(what_claims) -> str:
    lines = []
    for c in what_claims:
        refs = ", ".join(e.ref for e in c.evidence if e.kind == "code")
        lines.append(f"- {c.id} ({c.type}): {c.statement}  [code: {refs}]")
    return "\n".join(lines)


def _evidence_kind(ref: str) -> str | None:
    """Classify an artifact ref, or None if it is not one we accept."""
    ref = (ref or "").strip()
    if _PR_RE.match(ref):
        return "pr"
    if _PR_COMMENT_RE.match(ref):
        return "pr_comment"
    if _TICKET_RE.match(ref):
        return "ticket"
    return None


def _code_hypothesis(what_claim) -> list:
    """Copy a what-claim's primary code evidence onto a why-claim.

    Copied, never generated: the anchor was pinned by Spec B and the claim
    was verified in Pass 1, so the model gets no opportunity to invent a
    code ref. `replace` keeps the pin fields intact.
    """
    return [replace(e, role="primary") for e in what_claim.evidence
            if e.kind == "code" and e.role == "primary"]


def synthesize_why_claims(feature: str, what_claims: list, corpus: str,
                          ask) -> list:
    """One expensive call per feature/cluster; returns WHY-#### claims."""
    by_id = {c.id: c for c in what_claims}
    raw = ask(WHY_SYNTH_PROMPT.format(
        feature=feature, what_claims=_format_what_claims(what_claims),
        corpus=corpus, types=sorted(WHY_CLAIM_TYPES)))
    try:
        items = json.loads(_strip_fence(raw))
    except (ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    claims = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get("type") not in WHY_CLAIM_TYPES or not it.get("statement"):
            continue
        explains = [i for i in it.get("explains", []) if i in by_id]
        if not explains:
            continue        # no traceability and no code hypothesis
        evidence = []
        for e in it.get("evidence", []):
            if not isinstance(e, dict):
                continue
            kind = _evidence_kind(e.get("ref", ""))
            if kind is None:
                continue    # silently ignores any code ref the model emitted
            evidence.append(Evidence(kind=kind, role="corroborating",
                                     ref=e["ref"].strip(),
                                     excerpt=e.get("excerpt", "")))
        code = []
        seen_refs = set()
        for cid in explains:
            for ev in _code_hypothesis(by_id[cid]):
                if ev.ref not in seen_refs:
                    seen_refs.add(ev.ref)
                    code.append(ev)
        if not code:
            continue        # cannot exist without evidence
        claims.append(Claim(
            id=f"WHY-{len(claims) + 1:04d}", type=it["type"],
            statement=str(it["statement"]).strip(), feature=feature,
            layer="why", status="recovered", confidence=0.5,
            symbols=sorted({s for cid in explains
                            for s in by_id[cid].symbols}),
            evidence=code + evidence, explains=explains))
    return claims


WHY_VERIFY_SYSTEM = (
    "You are an adversarial verifier of why-layer claims. The quoted "
    "excerpts have already been proven to exist verbatim in the cited "
    "artifacts, so do NOT re-check whether the quote is real. Judge one "
    "thing: does the cited artifact actually STATE the rationale the claim "
    "asserts, or does it merely discuss the same area? An artifact that "
    "touches the topic without giving this reason does not support the "
    "claim. Reject a claim that generalises further than the artifact "
    "warrants, and reject a rationale the artifact only implies. Default to "
    "refuted when the artifact is ambiguous. Output only JSON."
)

WHY_VERIFY_PROMPT = """Why-claim ({type}): {statement}
Explains what-claims: {explains}
Cited artifacts: {refs}

Quoted excerpts (already verified verbatim):
{excerpts}

Full artifact corpus:
{corpus}

Return JSON: {{"supported": true|false, "confidence": 0.0-1.0,
"counter": "why it fails, or empty string"}}. Output the JSON only."""


def verify_why_claims(claims, corpus: str, ask) -> None:
    """Adversarially verify corroborated why-claims in place.

    Skips `code_inferred` claims entirely: with no artifact backing there is
    nothing to verify against, and the design forbids auto-verifying them.

    A failed call contests only its own claim, matching
    ``recover.verify_claims`` — an uncaught exception here would discard
    every previously verified claim, because saving happens after the loop.
    """
    for c in claims:
        if c.layer != "why" or c.corroboration != "corroborated":
            continue
        artifacts = [e for e in c.evidence if _is_artifact(e)]
        refs = ", ".join(e.ref for e in artifacts) or "(none)"
        excerpts = "\n".join(f"- [{e.ref}] {e.excerpt}" for e in artifacts)
        try:
            raw = ask(WHY_VERIFY_PROMPT.format(
                type=c.type, statement=c.statement,
                explains=", ".join(c.explains) or "(none)", refs=refs,
                excerpts=excerpts, corpus=corpus))
        except Exception as e:                      # noqa: BLE001
            c.status = "contested"
            c.confidence = 0.0
            c.counter_evidence = [f"why-verification call failed: {e}"]
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
