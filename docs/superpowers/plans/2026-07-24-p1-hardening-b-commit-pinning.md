# Commit-Pinned Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Anchor each piece of code evidence to a commit SHA + a whitespace-normalized content hash of the exact cited lines, so staleness (a semantic edit to a cited span) is decidable and survives line-number drift.

**Architecture:** Add optional anchor fields to `Evidence` (backward-compatible). A new `claims/pin.py` captures anchors right after synthesis (`pin_evidence`/`pin_claims`) and later decides staleness (`is_stale`/`stale_claims`) by relocating the pinned normalized block in the current working tree. Git reads (`HEAD` sha, blob sha, file-at-rev, dirty check) are added to `git_connector`. A `check-staleness` CLI reports stale or unpinnable claims.

**Tech Stack:** Python ≥3.12, `dataclasses`, `hashlib` (stdlib), `git` via `subprocess`, `click` CLI, `pytest`.

## Global Constraints

Copied verbatim from the spec — every task's requirements implicitly include these:

- **Additive and backward-compatible:** existing `file:line`-only evidence YAML still loads (all new `Evidence` fields default `None`).
- **Per-evidence degradation, never abort:** pinning/staleness failures degrade one evidence to `unpinnable`/false; they never raise out of the run (consistent with the verify loop's resilience fix).
- **Whitespace normalization lives in ONE helper** reused by both capture and check, so they can never diverge. Normalization = strip trailing whitespace per line + drop blank-only lines. (Leading indentation is *not* normalized — start conservative per spec §9.)
- **Staleness is content/blob-hash anchored:** flags semantic edits to a cited span, ignores cosmetic reflow, survives line-number drift.
- **Non-goals (do not build):** re-verifying/re-synthesizing stale claims; blame/attribution; cross-file move tracking (a moved file reads as unpinnable/stale).
- Run tests with `uv run pytest`.

## File Structure

- `src/archaeon/claims/schema.py` — **modify**: add optional anchor fields to `Evidence`. Owns the persisted claim/evidence shape.
- `src/archaeon/connectors/git_connector.py` — **modify**: add read helpers (`head_sha`, `blob_sha`, `show_file`, `is_dirty`). Owns all git subprocess access.
- `src/archaeon/claims/pin.py` — **create**: ref parsing, normalization, hashing, `pin_evidence`, `pin_claims`, `is_stale`, `stale_claims`. Owns anchoring + staleness logic.
- `src/archaeon/cli.py` — **modify**: wire `pin_claims` into `synthesize`; add `check-staleness` command.
- `tests/test_claims_schema.py` — **modify**: backward-compat + anchor-roundtrip.
- `tests/test_git_connector.py` — **modify**: new git read helpers.
- `tests/test_claims_pin.py` — **create**: ref/normalize/hash + pin + staleness.
- `tests/test_cli.py` — **modify**: `check-staleness` integration.

---

### Task 1: Evidence anchor fields (schema)

**Files:**
- Modify: `src/archaeon/claims/schema.py:13-19`
- Test: `tests/test_claims_schema.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Evidence` dataclass with new optional fields `commit_sha: str | None`, `blob_sha: str | None`, `line_start: int | None`, `line_end: int | None`, `content_hash: str | None`, `pin_status: str | None` (values `"pinned" | "dirty" | "unpinnable"`), all defaulting `None`. `Evidence.from_dict` path (`Evidence(**e)` in `Claim.from_dict`) already tolerates absent keys — no change needed there.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_claims_schema.py`:

```python
def test_evidence_backward_compat_loads_without_anchor_fields(tmp_path):
    # A pre-Spec-B claim YAML with file:line-only evidence must still load,
    # with all anchor fields null.
    d = tmp_path / "claims"
    d.mkdir()
    (d / "CLM-0001.yaml").write_text(
        "id: CLM-0001\ntype: threshold\nstatement: s\n"
        "evidence:\n- kind: code\n  ref: a.c:1\n  role: primary\n"
        "  excerpt: x\n",
        encoding="utf-8")
    [c] = load_claims(d)
    e = c.evidence[0]
    assert e.ref == "a.c:1"
    assert e.commit_sha is None and e.blob_sha is None
    assert e.line_start is None and e.line_end is None
    assert e.content_hash is None and e.pin_status is None


def test_evidence_anchor_fields_roundtrip(tmp_path):
    claims = [Claim(id="CLM-0001", type="threshold", statement="s",
                    evidence=[Evidence(
                        kind="code", ref="a.c:1-3", excerpt="body",
                        commit_sha="abc123", blob_sha="def456",
                        line_start=1, line_end=3,
                        content_hash="deadbeef", pin_status="pinned")])]
    save_claims(claims, tmp_path / "c")
    [c] = load_claims(tmp_path / "c")
    e = c.evidence[0]
    assert e.commit_sha == "abc123" and e.blob_sha == "def456"
    assert e.line_start == 1 and e.line_end == 3
    assert e.content_hash == "deadbeef" and e.pin_status == "pinned"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_claims_schema.py -v`
Expected: FAIL — `TypeError: Evidence.__init__() got an unexpected keyword argument 'commit_sha'`.

- [ ] **Step 3: Add the fields**

Replace the `Evidence` dataclass in `src/archaeon/claims/schema.py`:

```python
@dataclass
class Evidence:
    kind: str            # code | ticket | pr_comment
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_claims_schema.py -v`
Expected: PASS (including the pre-existing `test_claim_roundtrip_yaml`).

- [ ] **Step 5: Commit**

```bash
git add src/archaeon/claims/schema.py tests/test_claims_schema.py
git commit -m "feat(claims): add optional commit-pin anchor fields to Evidence"
```

---

### Task 2: Git read helpers

**Files:**
- Modify: `src/archaeon/connectors/git_connector.py`
- Test: `tests/test_git_connector.py`

**Interfaces:**
- Consumes: nothing.
- Produces (all take `repo_path: Path`, never raise on git failure):
  - `head_sha(repo_path) -> str | None` — full 40-char `HEAD` sha, or `None`.
  - `blob_sha(repo_path, path: str, rev: str = "HEAD") -> str | None` — blob sha of `path` at `rev`, `None` if untracked/missing.
  - `show_file(repo_path, path: str, rev: str = "HEAD") -> list[str] | None` — file contents at `rev` as a list of lines (no trailing newline element), `None` if missing.
  - `is_dirty(repo_path) -> bool` — `True` if the working tree has any tracked-change or untracked file (`git status --porcelain` non-empty).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_git_connector.py` (reuses the existing `_make_repo`, which commits `src/a.c` as `"int a;\n"` then `"int a;\nint b;\n"`):

```python
from archaeon.connectors.git_connector import (  # noqa: E402
    blob_sha, head_sha, is_dirty, show_file)


def test_head_sha_returns_full_sha(tmp_path):
    repo = _make_repo(tmp_path)
    sha = head_sha(repo)
    assert sha is not None and len(sha) == 40


def test_show_file_returns_lines_at_head(tmp_path):
    repo = _make_repo(tmp_path)
    assert show_file(repo, "src/a.c", "HEAD") == ["int a;", "int b;"]


def test_blob_sha_present_and_none_for_missing(tmp_path):
    repo = _make_repo(tmp_path)
    assert blob_sha(repo, "src/a.c", "HEAD")
    assert blob_sha(repo, "nope.c", "HEAD") is None
    assert show_file(repo, "nope.c", "HEAD") is None


def test_is_dirty_reflects_working_tree(tmp_path):
    repo = _make_repo(tmp_path)
    assert is_dirty(repo) is False
    (repo / "src" / "a.c").write_text("int a;\nint b;\nint c;\n")
    assert is_dirty(repo) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_git_connector.py -v`
Expected: FAIL — `ImportError: cannot import name 'head_sha'`.

- [ ] **Step 3: Add the helpers**

Append to `src/archaeon/connectors/git_connector.py`:

```python
def _run(repo_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command without raising on non-zero exit — callers inspect
    returncode so a missing file/ref degrades to None rather than aborting."""
    return subprocess.run(["git", "-C", str(repo_path), *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def head_sha(repo_path: Path) -> str | None:
    r = _run(repo_path, "rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def blob_sha(repo_path: Path, path: str, rev: str = "HEAD") -> str | None:
    r = _run(repo_path, "rev-parse", f"{rev}:{path}")
    return r.stdout.strip() if r.returncode == 0 else None


def show_file(repo_path: Path, path: str, rev: str = "HEAD") -> list[str] | None:
    r = _run(repo_path, "show", f"{rev}:{path}")
    return r.stdout.splitlines() if r.returncode == 0 else None


def is_dirty(repo_path: Path) -> bool:
    r = _run(repo_path, "status", "--porcelain")
    return bool(r.stdout.strip())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_git_connector.py -v`
Expected: PASS (all, including pre-existing ingest tests).

- [ ] **Step 5: Commit**

```bash
git add src/archaeon/connectors/git_connector.py tests/test_git_connector.py
git commit -m "feat(git): add head_sha/blob_sha/show_file/is_dirty read helpers"
```

---

### Task 3: Pin primitives — ref parsing, normalization, hashing

**Files:**
- Create: `src/archaeon/claims/pin.py`
- Test: `tests/test_claims_pin.py`

**Interfaces:**
- Consumes: git helpers from Task 2 (imported now, used in Tasks 4–5).
- Produces:
  - `parse_ref(ref: str) -> tuple[str, int, int] | None` — `(path, line_start, line_end)`; accepts `"path:line"` (start==end) and `"path:start-end"`; `None` for garbage or `end < start`.
  - `normalize(lines: list[str]) -> list[str]` — the single shared normalizer: `rstrip` each line, drop blank-only lines.
  - `content_hash(norm_lines: list[str]) -> str` — sha256 hex of `"\n".join(norm_lines)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_claims_pin.py`:

```python
from archaeon.claims.pin import content_hash, normalize, parse_ref


def test_parse_ref_single_line():
    assert parse_ref("src/a.c:12") == ("src/a.c", 12, 12)


def test_parse_ref_range():
    assert parse_ref("src/a.c:12-20") == ("src/a.c", 12, 20)


def test_parse_ref_rejects_garbage():
    assert parse_ref("no-line-here") is None
    assert parse_ref("") is None
    assert parse_ref("src/a.c:5-3") is None  # end before start


def test_normalize_strips_trailing_ws_and_drops_blanks():
    assert normalize(["a  ", "", "  ", "b\t"]) == ["a", "b"]


def test_normalize_keeps_leading_indentation():
    assert normalize(["  return 42;  "]) == ["  return 42;"]


def test_content_hash_stable_under_cosmetic_change():
    assert content_hash(normalize(["x = 1", "y = 2"])) == \
        content_hash(normalize(["x = 1   ", "", "y = 2"]))


def test_content_hash_changes_on_semantic_edit():
    assert content_hash(normalize(["return 42;"])) != \
        content_hash(normalize(["return 7;"]))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_claims_pin.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'archaeon.claims.pin'`.

- [ ] **Step 3: Create `pin.py` with the primitives**

Create `src/archaeon/claims/pin.py`:

```python
import hashlib
import re
from pathlib import Path

from archaeon.connectors.git_connector import (
    blob_sha, head_sha, is_dirty, show_file)

_REF_RE = re.compile(r"^(?P<path>.+):(?P<start>\d+)(?:-(?P<end>\d+))?$")


def parse_ref(ref: str) -> tuple[str, int, int] | None:
    """Parse 'path:line' or 'path:start-end' into (path, start, end).
    Returns None for anything that is not a well-formed ref (hallucinated
    prose, empty, or an inverted range)."""
    m = _REF_RE.match((ref or "").strip())
    if not m:
        return None
    start = int(m.group("start"))
    end = int(m.group("end")) if m.group("end") else start
    if end < start:
        return None
    return m.group("path"), start, end


def normalize(lines: list[str]) -> list[str]:
    """The single shared normalizer used by BOTH anchor capture and the
    staleness check, so they can never diverge. Strips trailing whitespace
    per line and drops blank-only lines; leading indentation is preserved."""
    return [s.rstrip() for s in lines if s.rstrip()]


def content_hash(norm_lines: list[str]) -> str:
    return hashlib.sha256("\n".join(norm_lines).encode("utf-8")).hexdigest()


def _span(lines: list[str], start: int, end: int) -> list[str] | None:
    """1-indexed inclusive slice; None if out of the file's bounds."""
    if start < 1 or end > len(lines) or start > end:
        return None
    return lines[start - 1:end]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_claims_pin.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/archaeon/claims/pin.py tests/test_claims_pin.py
git commit -m "feat(claims): pin primitives — ref parsing, normalization, hashing"
```

---

### Task 4: Anchor capture — `pin_evidence` / `pin_claims`

**Files:**
- Modify: `src/archaeon/claims/pin.py`
- Test: `tests/test_claims_pin.py`

**Interfaces:**
- Consumes: `parse_ref`, `normalize`, `content_hash`, `_span` (Task 3); `head_sha`, `blob_sha`, `show_file`, `is_dirty` (Task 2); `Evidence` (Task 1).
- Produces:
  - `pin_evidence(evidence: Evidence, repo_path) -> None` — mutates one `Evidence` in place. On success fills `commit_sha` (HEAD), `blob_sha`, `line_start`, `line_end`, `content_hash` (normalized hash of the HEAD span), and sets `pin_status="dirty"` if the repo has uncommitted/untracked changes else `"pinned"`. On a bad ref, missing file, or out-of-bounds range: leaves anchors `None` and sets `pin_status="unpinnable"`. Never raises.
  - `pin_claims(claims, repo_path) -> None` — calls `pin_evidence` on every `kind == "code"` evidence of every claim; leaves non-code evidence (ticket/pr_comment) untouched.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_claims_pin.py`:

```python
import subprocess

from archaeon.claims.pin import pin_claims, pin_evidence
from archaeon.claims.schema import Claim, Evidence


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True,
                   capture_output=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "src" / "f.c").write_text("int f(void) {\n  return 42;\n}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def test_pin_evidence_fills_anchor_on_clean_repo(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/f.c:1-3")
    pin_evidence(e, repo)
    assert e.pin_status == "pinned"
    assert e.commit_sha and len(e.commit_sha) == 40
    assert e.blob_sha
    assert e.line_start == 1 and e.line_end == 3
    assert e.content_hash


def test_pin_evidence_marks_dirty_repo(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src" / "untracked.c").write_text("x\n")  # working tree dirty
    e = Evidence(kind="code", ref="src/f.c:2")
    pin_evidence(e, repo)
    assert e.pin_status == "dirty"
    assert e.commit_sha  # still anchored, just provisional


def test_pin_evidence_unpinnable_on_bad_ref(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="this is not a ref")
    pin_evidence(e, repo)
    assert e.pin_status == "unpinnable"
    assert e.commit_sha is None and e.content_hash is None


def test_pin_evidence_unpinnable_out_of_bounds(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/f.c:99-120")
    pin_evidence(e, repo)
    assert e.pin_status == "unpinnable"


def test_pin_evidence_unpinnable_missing_file(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/ghost.c:1")
    pin_evidence(e, repo)
    assert e.pin_status == "unpinnable"


def test_pin_claims_only_touches_code_evidence(tmp_path):
    repo = _repo(tmp_path)
    c = Claim(id="CLM-0001", type="threshold", statement="s",
              evidence=[Evidence(kind="code", ref="src/f.c:1"),
                        Evidence(kind="ticket", ref="EMB-1")])
    pin_claims([c], repo)
    assert c.evidence[0].pin_status == "pinned"
    assert c.evidence[1].pin_status is None  # ticket evidence untouched
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_claims_pin.py -v`
Expected: FAIL — `ImportError: cannot import name 'pin_evidence'`.

- [ ] **Step 3: Add `pin_evidence` and `pin_claims`**

Append to `src/archaeon/claims/pin.py`:

```python
def pin_evidence(evidence, repo_path) -> None:
    """Capture a commit-pinned anchor on one Evidence in place. Degrades
    per-evidence: a bad ref, missing file, or out-of-bounds range sets
    pin_status='unpinnable' and never raises (a run must never abort here)."""
    repo_path = Path(repo_path)
    parsed = parse_ref(evidence.ref)
    if not parsed:
        evidence.pin_status = "unpinnable"
        return
    path, start, end = parsed
    sha = head_sha(repo_path)
    lines = show_file(repo_path, path, "HEAD") if sha else None
    if not sha or lines is None:
        evidence.pin_status = "unpinnable"
        return
    span = _span(lines, start, end)
    if span is None:
        evidence.pin_status = "unpinnable"
        return
    evidence.commit_sha = sha
    evidence.blob_sha = blob_sha(repo_path, path, "HEAD")
    evidence.line_start = start
    evidence.line_end = end
    evidence.content_hash = content_hash(normalize(span))
    evidence.pin_status = "dirty" if is_dirty(repo_path) else "pinned"


def pin_claims(claims, repo_path) -> None:
    """Pin every code evidence of every claim (non-code evidence, e.g.
    tickets/PR comments, has no repo anchor and is skipped)."""
    for c in claims:
        for e in c.evidence:
            if e.kind == "code":
                pin_evidence(e, repo_path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_claims_pin.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/archaeon/claims/pin.py tests/test_claims_pin.py
git commit -m "feat(claims): capture commit-pinned anchors (pin_evidence/pin_claims)"
```

---

### Task 5: Staleness check — `is_stale` / `stale_claims`

**Files:**
- Modify: `src/archaeon/claims/pin.py`
- Test: `tests/test_claims_pin.py`

**Interfaces:**
- Consumes: `parse_ref`, `normalize`, `content_hash`, `_span`, `show_file`, `Evidence`.
- Produces:
  - `is_stale(evidence: Evidence, repo_path) -> bool` — `True` iff a `pinned`/`dirty` anchor's normalized block can no longer be located anywhere in the current working-tree file. Legacy/`unpinnable`/unpinned evidence returns `False` (not stale — it is surfaced separately as unpinnable). Never raises.
  - `stale_claims(claims, repo_path) -> list` — claims having any **primary** `code` evidence for which `is_stale` is `True`.

**Approach note (content/blob-hash relocation):** to survive line-number drift (spec §7 test 1), staleness is *not* a fixed-line re-read. The pinned normalized block is re-read from the immutable `commit_sha` blob (giving block length `L`); it is "found" if any length-`L` window of the current file's normalized lines hashes to `content_hash`. Insert-above → block relocates → found → not stale. Edit-inside → block absent → stale. Cosmetic reflow → normalization erases it → still found → not stale.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_claims_pin.py` (reuses `_repo`, `_git` from Task 4):

```python
from archaeon.claims.pin import is_stale, stale_claims


def test_not_stale_when_lines_inserted_above(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/f.c:2")  # "  return 42;"
    pin_evidence(e, repo)
    assert e.pin_status == "pinned"
    # Insert a line ABOVE the cited span — line numbers drift, content does not.
    (repo / "src" / "f.c").write_text(
        "int g(void) { return 0; }\nint f(void) {\n  return 42;\n}\n")
    assert is_stale(e, repo) is False


def test_stale_when_cited_line_edited(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/f.c:2")
    pin_evidence(e, repo)
    (repo / "src" / "f.c").write_text("int f(void) {\n  return 7;\n}\n")
    assert is_stale(e, repo) is True


def test_not_stale_on_cosmetic_reflow(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/f.c:1-3")
    pin_evidence(e, repo)
    # Trailing whitespace + an inserted blank line inside the span only.
    (repo / "src" / "f.c").write_text(
        "int f(void) {  \n\n  return 42;   \n}\n")
    assert is_stale(e, repo) is False


def test_stale_when_cited_file_deleted(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/f.c:2")
    pin_evidence(e, repo)
    (repo / "src" / "f.c").unlink()
    assert is_stale(e, repo) is True


def test_legacy_evidence_is_never_stale(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/f.c:2")  # never pinned
    assert e.pin_status is None
    assert is_stale(e, repo) is False


def test_unpinnable_evidence_is_never_stale(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/ghost.c:1")
    pin_evidence(e, repo)
    assert e.pin_status == "unpinnable"
    assert is_stale(e, repo) is False


def test_stale_claims_selects_only_drifted_claim(tmp_path):
    repo = _repo(tmp_path)
    good = Claim(id="CLM-0001", type="threshold", statement="s1",
                 evidence=[Evidence(kind="code", ref="src/f.c:1")])
    bad = Claim(id="CLM-0002", type="threshold", statement="s2",
                evidence=[Evidence(kind="code", ref="src/f.c:2")])
    pin_claims([good, bad], repo)
    (repo / "src" / "f.c").write_text("int f(void) {\n  return 7;\n}\n")
    result = stale_claims([good, bad], repo)
    assert [c.id for c in result] == ["CLM-0002"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_claims_pin.py -v`
Expected: FAIL — `ImportError: cannot import name 'is_stale'`.

- [ ] **Step 3: Add `is_stale` and `stale_claims`**

Append to `src/archaeon/claims/pin.py`:

```python
def is_stale(evidence, repo_path) -> bool:
    """True iff a pinned code anchor's normalized block can no longer be
    located in the current working-tree file. Legacy/unpinnable evidence is
    never 'stale' (surfaced separately). Never raises."""
    if evidence.pin_status not in ("pinned", "dirty"):
        return False
    if not evidence.content_hash or not evidence.commit_sha:
        return False
    parsed = parse_ref(evidence.ref)
    if not parsed or evidence.line_start is None or evidence.line_end is None:
        return False
    path = parsed[0]
    repo_path = Path(repo_path)
    # Recover the pinned block (and its normalized length) from the immutable
    # commit the anchor was captured against.
    pinned = show_file(repo_path, path, evidence.commit_sha)
    span = _span(pinned, evidence.line_start, evidence.line_end) if pinned \
        else None
    block = normalize(span) if span else []
    if not block:
        return True  # anchored content no longer recoverable -> drifted
    current_file = repo_path / path
    if not current_file.is_file():
        return True  # cited file gone (moved/deleted) -> stale, not silent
    current = normalize(
        current_file.read_text(encoding="utf-8", errors="replace").splitlines())
    length = len(block)
    for i in range(len(current) - length + 1):
        if content_hash(current[i:i + length]) == evidence.content_hash:
            return False  # relocated -> not stale (survives line drift)
    return True


def stale_claims(claims, repo_path) -> list:
    """Claims with any stale primary code evidence."""
    return [c for c in claims
            if any(is_stale(e, repo_path) for e in c.evidence
                   if e.role == "primary" and e.kind == "code")]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_claims_pin.py -v`
Expected: PASS (all of Tasks 3–5).

- [ ] **Step 5: Commit**

```bash
git add src/archaeon/claims/pin.py tests/test_claims_pin.py
git commit -m "feat(claims): staleness check via content-hash relocation (is_stale/stale_claims)"
```

---

### Task 6: CLI wiring — pin on synthesize + `check-staleness`

**Files:**
- Modify: `src/archaeon/cli.py:148-175` (synthesize), and add a new command
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `pin_claims`, `is_stale` (Tasks 4–5); `load_claims` (schema); `config_mod.load`.
- Produces:
  - `synthesize` now calls `pin_claims(claims, repo_path)` after `verify_claims` and before `save_claims`, so persisted claim YAML carries anchors.
  - New command `check-staleness` (`--config`, `--claims <dir>`): prints one line per primary code evidence that is stale (`STALE  <claim_id>  <ref>  @<commit12>`) or unpinnable (`UNPINNABLE  <claim_id>  <ref>`), then a summary count. Reads `repo_path` from config only (no DB).

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_cli.py`:

```python
from archaeon.claims.pin import pin_claims  # noqa: E402
from archaeon.claims.schema import Claim, Evidence, save_claims  # noqa: E402


def _setup_repo_with_multiline_file(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "src" / "f.c").write_text(
        "int f(void) {\n  return 42;\n}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    config = tmp_path / "archaeon.toml"
    config.write_text(f"""
[component]
name = "demo"
db = "{(tmp_path / 'e.db').as_posix()}"
repo_path = "{repo.as_posix()}"
path_prefixes = ["src/"]

[jira]
base_url = "https://unused"
jql = "unused"
project_keys = ["EMB"]

[prs]
api_base = "https://unused"
repo = "o/r"

[wiki]
export_dir = "{(tmp_path / 'wiki').as_posix()}"

[llm]
cheap_model = "claude-haiku-4-5-20251001"
max_commits = 10
""")
    return repo, config


def test_check_staleness_flags_only_edited_claim(tmp_path):
    repo, config = _setup_repo_with_multiline_file(tmp_path)
    claims_dir = tmp_path / "claims"
    good = Claim(id="CLM-0001", type="threshold", statement="signature",
                 evidence=[Evidence(kind="code", ref="src/f.c:1")])
    bad = Claim(id="CLM-0002", type="threshold", statement="return value",
                evidence=[Evidence(kind="code", ref="src/f.c:2")])
    pin_claims([good, bad], repo)
    save_claims([good, bad], claims_dir)
    # Edit exactly the region CLM-0002 cites.
    (repo / "src" / "f.c").write_text("int f(void) {\n  return 7;\n}\n")

    runner = CliRunner()
    r = runner.invoke(main, ["check-staleness", "--config", str(config),
                             "--claims", str(claims_dir)])
    assert r.exit_code == 0, r.output
    assert "STALE" in r.output and "CLM-0002" in r.output
    assert "CLM-0001" not in r.output  # untouched claim not flagged


def test_check_staleness_reports_unpinnable(tmp_path):
    repo, config = _setup_repo_with_multiline_file(tmp_path)
    claims_dir = tmp_path / "claims"
    c = Claim(id="CLM-0001", type="threshold", statement="ghost",
              evidence=[Evidence(kind="code", ref="src/ghost.c:1")])
    pin_claims([c], repo)
    save_claims([c], claims_dir)

    runner = CliRunner()
    r = runner.invoke(main, ["check-staleness", "--config", str(config),
                             "--claims", str(claims_dir)])
    assert r.exit_code == 0, r.output
    assert "UNPINNABLE" in r.output and "CLM-0001" in r.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `check-staleness` is not a registered command (`r.exit_code != 0`, "No such command").

- [ ] **Step 3: Wire `pin_claims` into synthesize**

In `src/archaeon/cli.py`, in `cli_synthesize`, extend the local import and add the pin call. Change the import block:

```python
    from archaeon.claims.recover import (
        SYNTH_SYSTEM, VERIFY_SYSTEM, build_feature_bundle, synthesize_claims,
        verify_claims)
    from archaeon.claims.pin import pin_claims
    from archaeon.claims.schema import save_claims
```

Then, between `verify_claims(...)` and `save_claims(...)`, insert:

```python
    # Commit-pin each code evidence to a content hash + HEAD sha before
    # persisting, so staleness is decidable later (Spec B). Degrades
    # per-evidence; never aborts the run.
    pin_claims(claims, Path(cfg["component"]["repo_path"]))
    save_claims(claims, Path(out_dir))
```

- [ ] **Step 4: Add the `check-staleness` command**

Add to `src/archaeon/cli.py` (e.g. after `cli_claims_eval`):

```python
@main.command("check-staleness")
@config_option
@click.option("--claims", "claims_dir", default="claims", show_default=True)
def cli_check_staleness(config_path, claims_dir):
    """Report claims whose commit-pinned evidence has drifted (stale) or was
    never anchorable (unpinnable) — input to re-verification."""
    from archaeon.claims.pin import is_stale
    from archaeon.claims.schema import load_claims
    cfg = config_mod.load(Path(config_path))
    repo = Path(cfg["component"]["repo_path"])
    claims = load_claims(Path(claims_dir))
    flagged = 0
    for c in claims:
        for e in c.evidence:
            if e.role != "primary" or e.kind != "code":
                continue
            if is_stale(e, repo):
                click.echo(f"STALE       {c.id}  {e.ref}  "
                           f"@{(e.commit_sha or '')[:12]}")
                flagged += 1
            elif e.pin_status in (None, "unpinnable"):
                click.echo(f"UNPINNABLE  {c.id}  {e.ref}")
                flagged += 1
    click.echo(f"flagged: {flagged}  (claims scanned: {len(claims)})")
```

- [ ] **Step 5: Run the full suite to verify it passes**

Run: `uv run pytest -v`
Expected: PASS (all tests, including the two new CLI tests and every pre-existing test).

- [ ] **Step 6: Commit**

```bash
git add src/archaeon/cli.py tests/test_cli.py
git commit -m "feat(cli): pin evidence on synthesize; add check-staleness command"
```

---

## Self-Review

**1. Spec coverage:**

| Spec section | Task |
|---|---|
| §4.1 Evidence schema additions | Task 1 |
| §4.2 Anchor capture `pin_evidence`, called after synthesize before save, single normalization helper | Tasks 3, 4, 6 |
| §4.3 `is_stale`, `stale_claims` | Task 5 |
| §4.4 `check-staleness` CLI | Task 6 |
| §2 Additive/backward-compatible | Task 1 (backward-compat test) |
| §3 Content/blob-hash anchored, whitespace-normalized | Tasks 3 (normalize), 4 (capture), 5 (check) |
| §6 unpinnable / dirty / out-of-bounds degradation, never abort | Task 4 (all three cases), Task 5 |
| §7 hash stability (insert above) | Task 5 `test_not_stale_when_lines_inserted_above` |
| §7 edit inside span | Task 5 `test_stale_when_cited_line_edited` |
| §7 cosmetic reflow | Task 5 `test_not_stale_on_cosmetic_reflow` |
| §7 unpinnable | Task 4 + Task 5 `test_unpinnable_evidence_is_never_stale` |
| §7 backward compat (legacy loads, not stale) | Task 1 + Task 5 `test_legacy_evidence_is_never_stale` |
| §7 integration: pin, edit one region, only that claim stale | Task 6 `test_check_staleness_flags_only_edited_claim` |
| §8 `commit_sha` available for why-layer's `git log -L` | Task 1 field + Task 4 capture (no walk — correctly out of scope) |

Non-goals (re-verification, blame, `--follow` move tracking) are intentionally not implemented. Open question §9 (normalization exactness) is resolved conservatively: trailing-whitespace + blank-line normalization only, leading indentation preserved — asserted in Task 3 `test_normalize_keeps_leading_indentation`.

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to Task N". Every code step shows complete code; every test step shows the assertions; every run step gives the exact command and expected result.

**3. Type consistency:** `parse_ref` returns `(path, start, end)` everywhere; `normalize(list[str]) -> list[str]` and `content_hash(list[str]) -> str` used consistently by capture (Task 4) and check (Task 5); `pin_status` values `"pinned" | "dirty" | "unpinnable"` used identically in Tasks 4, 5, 6; git helpers' signatures match between Task 2 definition and Tasks 3–5 usage; `Evidence` field names (`commit_sha`, `blob_sha`, `line_start`, `line_end`, `content_hash`, `pin_status`) match between Task 1 and all consumers.
