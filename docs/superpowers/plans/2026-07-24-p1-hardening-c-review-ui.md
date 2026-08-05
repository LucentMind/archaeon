# P1 Hardening C — Review UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hand-edited `claim_labels.csv` review flow with a local web app that browses claims (component → cluster → claim card → evidence, colored by verification state) and persists accept/edit/reject decisions back into the git-tracked `claims/*.yaml`, producing minimal, reviewable diffs.

**Architecture:** A small FastAPI app serves a static vanilla-JS frontend and a JSON API over a directory of claim YAML files. All grouping, verification-state bucketing, queue scoring, and per-type *visual grammar* selection live in Python (server-side render-specs), so the JS only paints what the backend computes and every rule is unit-testable. Cluster metadata from Spec A (in the SQLite DB) is used when present and degrades to flat per-file grouping when absent. Writes go through one `save_claim` path that mutates the raw YAML mapping in place (preserving key order and untouched keys) and rejects stale writes via a content-hash version token.

**Tech Stack:** Python 3.12, FastAPI + uvicorn, `pyyaml` (existing), SQLite (existing, read-only here), vanilla JS + optional vendored Mermaid, `pytest` + FastAPI `TestClient`, Playwright (headless smoke test).

## Global Constraints

- **Python** `>=3.12` (matches `pyproject.toml`).
- **Local, single-reviewer tool.** No authentication, no multi-user server (spec §2 non-goals). Bind to `127.0.0.1` by default.
- **Reviewers accept/edit the *statement* and set status only.** Editing evidence/citations by hand is out of scope (spec §2); the API never mutates `evidence`, `symbols`, `confidence`, or `counter_evidence`.
- **All grammar/grouping/scoring logic lives in Python** and is unit-tested; the JS is a thin painter of server-produced render-specs (settled decision, spec §3.3).
- **Every claim type falls back to prose.** The UI must ship and function before all visual grammars exist (spec §3.3); an unmapped type renders as prose, never an error.
- **Graceful degradation is a tested path, never a crash** (spec §5): no Spec A cluster metadata → flat per-file grouping; malformed claim YAML → a broken-card entry carrying the parse error, the rest of the store still loads; write failure → surfaced to the UI, in-memory state not marked accepted.
- **Minimal, reviewable diffs.** `save_claim` loads the raw YAML mapping, mutates only the changed keys, and re-dumps with `sort_keys=False` so key order and untouched keys (including keys not on the `Claim` dataclass) are preserved (spec §3.1, §6).
- **Last-writer-wins is not allowed to silently clobber a hand edit** (spec §5). Every write carries a content-hash version token; a stale token is rejected with HTTP 409.
- **This track owns extending the status enum** (spec §3.1): add the review terminal states `expert_accepted` and `rejected` to the existing `recovered | machine_verified | contested`.
- **The claims YAML is the source of truth.** The DB is optional and read-only (cluster metadata + symbol fan-in only). Missing DB or missing Spec A tables must degrade, never raise.
- **The spike's CSV path stays untouched** (spec §7): do not modify `claims/claim_eval.py`, `claims-eval`, or the eval-label flow. This UI is the review surface, not the precision harness.
- **TDD, DRY, YAGNI, frequent commits.** One test-cycle per task; commit at the end of each task.

**Run tests with:** `uv run pytest <path> -v`. Run the full suite with `uv run pytest -q` before the final commit of each task.

---

## File Structure

**New files**
- `src/archeon/review/__init__.py` — new package marker (Task 2).
- `src/archeon/review/store.py` — claim loading, verification buckets, card shaping, component/cluster grouping (with cluster-join + flat fallback), and the impact×uncertainty queue (Tasks 2–4).
- `src/archeon/review/render.py` — per-claim-type visual-grammar render-specs with prose fallback (Task 5).
- `src/archeon/review/server.py` — FastAPI app factory, JSON API routes, POST writer, static mount (Task 6).
- `src/archeon/review/static/index.html`, `app.js`, `style.css` — frontend bundle (Task 8).
- Tests: `tests/test_save_claim.py`, `tests/test_review_store.py`, `tests/test_review_queue.py`, `tests/test_render_spec.py`, `tests/test_review_server.py`, `tests/test_cli_review.py`, `tests/test_review_smoke.py`.

**Modified files**
- `src/archeon/claims/schema.py` — add `STATUSES`, `StaleClaimError`, `claim_path`, `claim_version`, `save_claim` (Task 1). Existing `Claim`, `save_claims`, `load_claims` are untouched.
- `pyproject.toml` — add `fastapi`, `uvicorn` to dependencies; `playwright` to the dev group (Tasks 1 and 8).
- `src/archeon/cli.py` — new `review` command (Task 7).

**Unchanged (do not touch the logic)**
- `src/archeon/claims/recover.py`, `src/archeon/claims/claim_eval.py` — synthesis, verification, and the CSV precision harness stay exactly as-is (spec §7).
- `src/archeon/schema.sql` — no schema changes in this track; the DB is read-only here.

---

## Task 1: Status enum, single-claim minimal-diff writer, version token, web deps

Extends the claim schema module with the review terminal states and the one write path the API uses. The write mutates the raw YAML mapping (not the `Claim` dataclass) so unknown keys survive and diffs stay minimal.

**Files:**
- Modify: `src/archeon/claims/schema.py`
- Modify: `pyproject.toml:6-15` (dependencies)
- Test: `tests/test_save_claim.py`

**Interfaces:**
- Consumes: nothing (only the existing `claims/*.yaml` shape).
- Produces:
  - `STATUSES: set[str]` = `{"recovered", "machine_verified", "contested", "expert_accepted", "rejected"}`.
  - `class StaleClaimError(Exception)`.
  - `claim_path(claims_dir, claim_id: str) -> pathlib.Path` = `Path(claims_dir) / f"{claim_id}.yaml"`.
  - `claim_version(path) -> str` — hex SHA-256 of the file bytes.
  - `save_claim(claims_dir, claim_id: str, *, status: str | None = None, statement: str | None = None, expected_version: str | None = None) -> str` — validates `status ∈ STATUSES`, rejects a mismatched `expected_version` with `StaleClaimError`, mutates only the given keys of the raw mapping, re-dumps with `sort_keys=False`, and returns the new version. Raises `ValueError` on unknown status, `FileNotFoundError` if the claim file is absent.

- [ ] **Step 1: Add fastapi + uvicorn to dependencies**

Edit `pyproject.toml`, the `dependencies` list (`pyproject.toml:6-15`), adding two entries:

```toml
dependencies = [
    "click>=8.1",
    "requests>=2.32",
    "tree-sitter>=0.23",
    "tree-sitter-c>=0.23",
    "tree-sitter-cpp>=0.23",
    "libclang>=18",
    "claude-agent-sdk>=0.1",
    "pyyaml>=6",
    "fastapi>=0.115",
    "uvicorn>=0.30",
]
```

- [ ] **Step 2: Sync the environment**

Run: `uv sync`
Expected: resolves and installs `fastapi` and `uvicorn` with no error.

- [ ] **Step 3: Write the failing test**

Create `tests/test_save_claim.py`:

```python
import pytest
import yaml

from archeon.claims import schema


def _write(claims_dir, claim_id, extra=None):
    data = {
        "id": claim_id,
        "type": "conditional_rule",
        "statement": "original statement",
        "feature": "src/foo",
        "layer": "what",
        "status": "machine_verified",
        "confidence": 0.9,
        "symbols": ["A::b"],
        "evidence": [{"kind": "code", "ref": "foo.c:1-2", "role": "primary",
                      "excerpt": "return;"}],
        "counter_evidence": [],
    }
    if extra:
        data.update(extra)
    p = claims_dir / f"{claim_id}.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                 encoding="utf-8")
    return p


def test_save_claim_sets_status_and_returns_new_version(tmp_path):
    p = _write(tmp_path, "CLM-0001")
    v0 = schema.claim_version(p)
    v1 = schema.save_claim(tmp_path, "CLM-0001", status="expert_accepted",
                           expected_version=v0)
    reloaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert reloaded["status"] == "expert_accepted"
    assert v1 == schema.claim_version(p)
    assert v1 != v0


def test_save_claim_edits_statement(tmp_path):
    p = _write(tmp_path, "CLM-0002")
    schema.save_claim(tmp_path, "CLM-0002", status="expert_accepted",
                      statement="edited", expected_version=schema.claim_version(p))
    reloaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert reloaded["statement"] == "edited"
    assert reloaded["status"] == "expert_accepted"


def test_save_claim_preserves_key_order_and_unknown_keys(tmp_path):
    p = _write(tmp_path, "CLM-0003", extra={"provenance": "spike-run-7"})
    schema.save_claim(tmp_path, "CLM-0003", status="rejected",
                      expected_version=schema.claim_version(p))
    reloaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    # unknown key survives (not on the Claim dataclass)
    assert reloaded["provenance"] == "spike-run-7"
    # key order unchanged: id first, provenance still last
    keys = list(reloaded.keys())
    assert keys[0] == "id"
    assert keys[-1] == "provenance"
    # untouched fields intact
    assert reloaded["symbols"] == ["A::b"]
    assert reloaded["confidence"] == 0.9


def test_save_claim_rejects_stale_version(tmp_path):
    _write(tmp_path, "CLM-0004")
    with pytest.raises(schema.StaleClaimError):
        schema.save_claim(tmp_path, "CLM-0004", status="expert_accepted",
                          expected_version="deadbeef")


def test_save_claim_rejects_unknown_status(tmp_path):
    p = _write(tmp_path, "CLM-0005")
    with pytest.raises(ValueError):
        schema.save_claim(tmp_path, "CLM-0005", status="approved",
                          expected_version=schema.claim_version(p))


def test_save_claim_without_expected_version_skips_check(tmp_path):
    p = _write(tmp_path, "CLM-0006")
    schema.save_claim(tmp_path, "CLM-0006", status="rejected")
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["status"] == "rejected"
```

- [ ] **Step 4: Run it to verify it fails**

Run: `uv run pytest tests/test_save_claim.py -v`
Expected: FAIL with `AttributeError: module 'archeon.claims.schema' has no attribute 'save_claim'`.

- [ ] **Step 5: Implement the writer**

Edit `src/archeon/claims/schema.py`. Add `import hashlib` at the top (below the existing `from dataclasses import ...`), and append after `load_claims`:

```python
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
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_save_claim.py -v`
Expected: PASS (6 passed).

- [ ] **Step 7: Run the full suite to confirm nothing regressed**

Run: `uv run pytest -q`
Expected: all pre-existing tests still pass (the additions are new symbols; `Claim`/`save_claims`/`load_claims` are unchanged).

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock src/archeon/claims/schema.py tests/test_save_claim.py
git commit -m "feat(claims): review status enum + minimal-diff save_claim with version token"
```

---

## Task 2: Claim loading, verification buckets, card shaping, malformed handling

The store's low-level layer: read a claims directory into good claims + broken-card markers, map status to a verification bucket, and shape a claim mapping into the card dict the API returns. Written and tested before any grouping so the malformed-file path is proven early.

**Files:**
- Create: `src/archeon/review/__init__.py` (empty package marker)
- Create: `src/archeon/review/store.py`
- Test: `tests/test_review_store.py`

**Interfaces:**
- Consumes: `claim_version` (Task 1); `claims/*.yaml` on disk.
- Produces:
  - `verification_bucket(status: str) -> str` — one of `"verified"` (`machine_verified`, `expert_accepted`), `"contested"` (`contested`), `"rejected"` (`rejected`), `"unrecovered"` (`recovered` and any unknown).
  - `load_claim_files(claims_dir) -> tuple[list[dict], list[dict]]` — `(good, broken)`. Each good dict is the parsed mapping plus `_version` (its `claim_version`). Each broken dict is `{"id": <file stem>, "broken": True, "error": <str>, "_version": ""}`. A file that is not a mapping, or lacks an `id`, is broken; one broken file never aborts the load.
  - `claim_card(d: dict) -> dict` — normalized card: keys `id, type, statement, status, bucket, confidence, feature, symbols, evidence` (list of `{kind, ref, role, excerpt}`), `counter_evidence` (list of str), `version` (from `_version`). A broken dict passes through as `{id, broken, error, version}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_review_store.py`:

```python
import yaml

from archeon.review import store


def _write(claims_dir, claim_id, **over):
    data = {
        "id": claim_id, "type": "threshold", "statement": "s",
        "feature": "src/foo", "layer": "what", "status": "machine_verified",
        "confidence": 0.9, "symbols": ["A::b"],
        "evidence": [{"kind": "code", "ref": "foo.c:1-2", "role": "primary",
                      "excerpt": "x"}],
        "counter_evidence": [],
    }
    data.update(over)
    (claims_dir / f"{claim_id}.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_verification_bucket_mapping():
    assert store.verification_bucket("machine_verified") == "verified"
    assert store.verification_bucket("expert_accepted") == "verified"
    assert store.verification_bucket("contested") == "contested"
    assert store.verification_bucket("rejected") == "rejected"
    assert store.verification_bucket("recovered") == "unrecovered"
    assert store.verification_bucket("something_else") == "unrecovered"


def test_load_good_claims_carry_version(tmp_path):
    _write(tmp_path, "CLM-0001")
    good, broken = store.load_claim_files(tmp_path)
    assert len(good) == 1 and not broken
    assert good[0]["_version"]  # non-empty hash


def test_malformed_file_becomes_broken_card_not_a_crash(tmp_path):
    _write(tmp_path, "CLM-0001")
    (tmp_path / "CLM-0002.yaml").write_text(": not valid: yaml: [", encoding="utf-8")
    (tmp_path / "CLM-0003.yaml").write_text("just a string", encoding="utf-8")
    good, broken = store.load_claim_files(tmp_path)
    assert {g["id"] for g in good} == {"CLM-0001"}
    assert {b["id"] for b in broken} == {"CLM-0002", "CLM-0003"}
    assert all(b["broken"] and b["error"] for b in broken)


def test_claim_card_shape(tmp_path):
    _write(tmp_path, "CLM-0001", status="contested",
           counter_evidence=["verifier: symbol X not cited"])
    good, _ = store.load_claim_files(tmp_path)
    card = store.claim_card(good[0])
    assert card["id"] == "CLM-0001"
    assert card["bucket"] == "contested"
    assert card["evidence"][0]["ref"] == "foo.c:1-2"
    assert card["counter_evidence"] == ["verifier: symbol X not cited"]
    assert card["version"]


def test_claim_card_passes_broken_through(tmp_path):
    card = store.claim_card({"id": "CLM-9", "broken": True, "error": "boom",
                             "_version": ""})
    assert card["broken"] and card["error"] == "boom" and card["id"] == "CLM-9"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_review_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'archeon.review'`.

- [ ] **Step 3: Create the package marker**

Create `src/archeon/review/__init__.py` (empty file).

- [ ] **Step 4: Implement the loader, buckets, and card shaping**

Create `src/archeon/review/store.py`:

```python
from pathlib import Path

import yaml

from archeon.claims.schema import claim_version

_BUCKET = {
    "machine_verified": "verified",
    "expert_accepted": "verified",
    "contested": "contested",
    "rejected": "rejected",
    "recovered": "unrecovered",
}


def verification_bucket(status: str) -> str:
    return _BUCKET.get(status, "unrecovered")


def load_claim_files(claims_dir):
    """Read a claims directory into (good, broken).

    A malformed or non-mapping file becomes a broken-card marker instead of
    aborting the load, so one bad file never hides the rest of the store.
    """
    good, broken = [], []
    for p in sorted(Path(claims_dir).glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "id" not in data:
                raise ValueError("claim file is not a mapping with an id")
            data["_version"] = claim_version(p)
            good.append(data)
        except Exception as e:  # noqa: BLE001 - any parse failure is a broken card
            broken.append({"id": p.stem, "broken": True, "error": str(e),
                           "_version": ""})
    return good, broken


def claim_card(d: dict) -> dict:
    if d.get("broken"):
        return {"id": d["id"], "broken": True, "error": d.get("error", ""),
                "version": d.get("_version", "")}
    status = d.get("status", "recovered")
    return {
        "id": d["id"],
        "type": d.get("type", ""),
        "statement": d.get("statement", ""),
        "status": status,
        "bucket": verification_bucket(status),
        "confidence": d.get("confidence", 0.5),
        "feature": d.get("feature", ""),
        "symbols": list(d.get("symbols", [])),
        "evidence": [
            {"kind": e.get("kind", ""), "ref": e.get("ref", ""),
             "role": e.get("role", "primary"), "excerpt": e.get("excerpt", "")}
            for e in d.get("evidence", []) if isinstance(e, dict)],
        "counter_evidence": [str(c) for c in d.get("counter_evidence", [])],
        "version": d.get("_version", ""),
    }
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_review_store.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add src/archeon/review/__init__.py src/archeon/review/store.py tests/test_review_store.py
git commit -m "feat(review): claim loader, verification buckets, card shaping, broken-card handling"
```

---

## Task 3: Component/cluster grouping (cluster-join with flat fallback)

Adds the treemap/drill data: components (grouped by the claim's `feature`) with per-bucket counts, and clusters within a component. Clusters come from Spec A's DB metadata when present (claim symbol names → `symbols.id` → `cluster_members` → `clusters.label`) and fall back to a flat per-source-file grouping when the DB is absent or has no cluster tables. Both paths return the same shape.

**Files:**
- Modify: `src/archeon/review/store.py`
- Test: extend `tests/test_review_store.py`

**Interfaces:**
- Consumes: `load_claim_files`, `verification_bucket`, `claim_card` (Task 2); an optional `sqlite3.Connection` (the existing `archeon.db.connect` shape, read-only).
- Produces:
  - `components(claims_dir) -> list[dict]` — one dict per distinct `feature` (broken claims group under `"(unparsed)"`): keys `component, verified, contested, unrecovered, rejected, broken, total`, sorted by `-total`.
  - `clusters(claims_dir, component, conn=None) -> list[dict]` — one dict per cluster within `component`: keys `cluster` (label), `clustered` (bool, True iff from DB), `verified, contested, unrecovered, rejected, broken, total`, sorted by `-total`.
  - `claims_in(claims_dir, *, component=None, cluster=None, conn=None) -> list[dict]` — `claim_card`s (good and broken) filtered to the given `component` and, if given, `cluster` (using the same cluster keying as `clusters`). Broken cards are grouped under `component="(unparsed)"`, `cluster="(unfiled)"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_review_store.py`:

```python
from archeon.db import connect


def test_components_count_by_bucket(tmp_path):
    _write(tmp_path, "CLM-0001", feature="src/foo", status="machine_verified")
    _write(tmp_path, "CLM-0002", feature="src/foo", status="contested")
    _write(tmp_path, "CLM-0003", feature="src/bar", status="recovered")
    comps = {c["component"]: c for c in store.components(tmp_path)}
    assert comps["src/foo"]["verified"] == 1
    assert comps["src/foo"]["contested"] == 1
    assert comps["src/foo"]["total"] == 2
    assert comps["src/bar"]["unrecovered"] == 1


def test_broken_claims_group_under_unparsed(tmp_path):
    (tmp_path / "CLM-0002.yaml").write_text("nope: [", encoding="utf-8")
    comps = {c["component"]: c for c in store.components(tmp_path)}
    assert comps["(unparsed)"]["broken"] == 1


def test_clusters_flat_fallback_without_db(tmp_path):
    _write(tmp_path, "CLM-0001", feature="src/foo",
           evidence=[{"kind": "code", "ref": "a.c:1-2"}])
    _write(tmp_path, "CLM-0002", feature="src/foo",
           evidence=[{"kind": "code", "ref": "b.c:3-4"}])
    cl = store.clusters(tmp_path, "src/foo", conn=None)
    labels = {c["cluster"] for c in cl}
    assert labels == {"a.c", "b.c"}
    assert all(c["clustered"] is False for c in cl)


def test_clusters_use_db_metadata_when_present(tmp_path):
    _write(tmp_path, "CLM-0001", feature="src/foo", symbols=["do_thing"])
    conn = connect(tmp_path / "e.db")
    conn.execute(
        "INSERT INTO symbols(name, kind, path, line, end_line, signature, "
        "source) VALUES ('do_thing','function','src/a.c',1,2,'','tree-sitter')")
    sid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    cid = conn.execute(
        "INSERT INTO clusters(component, label, candidate_types) "
        "VALUES ('src/foo','thermal path','threshold')").lastrowid
    conn.execute("INSERT INTO cluster_members(cluster_id, symbol_id) "
                 "VALUES (?, ?)", (cid, sid))
    conn.commit()
    cl = store.clusters(tmp_path, "src/foo", conn=conn)
    assert cl[0]["cluster"] == "thermal path" and cl[0]["clustered"] is True


def test_claims_in_filters_by_component_and_cluster(tmp_path):
    _write(tmp_path, "CLM-0001", feature="src/foo",
           evidence=[{"kind": "code", "ref": "a.c:1-2"}])
    _write(tmp_path, "CLM-0002", feature="src/foo",
           evidence=[{"kind": "code", "ref": "b.c:3-4"}])
    cards = store.claims_in(tmp_path, component="src/foo", cluster="a.c")
    assert [c["id"] for c in cards] == ["CLM-0001"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_review_store.py -v`
Expected: FAIL with `AttributeError: module 'archeon.review.store' has no attribute 'components'`.

- [ ] **Step 3: Implement grouping**

Append to `src/archeon/review/store.py` (add `import sqlite3` to the imports at the top):

```python
_ZERO = ("verified", "contested", "unrecovered", "rejected", "broken")


def _component_of(d: dict) -> str:
    return d.get("feature") or "(unparsed)"


def _flat_cluster(d: dict) -> str:
    for e in d.get("evidence", []) or []:
        ref = e.get("ref") if isinstance(e, dict) else None
        if ref:
            return ref.split(":")[0]
    return "(unfiled)"


def _db_cluster_label(conn, symbols):
    """Cluster label for a claim's first symbol that maps into cluster_members.

    Claims name symbols as strings; Spec A keys clusters by symbols.id. Join by
    name. Returns None when the DB lacks the tables or nothing matches (both
    degrade to flat grouping).
    """
    for name in symbols:
        try:
            row = conn.execute(
                "SELECT c.id AS cid, c.label AS label FROM symbols s "
                "JOIN cluster_members m ON m.symbol_id = s.id "
                "JOIN clusters c ON c.id = m.cluster_id "
                "WHERE s.name = ? LIMIT 1", (name,)).fetchone()
        except sqlite3.OperationalError:
            return None
        if row:
            return row["label"] or f"cluster-{row['cid']}"
    return None


def _cluster_key(d: dict, conn):
    if conn is not None:
        label = _db_cluster_label(conn, d.get("symbols", []))
        if label is not None:
            return label, True
    return _flat_cluster(d), False


def _blank(key_name: str, key_val: str) -> dict:
    row = {key_name: key_val, "total": 0}
    row.update({b: 0 for b in _ZERO})
    return row


def _tally(row: dict, d: dict) -> None:
    row["total"] += 1
    if d.get("broken"):
        row["broken"] += 1
    else:
        row[verification_bucket(d.get("status", "recovered"))] += 1


def components(claims_dir) -> list:
    good, broken = load_claim_files(claims_dir)
    out: dict[str, dict] = {}
    for d in good + broken:
        key = _component_of(d)
        out.setdefault(key, _blank("component", key))
        _tally(out[key], d)
    return sorted(out.values(), key=lambda c: -c["total"])


def clusters(claims_dir, component, conn=None) -> list:
    good, broken = load_claim_files(claims_dir)
    out: dict[str, dict] = {}
    for d in good:
        if _component_of(d) != component:
            continue
        key, clustered = _cluster_key(d, conn)
        row = out.setdefault(key, _blank("cluster", key))
        row["clustered"] = clustered
        _tally(row, d)
    for d in broken:
        if _component_of(d) == component:
            row = out.setdefault("(unfiled)", _blank("cluster", "(unfiled)"))
            row.setdefault("clustered", False)
            _tally(row, d)
    return sorted(out.values(), key=lambda c: -c["total"])


def claims_in(claims_dir, *, component=None, cluster=None, conn=None) -> list:
    good, broken = load_claim_files(claims_dir)
    cards = []
    for d in good:
        if component is not None and _component_of(d) != component:
            continue
        if cluster is not None and _cluster_key(d, conn)[0] != cluster:
            continue
        cards.append(claim_card(d))
    for d in broken:
        if component not in (None, "(unparsed)"):
            continue
        if cluster not in (None, "(unfiled)"):
            continue
        cards.append(claim_card(d))
    return cards
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_review_store.py -v`
Expected: PASS (all store tests, 10 passed).

- [ ] **Step 5: Commit**

```bash
git add src/archeon/review/store.py tests/test_review_store.py
git commit -m "feat(review): component/cluster grouping with Spec A cluster-join and flat fallback"
```

---

## Task 4: Impact × uncertainty queue

The queue surfaces the reviewer's remaining load, ordered so contested claims come first and then by `impact × uncertainty`. `uncertainty = 1 - confidence`; impact is symbol fan-in from the DB when available, else the claim's symbol count (spec §8 starting proxy). Already-reviewed terminal states (`expert_accepted`, `rejected`) drop out of the queue.

**Files:**
- Modify: `src/archeon/review/store.py`
- Test: `tests/test_review_queue.py`

**Interfaces:**
- Consumes: `load_claim_files`, `claim_card` (Task 2); optional `sqlite3.Connection`.
- Produces:
  - `queue(claims_dir, conn=None) -> list[dict]` — `claim_card`s for non-terminal claims, each with added keys `impact: int`, `uncertainty: float`, `score: float`. Sorted with contested first, then `-score`. Broken cards are excluded.

- [ ] **Step 1: Write the failing test**

Create `tests/test_review_queue.py`:

```python
import yaml

from archeon.review import store
from archeon.db import connect


def _write(claims_dir, claim_id, **over):
    data = {"id": claim_id, "type": "threshold", "statement": "s",
            "feature": "src/foo", "status": "machine_verified",
            "confidence": 0.9, "symbols": ["A::b"], "evidence": [],
            "counter_evidence": []}
    data.update(over)
    (claims_dir / f"{claim_id}.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_queue_contested_first_then_score(tmp_path):
    _write(tmp_path, "HI", status="machine_verified", confidence=0.1,
           symbols=["a", "b", "c"])           # high score, not contested
    _write(tmp_path, "LO", status="machine_verified", confidence=0.95,
           symbols=["a"])                       # low score
    _write(tmp_path, "CON", status="contested", confidence=0.8, symbols=["a"])
    ids = [c["id"] for c in store.queue(tmp_path)]
    assert ids[0] == "CON"          # contested surfaced first
    assert ids[1:] == ["HI", "LO"]  # then by impact*uncertainty desc


def test_queue_excludes_terminal_states(tmp_path):
    _write(tmp_path, "DONE", status="expert_accepted")
    _write(tmp_path, "NO", status="rejected")
    _write(tmp_path, "OPEN", status="machine_verified")
    assert [c["id"] for c in store.queue(tmp_path)] == ["OPEN"]


def test_queue_uses_db_fan_in_for_impact(tmp_path):
    _write(tmp_path, "CLM-0001", confidence=0.5, symbols=["callee"])
    conn = connect(tmp_path / "e.db")
    conn.executescript(
        "INSERT INTO symbols(name,kind,path,line,end_line,signature,source) "
        "VALUES ('callee','function','a.c',1,2,'','ts');"
        "INSERT INTO symbols(name,kind,path,line,end_line,signature,source) "
        "VALUES ('c1','function','a.c',3,4,'','ts');"
        "INSERT INTO symbols(name,kind,path,line,end_line,signature,source) "
        "VALUES ('c2','function','a.c',5,6,'','ts');")
    callee = conn.execute("SELECT id FROM symbols WHERE name='callee'").fetchone()["id"]
    c1 = conn.execute("SELECT id FROM symbols WHERE name='c1'").fetchone()["id"]
    c2 = conn.execute("SELECT id FROM symbols WHERE name='c2'").fetchone()["id"]
    conn.executemany(
        "INSERT INTO symbol_edges(src_id,dst_id,kind,weight) "
        "VALUES (?,?,'references',1.0)", [(c1, callee), (c2, callee)])
    conn.commit()
    q = store.queue(tmp_path, conn=conn)
    assert q[0]["impact"] == 2  # two callers reference `callee`
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_review_queue.py -v`
Expected: FAIL with `AttributeError: module 'archeon.review.store' has no attribute 'queue'`.

- [ ] **Step 3: Implement the queue**

Append to `src/archeon/review/store.py`:

```python
_TERMINAL = {"expert_accepted", "rejected"}


def _impact(d: dict, conn) -> int:
    symbols = d.get("symbols", [])
    if conn is not None:
        try:
            total = 0
            for name in symbols:
                r = conn.execute(
                    "SELECT COUNT(*) AS c FROM symbol_edges e "
                    "JOIN symbols s ON s.id = e.dst_id "
                    "WHERE s.name = ?", (name,)).fetchone()
                total += r["c"]
            if total:
                return total
        except sqlite3.OperationalError:
            pass
    return max(len(symbols), 1)


def queue(claims_dir, conn=None) -> list:
    good, _ = load_claim_files(claims_dir)
    items = []
    for d in good:
        if d.get("status", "recovered") in _TERMINAL:
            continue
        card = claim_card(d)
        card["uncertainty"] = 1.0 - float(d.get("confidence", 0.5))
        card["impact"] = _impact(d, conn)
        card["score"] = card["impact"] * card["uncertainty"]
        items.append(card)
    return sorted(items, key=lambda c: (c["bucket"] != "contested", -c["score"]))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_review_queue.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/archeon/review/store.py tests/test_review_queue.py
git commit -m "feat(review): impact x uncertainty review queue with contested surfacing"
```

---

## Task 5: Per-type visual-grammar render-specs (prose fallback)

The server-side render layer. `render_spec(card)` returns a tagged union the frontend paints: a Mermaid diagram (`state`/`sequence`), a parameter table, or prose. Grammars are deterministic scaffolds over the fields a claim already has (`type`, `symbols`, `statement`) — no structured transition/threshold fields exist in the schema yet, so the grammars shape existing data per type and every unmapped type falls back to prose (spec §3.3, fallback-first).

**Files:**
- Create: `src/archeon/review/render.py`
- Test: `tests/test_render_spec.py`

**Interfaces:**
- Consumes: a `claim_card` dict (Task 2).
- Produces:
  - `render_spec(card: dict) -> dict` — one of:
    - `{"mode": "mermaid", "kind": "state"|"sequence", "src": <str>, "caption": <statement>}`
    - `{"mode": "table", "columns": ["field", "value"], "rows": list[list[str]]}`
    - `{"mode": "prose", "text": <statement>}`
  - Mapping: `state_transition → mermaid/state`; `interaction_sequence → mermaid/sequence`; `threshold → table`; every other type (`conditional_rule`, `invariant`, `timing_budget`, and any unknown) → prose.

- [ ] **Step 1: Write the failing test**

Create `tests/test_render_spec.py`:

```python
from archeon.review import render


def _card(type_, symbols, statement="does a thing"):
    return {"id": "X", "type": type_, "symbols": symbols,
            "statement": statement}


def test_state_transition_is_mermaid_state_with_two_nodes():
    spec = render.render_spec(_card("state_transition", ["Idle", "Active"]))
    assert spec["mode"] == "mermaid" and spec["kind"] == "state"
    assert "Idle --> Active" in spec["src"]
    assert spec["caption"] == "does a thing"


def test_interaction_sequence_is_mermaid_sequence():
    spec = render.render_spec(_card("interaction_sequence", ["Caller", "Callee"]))
    assert spec["mode"] == "mermaid" and spec["kind"] == "sequence"
    assert "sequenceDiagram" in spec["src"]
    assert "Caller" in spec["src"] and "Callee" in spec["src"]


def test_threshold_is_table_of_symbols_and_statement():
    spec = render.render_spec(_card("threshold", ["MAX_TEMP"], "temp <= 90"))
    assert spec["mode"] == "table"
    flat = [cell for row in spec["rows"] for cell in row]
    assert "MAX_TEMP" in flat and "temp <= 90" in flat


def test_unmapped_types_fall_back_to_prose():
    for t in ("conditional_rule", "invariant", "timing_budget", "mystery", ""):
        spec = render.render_spec(_card(t, ["A"], "the rule"))
        assert spec == {"mode": "prose", "text": "the rule"}


def test_mermaid_node_ids_are_sanitized():
    spec = render.render_spec(_card("state_transition", ["A::b()", "C d"]))
    # non-alnum chars replaced so Mermaid parses the node ids
    assert "::" not in spec["src"] and "()" not in spec["src"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_render_spec.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'archeon.review.render'`.

- [ ] **Step 3: Implement the render specs**

Create `src/archeon/review/render.py`:

```python
def _node(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in name).strip("_")
    return (cleaned[:40] or "n")


def _state_src(card: dict) -> str:
    syms = card.get("symbols", [])
    lines = ["stateDiagram-v2"]
    if len(syms) >= 2:
        lines.append(f"    {_node(syms[0])} --> {_node(syms[1])}")
    elif syms:
        lines.append(f"    [*] --> {_node(syms[0])}")
    else:
        lines.append("    [*] --> Unknown")
    return "\n".join(lines)


def _sequence_src(card: dict) -> str:
    parts = [_node(s) for s in (card.get("symbols") or ["actor"])[:6]]
    lines = ["sequenceDiagram"]
    lines += [f"    participant {p}" for p in parts]
    if len(parts) == 1:
        lines.append(f"    {parts[0]}->>{parts[0]}: self")
    else:
        lines += [f"    {a}->>{b}: call" for a, b in zip(parts, parts[1:])]
    return "\n".join(lines)


def _threshold_rows(card: dict) -> list:
    rows = [["symbol", s] for s in card.get("symbols", [])]
    rows.append(["statement", card.get("statement", "")])
    return rows


def render_spec(card: dict) -> dict:
    t = card.get("type", "")
    if t == "state_transition":
        return {"mode": "mermaid", "kind": "state", "src": _state_src(card),
                "caption": card.get("statement", "")}
    if t == "interaction_sequence":
        return {"mode": "mermaid", "kind": "sequence", "src": _sequence_src(card),
                "caption": card.get("statement", "")}
    if t == "threshold":
        return {"mode": "table", "columns": ["field", "value"],
                "rows": _threshold_rows(card)}
    return {"mode": "prose", "text": card.get("statement", "")}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_render_spec.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/archeon/review/render.py tests/test_render_spec.py
git commit -m "feat(review): per-type visual-grammar render-specs with prose fallback"
```

---

## Task 6: FastAPI app — JSON API, POST writer, static mount

Assembles the store, render, and `save_claim` behind a JSON API and mounts the static frontend. The POST route maps review actions to statuses, enforces the version token (409 on stale), and surfaces write failures (spec §5).

**Files:**
- Create: `src/archeon/review/server.py`
- Test: `tests/test_review_server.py`

**Interfaces:**
- Consumes: `store.components/clusters/claims_in/queue` (Tasks 2–4); `render.render_spec` (Task 5); `schema.save_claim`, `schema.StaleClaimError` (Task 1); `archeon.db.connect`.
- Produces:
  - `create_app(claims_dir, db=None) -> fastapi.FastAPI`. Routes:
    - `GET /api/components` → `store.components`.
    - `GET /api/clusters?component=` → `store.clusters`.
    - `GET /api/claims?component=&cluster=` → `store.claims_in`, each non-broken card gets a `render` key.
    - `GET /api/queue` → `store.queue`, each card gets a `render` key.
    - `POST /api/claims/{claim_id}` with body `{status, statement?, version}` → `save_claim`; `409` on `StaleClaimError`, `400` on unknown status, `404` if the file is gone, `500` on write failure; on success `{ok: true, version}`.
    - Static files mounted at `/` (serves `index.html`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_review_server.py`:

```python
import yaml
from fastapi.testclient import TestClient

from archeon.review.server import create_app


def _write(claims_dir, claim_id, **over):
    data = {"id": claim_id, "type": "threshold", "statement": "s",
            "feature": "src/foo", "status": "machine_verified",
            "confidence": 0.9, "symbols": ["A::b"],
            "evidence": [{"kind": "code", "ref": "a.c:1-2"}],
            "counter_evidence": []}
    data.update(over)
    (claims_dir / f"{claim_id}.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _client(tmp_path):
    return TestClient(create_app(tmp_path))


def test_components_endpoint(tmp_path):
    _write(tmp_path, "CLM-0001")
    r = _client(tmp_path).get("/api/components")
    assert r.status_code == 200
    assert r.json()[0]["component"] == "src/foo"


def test_claims_endpoint_attaches_render_spec(tmp_path):
    _write(tmp_path, "CLM-0001", type="threshold")
    cards = _client(tmp_path).get("/api/claims", params={"component": "src/foo"}).json()
    assert cards[0]["render"]["mode"] == "table"


def test_post_accept_writes_yaml_and_returns_new_version(tmp_path):
    _write(tmp_path, "CLM-0001")
    c = _client(tmp_path)
    card = c.get("/api/claims", params={"component": "src/foo"}).json()[0]
    r = c.post(f"/api/claims/CLM-0001",
               json={"status": "expert_accepted", "version": card["version"]})
    assert r.status_code == 200 and r.json()["ok"] is True
    on_disk = yaml.safe_load((tmp_path / "CLM-0001.yaml").read_text())
    assert on_disk["status"] == "expert_accepted"
    assert r.json()["version"] != card["version"]


def test_post_edit_sets_statement(tmp_path):
    _write(tmp_path, "CLM-0001")
    c = _client(tmp_path)
    card = c.get("/api/claims", params={"component": "src/foo"}).json()[0]
    c.post("/api/claims/CLM-0001", json={"status": "expert_accepted",
                                         "statement": "edited",
                                         "version": card["version"]})
    on_disk = yaml.safe_load((tmp_path / "CLM-0001.yaml").read_text())
    assert on_disk["statement"] == "edited"


def test_post_stale_version_is_409(tmp_path):
    _write(tmp_path, "CLM-0001")
    r = _client(tmp_path).post("/api/claims/CLM-0001",
                               json={"status": "rejected", "version": "stale"})
    assert r.status_code == 409


def test_post_unknown_status_is_400(tmp_path):
    _write(tmp_path, "CLM-0001")
    c = _client(tmp_path)
    card = c.get("/api/claims", params={"component": "src/foo"}).json()[0]
    r = c.post("/api/claims/CLM-0001",
               json={"status": "approved", "version": card["version"]})
    assert r.status_code == 400


def test_post_missing_claim_is_404(tmp_path):
    r = _client(tmp_path).post("/api/claims/NOPE",
                               json={"status": "rejected", "version": "x"})
    assert r.status_code == 404


def test_index_html_served_at_root(tmp_path):
    r = _client(tmp_path).get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_review_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'archeon.review.server'` (the static-file test also requires Task 8's assets; it will pass once `index.html` exists — see Step 4 note).

- [ ] **Step 3: Add a placeholder static directory so the mount has a target**

Create `src/archeon/review/static/index.html` with a minimal placeholder (Task 8 replaces it with the real app):

```html
<!doctype html>
<title>Archeon Review</title>
<div id="app">loading…</div>
```

- [ ] **Step 4: Implement the app factory**

Create `src/archeon/review/server.py`:

```python
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from archeon.claims.schema import StaleClaimError, save_claim
from archeon.db import connect
from archeon.review import render, store


class ReviewIn(BaseModel):
    status: str
    version: str
    statement: str | None = None


def create_app(claims_dir, db=None) -> FastAPI:
    claims_dir = Path(claims_dir)
    conn = connect(db) if db else None
    app = FastAPI(title="Archeon Review")

    @app.get("/api/components")
    def components():
        return store.components(claims_dir)

    @app.get("/api/clusters")
    def clusters(component: str):
        return store.clusters(claims_dir, component, conn=conn)

    @app.get("/api/claims")
    def claims(component: str | None = None, cluster: str | None = None):
        cards = store.claims_in(claims_dir, component=component,
                                cluster=cluster, conn=conn)
        for c in cards:
            if not c.get("broken"):
                c["render"] = render.render_spec(c)
        return cards

    @app.get("/api/queue")
    def review_queue():
        cards = store.queue(claims_dir, conn=conn)
        for c in cards:
            c["render"] = render.render_spec(c)
        return cards

    @app.post("/api/claims/{claim_id}")
    def review(claim_id: str, body: ReviewIn):
        try:
            version = save_claim(claims_dir, claim_id, status=body.status,
                                 statement=body.statement,
                                 expected_version=body.version)
        except StaleClaimError:
            raise HTTPException(status_code=409,
                                detail="claim changed on disk; reload")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="claim not found")
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"write failed: {e}")
        return {"ok": True, "version": version}

    static = Path(__file__).parent / "static"
    app.mount("/", StaticFiles(directory=str(static), html=True), name="static")
    return app
```

Note: the `/api/*` routes are registered before the `/` static mount, so Starlette matches them first; the mount serves `index.html` and other assets.

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_review_server.py -v`
Expected: PASS (8 passed).

- [ ] **Step 6: Commit**

```bash
git add src/archeon/review/server.py src/archeon/review/static/index.html tests/test_review_server.py
git commit -m "feat(review): FastAPI app - JSON API, version-checked POST writer, static mount"
```

---

## Task 7: CLI `review` command

Wires the app into the CLI. `--config` is optional; when given, the component's DB is opened read-only to enable cluster metadata and fan-in impact — otherwise the tool runs fully on the claims directory alone (spec §5 degradation).

**Files:**
- Modify: `src/archeon/cli.py`
- Test: `tests/test_cli_review.py`

**Interfaces:**
- Consumes: `create_app` (Task 6); `config_mod.load` (existing).
- Produces: `archeon review --claims <dir> [--config <toml>] [--host 127.0.0.1] [--port 8000]` — builds the app and calls `uvicorn.run(app, host, port)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_review.py`:

```python
from click.testing import CliRunner

from archeon import cli


def test_review_builds_app_and_runs_uvicorn(tmp_path, monkeypatch):
    (tmp_path / "CLM-0001.yaml").write_text(
        "id: CLM-0001\ntype: threshold\nstatement: s\nfeature: src/foo\n"
        "status: recovered\nconfidence: 0.5\nsymbols: []\nevidence: []\n"
        "counter_evidence: []\n", encoding="utf-8")
    captured = {}

    def fake_run(app, host, port):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    import uvicorn
    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = CliRunner().invoke(
        cli.main, ["review", "--claims", str(tmp_path), "--port", "9123"])
    assert result.exit_code == 0, result.output
    assert captured["port"] == 9123
    assert captured["host"] == "127.0.0.1"
    # the app exposes the review API
    assert any(getattr(r, "path", "") == "/api/components"
               for r in captured["app"].routes)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_cli_review.py -v`
Expected: FAIL with a `click` "No such command 'review'" error (exit code non-zero).

- [ ] **Step 3: Implement the command**

Edit `src/archeon/cli.py`. Add this command (place it after `cli_synthesize`, before `cli_claims_eval`):

```python
@main.command("review")
@click.option("--claims", "claims_dir", default="claims", show_default=True,
              help="directory of claim YAML files to review")
@click.option("--config", "config_path", default=None,
              help="optional archeon.toml; enables cluster + fan-in metadata")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
def cli_review(claims_dir, config_path, host, port):
    """Local review UI: browse + accept/edit/reject claims back into YAML."""
    import uvicorn

    from archeon.review.server import create_app
    db = None
    if config_path:
        cfg = config_mod.load(Path(config_path))
        db = cfg["component"]["db"]
    app = create_app(claims_dir, db=db)
    click.echo(f"review UI on http://{host}:{port}  (claims: {claims_dir})")
    uvicorn.run(app, host=host, port=port)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_cli_review.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/archeon/cli.py tests/test_cli_review.py
git commit -m "feat(cli): archeon review command launches the local review UI"
```

---

## Task 8: Frontend bundle + Playwright headless smoke test

The vanilla-JS frontend: a component treemap colored by verification state, drill to clusters → claim cards → evidence, per-card render-spec painting (Mermaid/table/prose), single-keystroke accept/edit/reject that POSTs with the version token, and a queue tab. Then a Playwright smoke test drives the real browser → API → YAML round-trip. The visual grammar and grouping logic is already tested in Python (Tasks 3–5); this task's automated gate is the browser round-trip.

Use the **webapp-testing** skill for the Playwright test (browser install, launching the app, and driving the page).

**Files:**
- Modify: `src/archeon/review/static/index.html` (replace the placeholder)
- Create: `src/archeon/review/static/style.css`
- Create: `src/archeon/review/static/app.js`
- Modify: `pyproject.toml` (dev group: add `playwright`)
- Test: `tests/test_review_smoke.py`

**Interfaces:**
- Consumes: the JSON API (Task 6).
- Produces: a static bundle served at `/`. Keybindings while a card is focused: `a` → accept (`expert_accepted`), `r` → reject (`rejected`), `e` → edit statement then accept. Each POST sends the focused card's `version`; a `409` shows a reload prompt.

- [ ] **Step 1: Add playwright to the dev dependency group**

Edit `pyproject.toml`:

```toml
[dependency-groups]
dev = ["pytest>=8", "playwright>=1.4"]
```

- [ ] **Step 2: Sync and install the browser**

Run: `uv sync && uv run playwright install chromium`
Expected: Playwright and a Chromium build install with no error.

- [ ] **Step 3: Write the frontend — styles**

Create `src/archeon/review/static/style.css`:

```css
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.4 system-ui, sans-serif; color: #1a1a1a; }
header { padding: 8px 14px; background: #111; color: #fff; display: flex; gap: 16px; align-items: baseline; }
header button { background: none; border: 1px solid #555; color: #ddd; padding: 4px 10px; cursor: pointer; }
header button.active { background: #fff; color: #111; }
#main { display: flex; }
#nav { width: 320px; border-right: 1px solid #ddd; min-height: 90vh; padding: 8px; }
#cards { flex: 1; padding: 12px; }
.tile { display: block; width: 100%; text-align: left; border: 1px solid #ccc; margin: 4px 0; padding: 8px; cursor: pointer; background: #fafafa; }
.tile .bar { height: 6px; display: flex; margin-top: 6px; }
.bar .verified { background: #2e7d32; }
.bar .contested { background: #ef6c00; }
.bar .unrecovered { background: #9e9e9e; }
.bar .rejected { background: #b71c1c; }
.card { border: 1px solid #ccc; border-left: 6px solid #9e9e9e; padding: 10px; margin: 8px 0; }
.card.focus { outline: 2px solid #1565c0; }
.card.verified { border-left-color: #2e7d32; }
.card.contested { border-left-color: #ef6c00; }
.card.rejected { border-left-color: #b71c1c; }
.card.broken { border-left-color: #000; background: #fff3f3; }
.meta { color: #666; font-size: 12px; }
.evidence { background: #f4f4f4; padding: 6px; margin-top: 6px; white-space: pre-wrap; font-family: ui-monospace, monospace; font-size: 12px; }
.counter { color: #b71c1c; margin-top: 6px; }
table.grammar { border-collapse: collapse; margin-top: 6px; }
table.grammar td { border: 1px solid #ccc; padding: 2px 6px; }
.hint { color: #888; font-size: 12px; }
```

- [ ] **Step 4: Write the frontend — HTML shell**

Replace `src/archeon/review/static/index.html` with:

```html
<!doctype html>
<meta charset="utf-8">
<title>Archeon Review</title>
<link rel="stylesheet" href="/style.css">
<header>
  <strong>Archeon Review</strong>
  <button id="tab-browse" class="active">Browse</button>
  <button id="tab-queue">Queue</button>
  <span class="hint">focus a card, then: <b>a</b> accept · <b>e</b> edit · <b>r</b> reject</span>
</header>
<div id="main">
  <div id="nav"></div>
  <div id="cards"></div>
</div>
<script src="/app.js"></script>
```

- [ ] **Step 5: Write the frontend — app.js**

Create `src/archeon/review/static/app.js`:

```javascript
const nav = document.getElementById("nav");
const cards = document.getElementById("cards");
let state = { component: null, cluster: null, focus: -1, list: [] };

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function bar(row) {
  const seg = (k) => row[k] ? `<div class="${k}" style="flex:${row[k]}"></div>` : "";
  return `<div class="bar">${seg("verified")}${seg("contested")}${seg("unrecovered")}${seg("rejected")}</div>`;
}

async function showComponents() {
  state.component = state.cluster = null;
  const comps = await api("/api/components");
  nav.innerHTML = "<h3>Components</h3>" + comps.map((c) =>
    `<button class="tile" data-comp="${encodeURIComponent(c.component)}">
       ${c.component} <span class="meta">(${c.total})</span>${bar(c)}</button>`).join("");
  nav.querySelectorAll("[data-comp]").forEach((b) =>
    b.onclick = () => showClusters(decodeURIComponent(b.dataset.comp)));
  cards.innerHTML = "<p class='hint'>Pick a component.</p>";
}

async function showClusters(component) {
  state.component = component; state.cluster = null;
  const cl = await api("/api/clusters?component=" + encodeURIComponent(component));
  nav.innerHTML = `<button class="tile" id="back">← components</button>
    <h3>${component}</h3>` + cl.map((c) =>
    `<button class="tile" data-cl="${encodeURIComponent(c.cluster)}">
       ${c.cluster} <span class="meta">(${c.total})${c.clustered ? " ●" : ""}</span>${bar(c)}</button>`).join("");
  document.getElementById("back").onclick = showComponents;
  nav.querySelectorAll("[data-cl]").forEach((b) =>
    b.onclick = () => showCards(component, decodeURIComponent(b.dataset.cl)));
}

async function showCards(component, cluster) {
  state.component = component; state.cluster = cluster;
  state.list = await api(`/api/claims?component=${encodeURIComponent(component)}&cluster=${encodeURIComponent(cluster)}`);
  renderCards();
}

async function showQueue() {
  state.component = " queue"; state.cluster = null;
  state.list = await api("/api/queue");
  nav.innerHTML = "<h3>Queue</h3><p class='hint'>impact × uncertainty; contested first.</p>";
  renderCards();
}

function grammar(spec) {
  if (!spec) return "";
  if (spec.mode === "prose") return `<p>${spec.text}</p>`;
  if (spec.mode === "table")
    return `<table class="grammar">` +
      spec.rows.map((r) => `<tr><td>${r[0]}</td><td>${r[1]}</td></tr>`).join("") + `</table>`;
  if (spec.mode === "mermaid") {
    const pre = `<pre class="mermaid">${spec.src}</pre>`;
    if (window.mermaid) queueMicrotask(() => window.mermaid.run());
    return pre + `<div class="meta">${spec.caption || ""}</div>`;
  }
  return "";
}

function renderCards() {
  state.focus = state.list.length ? 0 : -1;
  cards.innerHTML = state.list.map((c, i) => {
    if (c.broken)
      return `<div class="card broken" data-i="${i}"><b>${c.id}</b> — parse error: ${c.error}</div>`;
    const ev = (c.evidence || []).map((e) =>
      `<div class="evidence">${e.ref}\n${e.excerpt || ""}</div>`).join("");
    const ce = (c.counter_evidence || []).length
      ? `<div class="counter">⚠ ${c.counter_evidence.join("; ")}</div>` : "";
    return `<div class="card ${c.bucket}" data-i="${i}">
      <div class="meta">${c.id} · ${c.type} · ${c.status} · conf ${c.confidence}</div>
      <p class="statement">${c.statement}</p>
      ${grammar(c.render)}${ev}${ce}</div>`;
  }).join("") || "<p class='hint'>No claims here.</p>";
  updateFocus();
  cards.querySelectorAll("[data-i]").forEach((el) =>
    el.onclick = () => { state.focus = +el.dataset.i; updateFocus(); });
}

function updateFocus() {
  cards.querySelectorAll(".card").forEach((el) =>
    el.classList.toggle("focus", +el.dataset.i === state.focus));
}

async function act(status, statement) {
  const c = state.list[state.focus];
  if (!c || c.broken) return;
  const body = { status, version: c.version };
  if (statement != null) body.statement = statement;
  const r = await fetch("/api/claims/" + encodeURIComponent(c.id), {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify(body) });
  if (r.status === 409) { alert("This claim changed on disk. Reloading."); return reloadList(); }
  if (!r.ok) { alert("Write failed: " + (await r.text())); return; }
  const out = await r.json();
  c.version = out.version; c.status = status;
  if (statement != null) c.statement = statement;
  c.bucket = status === "rejected" ? "rejected"
           : status === "expert_accepted" ? "verified" : c.bucket;
  renderCards();
}

function reloadList() {
  if (state.component === " queue") return showQueue();
  if (state.cluster != null) return showCards(state.component, state.cluster);
  return showComponents();
}

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || state.focus < 0) return;
  if (e.key === "a") act("expert_accepted");
  else if (e.key === "r") act("rejected");
  else if (e.key === "e") {
    const c = state.list[state.focus];
    const next = prompt("Edit statement:", c ? c.statement : "");
    if (next != null) act("expert_accepted", next);
  }
});

document.getElementById("tab-browse").onclick = (e) => {
  setTab(e.target); showComponents();
};
document.getElementById("tab-queue").onclick = (e) => {
  setTab(e.target); showQueue();
};
function setTab(btn) {
  document.querySelectorAll("header button").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
}

showComponents();
```

- [ ] **Step 6: Write the Playwright smoke test**

Create `tests/test_review_smoke.py`:

```python
import socket
import subprocess
import sys
import time

import pytest
import yaml

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _write_claim(claims_dir):
    (claims_dir / "CLM-0001.yaml").write_text(yaml.safe_dump({
        "id": "CLM-0001", "type": "threshold", "statement": "temp <= 90",
        "feature": "src/foo", "layer": "what", "status": "machine_verified",
        "confidence": 0.9, "symbols": ["MAX_TEMP"],
        "evidence": [{"kind": "code", "ref": "a.c:1-2", "role": "primary",
                      "excerpt": "if (t > MAX_TEMP)"}],
        "counter_evidence": [],
    }, sort_keys=False), encoding="utf-8")


def _wait_port(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("server did not start")


def test_browse_and_accept_round_trip(tmp_path):
    _write_claim(tmp_path)
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "archeon.cli", "review",
         "--claims", str(tmp_path), "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        _wait_port(port)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}/")
            page.click("button[data-comp]")           # open the component
            page.click("button[data-cl]")             # open its cluster
            page.wait_for_selector(".card")           # treemap drilled to a card
            page.click(".card")                        # focus it
            page.keyboard.press("a")                   # accept
            page.wait_for_timeout(400)
            browser.close()
        on_disk = yaml.safe_load((tmp_path / "CLM-0001.yaml").read_text())
        assert on_disk["status"] == "expert_accepted"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
```

- [ ] **Step 7: Run the smoke test to verify it passes**

Run: `uv run pytest tests/test_review_smoke.py -v`
Expected: PASS (1 passed). If Playwright's browser is missing, it fails with a clear install message — run `uv run playwright install chromium` (Step 2).

- [ ] **Step 8: Manual visual check (optional but recommended)**

Run: `uv run archeon review --claims claims_motor_ctrl --port 8000` and open `http://127.0.0.1:8000/`.
Expected: components list with colored bars; drilling reaches claim cards; `a`/`e`/`r` change a card and the corresponding `claims_motor_ctrl/*.yaml` shows a git diff. Stop with Ctrl-C.

- [ ] **Step 9: Run the full suite**

Run: `uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 10: Commit**

```bash
git add pyproject.toml uv.lock src/archeon/review/static tests/test_review_smoke.py
git commit -m "feat(review): frontend bundle (treemap/cards/queue/keybindings) + Playwright smoke test"
```

---

## Self-Review

**Spec coverage (spec §1–§8):**
- §2 Browse component→cluster→claim→evidence, colored by state → Tasks 2, 3, 6, 8.
- §2 Accept/edit/reject with single keystrokes, persisted to YAML as git diffs → Tasks 1, 6, 8.
- §2 Per-type visual grammar with prose fallback → Task 5 (server-side render-specs).
- §2 Queue sorted by impact × uncertainty, contested surfaced → Task 4.
- §3.1 Backend routes (`/components`, `/clusters`, `/claims`, `POST /claims/{id}`) → Task 6. Single `save_claim` path preserving field order/untouched fields → Task 1. Status enum delta (`expert_accepted`, `rejected`) → Task 1.
- §3.2 Static frontend, treemap entry, drill, claim card, keystrokes; vanilla JS + Mermaid, no SPA framework → Task 8.
- §3.3 Renderer registry keyed by type, prose fallback for every type → Task 5.
- §3.4 Queue view → Task 4 (backend) + Task 8 (tab).
- §5 Degradation: no cluster metadata → flat grouping (Task 3); stale write → 409 (Tasks 1, 6); malformed YAML → broken card (Task 2); write failure surfaced (Task 6, 8).
- §6 Testing: backend round-trip, stale-write rejection, degradation, renderers, frontend smoke → Tasks 1, 2, 3, 5, 6, 8.
- §7 CSV path untouched → global constraint; no task modifies `claim_eval.py`.
- §8 Open questions resolved: queue scoring `uncertainty = 1 - confidence`, impact = fan-in / symbol count (Task 4); Mermaid chosen, guarded so a missing lib degrades to showing diagram source (Task 8).

**Placeholder scan:** none — every code step carries complete code; no TBD/TODO.

**Type consistency:** `save_claim(status, statement, expected_version) -> version` (Task 1) is called consistently by the server (Task 6). `claim_card` keys (Task 2) are consumed by `render_spec` (Task 5: `type`, `symbols`, `statement`) and the frontend (Task 8: `id`, `type`, `status`, `bucket`, `confidence`, `statement`, `evidence`, `counter_evidence`, `version`, `render`). Verification buckets (`verified`/`contested`/`unrecovered`/`rejected`) are consistent across store counts, card `bucket`, and CSS classes. Cluster keying (`_cluster_key`) is shared by `clusters` and `claims_in` so drill filters match their counts.

**Scope:** single subsystem (a local review app over one artifact directory); one implementation plan is appropriate.
