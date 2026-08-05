import csv
from pathlib import Path

_TRUE = {"yes", "true", "1", "y", "correct"}


def load_labels(csv_path: Path) -> dict:
    """Read an expert-labeled CSV: header `claim_id,correct` where correct is
    yes/no. Maps claim id -> bool."""
    labels = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            labels[row["claim_id"].strip()] = \
                (row["correct"] or "").strip().lower() in _TRUE
    return labels


def evaluate_claims(claims: list, labels: dict) -> dict:
    """Precision per layer against expert labels. Only labeled claims count.

    Reports two numbers per layer: `precision` over *every* labeled claim
    (pre-verification — includes claims the adversarial verifier itself
    contested), and `verified_precision` over only the claims that reached
    `machine_verified` status (post-verification — what would actually
    surface to a user or the guardrail, per the design's "contested claims
    stay silent" rule). A synthesis batch can have real defects that the
    verifier reliably catches; the pre-verification number alone conflates
    synthesis quality with verification quality.

    Returns {layer: {n, correct, precision, verified_n, verified_correct,
    verified_precision, corroborated_n, corroborated_correct,
    corroborated_precision}}. The corroborated_* numbers count only claims
    whose rationale rests on a real artifact — the design's why-layer gate
    denominator, kept separate so a large code-inferred tail can neither
    dilute nor inflate it.
    """
    by_layer: dict = {}
    for c in claims:
        if c.id not in labels:
            continue
        s = by_layer.setdefault(c.layer, {"n": 0, "correct": 0,
                                          "verified_n": 0,
                                          "verified_correct": 0,
                                          "corroborated_n": 0,
                                          "corroborated_correct": 0})
        s["n"] += 1
        correct = labels[c.id]
        if correct:
            s["correct"] += 1
        if c.status == "machine_verified":
            s["verified_n"] += 1
            if correct:
                s["verified_correct"] += 1
        # Corroborated = rationale rests on a real artifact. Independent of
        # status, and the denominator the design's why-layer gate uses.
        if getattr(c, "corroboration", None) == "corroborated":
            s["corroborated_n"] += 1
            if correct:
                s["corroborated_correct"] += 1
    for s in by_layer.values():
        s["precision"] = s["correct"] / s["n"] if s["n"] else 0.0
        s["verified_precision"] = (s["verified_correct"] / s["verified_n"]
                                   if s["verified_n"] else 0.0)
        s["corroborated_precision"] = (
            s["corroborated_correct"] / s["corroborated_n"]
            if s["corroborated_n"] else 0.0)
    return by_layer
