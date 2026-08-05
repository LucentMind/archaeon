from dataclasses import asdict, dataclass, field
from pathlib import Path
import hashlib

import yaml

# what-layer claim types (code is the sole authority for these)
CLAIM_TYPES = {
    "state_transition", "timing_budget", "threshold", "conditional_rule",
    "interaction_sequence", "invariant",
}

# why-layer claim types (code is a hypothesis; artifacts corroborate)
WHY_CLAIM_TYPES = {
    "intent", "rationale", "constraint_origin", "tradeoff",
}

# A why-claim with no surviving artifact evidence keeps its code hypothesis
# but is capped here and never auto-verified (design section 10.1).
CODE_INFERRED_MAX_CONFIDENCE = 0.4


@dataclass
class Evidence:
    kind: str            # code | ticket | pr | pr_comment
    ref: str             # e.g. "fault_handler.c:214" or "fault_handler.c:214-230"
    role: str = "primary"
    excerpt: str = ""
    # Spec B commit-pinned anchor (all None on legacy file:line-only evidence):
    commit_sha: str | None = None
    blob_sha: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    content_hash: str | None = None
    pin_status: str | None = None    # pinned | dirty | unpinnable


@dataclass
class Claim:
    id: str
    type: str
    statement: str
    feature: str = ""
    layer: str = "what"
    status: str = "recovered"    # recovered | machine_verified | contested
    confidence: float = 0.5
    symbols: list = field(default_factory=list)
    evidence: list = field(default_factory=list)      # list[Evidence]
    counter_evidence: list = field(default_factory=list)
    # why-layer only: corroborated | code_inferred. Orthogonal to `status`,
    # because the axes are independent — a code-inferred claim can still be
    # contested or expert-accepted without that erasing the fact that it was
    # never corroborated.
    corroboration: str | None = None
    explains: list = field(default_factory=list)   # what-claim ids

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Claim":
        return cls(
            id=d["id"], type=d["type"], statement=d["statement"],
            feature=d.get("feature", ""), layer=d.get("layer", "what"),
            status=d.get("status", "recovered"),
            confidence=d.get("confidence", 0.5),
            symbols=list(d.get("symbols", [])),
            evidence=[Evidence(**e) for e in d.get("evidence", [])],
            counter_evidence=list(d.get("counter_evidence", [])),
            corroboration=d.get("corroboration"),
            explains=list(d.get("explains", [])))


def save_claims(claims: list, out_dir: Path) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for c in claims:
        (out_dir / f"{c.id}.yaml").write_text(
            yaml.safe_dump(c.to_dict(), sort_keys=False, allow_unicode=True),
            encoding="utf-8")


def load_claims(out_dir: Path) -> list:
    return [Claim.from_dict(yaml.safe_load(p.read_text(encoding="utf-8")))
            for p in sorted(Path(out_dir).glob("*.yaml"))]


STATUSES = {
    "recovered", "machine_verified", "contested",
    "expert_accepted", "rejected",
}


class StaleClaimError(Exception):
    """Raised when a write targets a claim file that changed on disk."""


def claim_path(claims_dir, claim_id: str) -> Path:
    return Path(claims_dir) / f"{claim_id}.yaml"


def claim_version(path) -> str:
    """Content-hash version token for a claim file (stale-write detection)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_claim(claims_dir, claim_id: str, *, status: str | None = None,
               statement: str | None = None,
               expected_version: str | None = None) -> str:
    """Minimal-diff single-claim write.

    Mutates the raw YAML mapping (not the Claim dataclass) so key order and
    keys unknown to the dataclass are preserved. Re-dumps with sort_keys=False.
    Rejects a stale expected_version so a hand edit is never clobbered.
    """
    path = claim_path(claims_dir, claim_id)
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    if not path.is_file():
        raise FileNotFoundError(str(path))
    if expected_version is not None and claim_version(path) != expected_version:
        raise StaleClaimError(claim_id)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if status is not None:
        data["status"] = status
    if statement is not None:
        data["statement"] = statement
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8")
    return claim_version(path)
