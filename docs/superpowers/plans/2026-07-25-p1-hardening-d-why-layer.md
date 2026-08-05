# Why-layer Recovery (Pass 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `archeon why` stage that recovers why-layer claims (intent, rationale, constraint origin, tradeoff) by walking each what-claim's commit-pinned code span back through git history to the tickets and PRs that shaped it, then grounding and adversarially verifying the rationale against those artifacts.

**Architecture:** Three new modules with clean seams. `retrieval/archaeology.py` is pure git+SQL (span → shaping commits → ticket/PR refs). `claims/why_corpus.py` is pure SQL (rank and pack artifacts into a token-bounded corpus). `claims/why.py` holds synthesis, a deterministic LLM-free citation-grounding pass, and adversarial verification — mirroring `claims/recover.py`'s shape and taking an injected `ask` callable. `cli.py` gains a `why` command. Nothing new goes in SQL; results live in the claim YAML.

**Tech Stack:** Python ≥3.13, click, PyYAML, sqlite3, `git` CLI via subprocess, Claude Agent SDK via `archeon.llm.AgentClassifier`, pytest.

**Spec:** [2026-07-25-p1-hardening-d-why-layer-design.md](../specs/2026-07-25-p1-hardening-d-why-layer-design.md)

## Global Constraints

- Python `requires-python = ">=3.13"`. Run everything through `uv run`.
- A why-run **degrades per-unit and never aborts wholesale**. Only two conditions hard-fail, both *before* any LLM spend: an empty artifact lake, and a missing claims directory.
- **Never** write `run_cost.json` from `why` — `cli_synthesize` owns that filename in the same directory. `why` writes `why_cost.json`.
- The cost meter attaches to `AgentClassifier(model, system, max_turns=, meter=, stage=)`. Functions in `why.py` take **no** `meter` parameter.
- Cost stage names: `why-synth` and `why-verify` (hyphenated, matching the existing `cluster-label` / `synthesize` / `verify` vocabulary; both fit `format_summary`'s `{stage:<16}` column).
- Why-claim ids use the `WHY-` prefix, never `CLM-`.
- A `code_inferred` claim is **never** `machine_verified` and its confidence is capped at `CODE_INFERRED_MAX_CONFIDENCE = 0.4`.
- The model never emits code refs. Primary code evidence is copied mechanically off the explained what-claim.
- All new schema fields are additive with defaults, so every existing claim YAML loads unchanged. No `schema.sql` migration.
- ASCII only in anything printed via `click.echo` (stdout may be cp1252).

## File Structure

| File | Responsibility |
|---|---|
| `src/archeon/claims/schema.py` (modify) | `WHY_CLAIM_TYPES`, `corroboration` + `explains` fields, `CODE_INFERRED_MAX_CONFIDENCE` |
| `src/archeon/retrieval/archaeology.py` (create) | span → shaping commits; commits → ticket/PR refs |
| `src/archeon/config.py` (modify) | `WHY_DEFAULTS` + `why()` merge helper |
| `src/archeon/claims/why_corpus.py` (create) | rank + pack artifacts into a token-bounded corpus |
| `src/archeon/claims/why.py` (create) | prompts, synthesis, grounding, verification |
| `src/archeon/claims/claim_eval.py` (modify) | corroborated-precision metric |
| `src/archeon/cli.py` (modify) | `why` command, preconditions, cost wiring |
| `README.md` (modify) | why-layer runbook + gate |

Tests mirror this: `tests/test_archaeology.py`, `tests/test_why_corpus.py`, `tests/test_why.py`, plus additions to `tests/test_claims_schema.py`, `tests/test_claim_eval.py`, `tests/test_cli.py`, `tests/test_retrieval_config.py`.

---

### Task 1: Schema fields and why-layer types

**Files:**
- Modify: `src/archeon/claims/schema.py:1-55`
- Test: `tests/test_claims_schema.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `WHY_CLAIM_TYPES: set[str]`; `CODE_INFERRED_MAX_CONFIDENCE: float = 0.4`; `Claim.corroboration: str | None`; `Claim.explains: list`. `Claim.from_dict` defaults both new fields.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_claims_schema.py`:

```python
from archeon.claims.schema import (
    CODE_INFERRED_MAX_CONFIDENCE, CLAIM_TYPES, WHY_CLAIM_TYPES, Claim)


def test_why_claim_types_are_disjoint_from_what_types():
    assert WHY_CLAIM_TYPES == {
        "intent", "rationale", "constraint_origin", "tradeoff"}
    assert not (WHY_CLAIM_TYPES & CLAIM_TYPES)


def test_code_inferred_cap_is_below_default_confidence():
    assert CODE_INFERRED_MAX_CONFIDENCE == 0.4


def test_claim_defaults_corroboration_and_explains():
    c = Claim(id="WHY-0001", type="rationale", statement="s")
    assert c.corroboration is None
    assert c.explains == []


def test_legacy_claim_dict_without_new_fields_still_loads():
    # A claim file written before Spec D must round-trip unchanged.
    legacy = {"id": "CLM-0001", "type": "threshold", "statement": "s",
              "layer": "what", "status": "machine_verified"}
    c = Claim.from_dict(legacy)
    assert c.corroboration is None
    assert c.explains == []
    assert c.to_dict()["corroboration"] is None


def test_why_claim_round_trips_new_fields():
    c = Claim(id="WHY-0001", type="rationale", statement="s", layer="why",
              corroboration="corroborated", explains=["CLM-0007"])
    assert Claim.from_dict(c.to_dict()).explains == ["CLM-0007"]
    assert Claim.from_dict(c.to_dict()).corroboration == "corroborated"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_claims_schema.py -v -k "why_claim_types or code_inferred or corroboration or legacy_claim_dict or round_trips_new"`
Expected: FAIL with `ImportError: cannot import name 'WHY_CLAIM_TYPES'`

- [ ] **Step 3: Write minimal implementation**

In `src/archeon/claims/schema.py`, after the existing `CLAIM_TYPES` block:

```python
# why-layer claim types (code is a hypothesis; artifacts corroborate)
WHY_CLAIM_TYPES = {
    "intent", "rationale", "constraint_origin", "tradeoff",
}

# A why-claim with no surviving artifact evidence keeps its code hypothesis
# but is capped here and never auto-verified (design section 10.1).
CODE_INFERRED_MAX_CONFIDENCE = 0.4
```

Add two fields to `Claim`, after `counter_evidence`:

```python
    # why-layer only: corroborated | code_inferred. Orthogonal to `status`,
    # because the axes are independent — a code-inferred claim can still be
    # contested or expert-accepted without that erasing the fact that it was
    # never corroborated.
    corroboration: str | None = None
    explains: list = field(default_factory=list)   # what-claim ids
```

Extend `Claim.from_dict`'s constructor call with:

```python
            corroboration=d.get("corroboration"),
            explains=list(d.get("explains", [])),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_claims_schema.py -v`
Expected: PASS (all, including pre-existing tests)

- [ ] **Step 5: Confirm no existing claim files broke**

Run: `uv run pytest tests/test_save_claim.py tests/test_claims_recover.py tests/test_claim_eval.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/archeon/claims/schema.py tests/test_claims_schema.py
git commit -m "feat(claims/schema): why-layer types, corroboration and explains fields"
```

---

### Task 2: Span archaeology — shaping commits

**Files:**
- Create: `src/archeon/retrieval/archaeology.py`
- Test: `tests/test_archaeology.py`

**Interfaces:**
- Consumes: `archeon.connectors.git_connector._run(repo_path, *args) -> CompletedProcess` (does not raise on non-zero; callers inspect `returncode`).
- Produces: `shaping_commits(repo_path, path, start, end, rev="HEAD", max_commits=50) -> list[str]` and `file_level_commits(repo_path, path, max_commits=50) -> list[str]`, both newest-first, both returning `[]` on any git failure.

**Background the implementer needs:** `git log -L<start>,<end>:<path> <rev> --format=%H -s` lists the commits that changed that line range, newest first. `-s` suppresses the patch body so only shas print. `-L` **cannot** be combined with `--follow` (git exits with "--follow requires exactly one pathspec"), which is why the file-level fallback is a separate function. Passing `rev` matters: line numbers are only meaningful against the commit they were captured at, which is exactly what Spec B's `evidence.commit_sha` records.

- [ ] **Step 1: Write the failing test**

Create `tests/test_archaeology.py`:

```python
import subprocess

from archeon.retrieval.archaeology import file_level_commits, shaping_commits


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True,
                   capture_output=True)


def _sha(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True,
                          check=True).stdout.strip()


def _history_repo(tmp_path):
    """Repo with three commits: A creates 10 lines, B edits line 5,
    C inserts two lines at the top (shifting old line 5 to line 7)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    f = repo / "f.c"
    f.write_text("".join(f"l{i}\n" for i in range(1, 11)))
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "A")
    a = _sha(repo)
    f.write_text(f.read_text().replace("l5\n", "l5 modified\n"))
    _git(repo, "commit", "-am", "B")
    b = _sha(repo)
    f.write_text("new1\nnew2\n" + f.read_text())
    _git(repo, "commit", "-am", "C")
    c = _sha(repo)
    return repo, a, b, c


def test_shaping_commits_returns_touching_commits_newest_first(tmp_path):
    repo, a, b, _c = _history_repo(tmp_path)
    # Line 5 as of commit B was touched by B, and created by A.
    assert shaping_commits(repo, "f.c", 5, 5, rev=b) == [b, a]


def test_shaping_commits_anchors_on_the_given_rev(tmp_path):
    # The whole point of Spec B's commit_sha: the same line numbers mean
    # different content at different revs. At HEAD, line 5 is old line 3,
    # which only A ever touched.
    repo, a, b, _c = _history_repo(tmp_path)
    assert shaping_commits(repo, "f.c", 5, 5, rev="HEAD") == [a]
    assert shaping_commits(repo, "f.c", 5, 5, rev=b) == [b, a]


def test_shaping_commits_tracks_content_through_a_shift(tmp_path):
    # C inserted two lines above, so the pinned content sits at line 7 now.
    repo, a, b, _c = _history_repo(tmp_path)
    assert shaping_commits(repo, "f.c", 7, 7, rev="HEAD") == [b, a]


def test_shaping_commits_respects_max_commits(tmp_path):
    repo, _a, b, _c = _history_repo(tmp_path)
    assert shaping_commits(repo, "f.c", 5, 5, rev=b, max_commits=1) == [b]


def test_shaping_commits_returns_empty_for_unknown_path(tmp_path):
    repo, _a, _b, _c = _history_repo(tmp_path)
    assert shaping_commits(repo, "does/not/exist.c", 1, 2) == []


def test_shaping_commits_returns_empty_for_out_of_range_span(tmp_path):
    repo, _a, _b, _c = _history_repo(tmp_path)
    assert shaping_commits(repo, "f.c", 900, 950) == []


def test_file_level_commits_returns_whole_file_history(tmp_path):
    repo, a, b, c = _history_repo(tmp_path)
    assert file_level_commits(repo, "f.c") == [c, b, a]


def test_file_level_commits_respects_max_and_unknown_path(tmp_path):
    repo, _a, _b, c = _history_repo(tmp_path)
    assert file_level_commits(repo, "f.c", max_commits=1) == [c]
    assert file_level_commits(repo, "nope.c") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_archaeology.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'archeon.retrieval.archaeology'`

- [ ] **Step 3: Write minimal implementation**

Create `src/archeon/retrieval/archaeology.py`:

```python
"""Span-scoped git archaeology: which commits shaped a cited line range.

This is the Pass 2 entry point. It runs against the commit a piece of
evidence was *pinned* to (Spec B's ``evidence.commit_sha``), not HEAD —
line numbers only mean something against the commit they were captured
at, so anchoring is what makes this exact rather than approximate.
"""

from pathlib import Path

# Reuses the git connector's runner: it deliberately does not raise on a
# non-zero exit, so a missing path or bad rev degrades to [] here instead
# of aborting a whole why-run.
from archeon.connectors.git_connector import _run as git_run


def _shas(result, max_commits: int) -> list[str]:
    """Newest-first shas from a --format=%H log, deduped, capped."""
    if result.returncode != 0:
        return []
    out, seen = [], set()
    for line in result.stdout.splitlines():
        sha = line.strip()
        if not sha or sha in seen:
            continue
        seen.add(sha)
        out.append(sha)
        if len(out) >= max_commits:
            break
    return out


def shaping_commits(repo_path, path: str, start: int, end: int,
                    rev: str = "HEAD", max_commits: int = 50) -> list[str]:
    """Commits that changed lines ``start..end`` of ``path`` as of ``rev``.

    Newest first. Returns [] when the path, rev, or range does not resolve —
    a why-run must never abort on one bad anchor.
    """
    if start < 1 or end < start:
        return []
    return _shas(git_run(Path(repo_path), "log", f"-L{start},{end}:{path}",
                         rev, "--format=%H", "-s",
                         f"--max-count={max_commits}"), max_commits)


def file_level_commits(repo_path, path: str,
                       max_commits: int = 50) -> list[str]:
    """Whole-file history fallback for evidence with no usable span.

    Separate from ``shaping_commits`` because git rejects ``-L`` together
    with ``--follow`` ("--follow requires exactly one pathspec").
    """
    return _shas(git_run(Path(repo_path), "log", "--follow", "--format=%H",
                         f"--max-count={max_commits}", "--", path),
                 max_commits)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_archaeology.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add src/archeon/retrieval/archaeology.py tests/test_archaeology.py
git commit -m "feat(archaeology): span-scoped shaping-commit recovery anchored on the pin rev"
```

---

### Task 3: Resolve shaping commits to tickets and PRs

**Files:**
- Modify: `src/archeon/retrieval/archaeology.py`
- Test: `tests/test_archaeology.py`

**Interfaces:**
- Consumes: `shaping_commits` from Task 2; the `links` and `pr_commits` tables.
- Produces: `@dataclass ArtifactRefs` with `tickets: dict[str, set[str]]`, `prs: dict[int, set[str]]`, `unknown: set[str]`; and `artifacts_for_commits(conn, shas) -> ArtifactRefs`. Each dict maps an artifact to the shaping shas that reached it — those set sizes are the support scores Task 5 ranks by.

**Background the implementer needs:** the `links` table holds `(src_type, src_ref, dst_type, dst_ref, method, confidence)`. Three link shapes matter here, all present in real data: `commit→ticket` (`method` `key_regex` or `pr_inherited`), `pr→commit` (`method='merge_sha'`, so `dst_ref` is the commit sha and `src_ref` is the PR number as text), and `pr→ticket` (`key_regex` or `branch_regex`). `pr_commits(pr_number, sha)` is a **secondary** path: on the validation repo it joins only 2 of 8,103 rows because that repo squash-merges, so the PR's branch commits never land on the mainline. `merge_sha` joins 1,533 of 1,569 there. Both paths stay because other repos merge differently.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_archaeology.py`:

```python
from archeon.db import connect
from archeon.retrieval.archaeology import ArtifactRefs, artifacts_for_commits


def _lake(tmp_path):
    conn = connect(tmp_path / "e.db")
    for sha in ("sha_a", "sha_b"):
        conn.execute("INSERT INTO commits(sha, author, date, message) "
                     "VALUES (?, 'a', '2026-01-01', 'm')", (sha,))
    return conn


def _link(conn, st, sr, dt, dr, method):
    conn.execute("INSERT INTO links(src_type, src_ref, dst_type, dst_ref, "
                 "method, confidence) VALUES (?,?,?,?,?,1.0)",
                 (st, sr, dt, dr, method))


def test_direct_commit_to_ticket_links_resolve(tmp_path):
    conn = _lake(tmp_path)
    _link(conn, "commit", "sha_a", "ticket", "EMB-1", "key_regex")
    refs = artifacts_for_commits(conn, ["sha_a"])
    assert refs.tickets == {"EMB-1": {"sha_a"}}
    assert refs.prs == {}
    assert refs.unknown == set()


def test_pr_reached_via_merge_sha_and_its_ticket(tmp_path):
    conn = _lake(tmp_path)
    _link(conn, "pr", "42", "commit", "sha_a", "merge_sha")
    _link(conn, "pr", "42", "ticket", "EMB-9", "branch_regex")
    refs = artifacts_for_commits(conn, ["sha_a"])
    assert refs.prs == {42: {"sha_a"}}
    # The PR's ticket is inherited by the commit that reached the PR.
    assert refs.tickets == {"EMB-9": {"sha_a"}}


def test_pr_commits_secondary_path_also_reaches_the_pr(tmp_path):
    # Repos that do not squash-merge expose members via pr_commits instead.
    conn = _lake(tmp_path)
    conn.execute("INSERT INTO pr_commits(pr_number, sha) VALUES (7, 'sha_b')")
    refs = artifacts_for_commits(conn, ["sha_b"])
    assert refs.prs == {7: {"sha_b"}}


def test_support_accumulates_across_shaping_commits(tmp_path):
    conn = _lake(tmp_path)
    _link(conn, "commit", "sha_a", "ticket", "EMB-1", "key_regex")
    _link(conn, "commit", "sha_b", "ticket", "EMB-1", "pr_inherited")
    refs = artifacts_for_commits(conn, ["sha_a", "sha_b"])
    # Two commits reached EMB-1 -> support of 2, which drives corpus ranking.
    assert refs.tickets["EMB-1"] == {"sha_a", "sha_b"}


def test_commits_absent_from_the_lake_are_counted_not_dropped(tmp_path):
    conn = _lake(tmp_path)
    refs = artifacts_for_commits(conn, ["sha_a", "sha_missing"])
    assert refs.unknown == {"sha_missing"}


def test_empty_input_is_an_empty_result(tmp_path):
    refs = artifacts_for_commits(_lake(tmp_path), [])
    assert refs == ArtifactRefs(tickets={}, prs={}, unknown=set())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_archaeology.py -v -k "artifact or link or pr_commits or support or absent or empty_input"`
Expected: FAIL with `ImportError: cannot import name 'ArtifactRefs'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/archeon/retrieval/archaeology.py` (extend the import line to `from dataclasses import dataclass, field`):

```python
@dataclass
class ArtifactRefs:
    """Artifacts reached from a set of shaping commits.

    The dict values are the shas that reached each artifact; ``len`` of one
    is that artifact's *support*, which the corpus builder ranks by.
    """
    tickets: dict = field(default_factory=dict)   # key -> {sha}
    prs: dict = field(default_factory=dict)       # number -> {sha}
    unknown: set = field(default_factory=set)     # shas not in `commits`

    def _add(self, bucket: dict, key, sha: str) -> None:
        bucket.setdefault(key, set()).add(sha)


def artifacts_for_commits(conn, shas) -> ArtifactRefs:
    """Resolve shaping commits to ticket keys and PR numbers via `links`.

    Shas absent from `commits` are reported in `unknown` rather than dropped:
    span archaeology legitimately reaches commits outside the ingest scope
    (bot-filtered, or under a path prefix this component does not cover), and
    silently discarding them would overstate coverage.
    """
    refs = ArtifactRefs()
    for sha in dict.fromkeys(shas):        # dedupe, preserve order
        if conn.execute("SELECT 1 FROM commits WHERE sha=?",
                        (sha,)).fetchone() is None:
            refs.unknown.add(sha)
            continue
        for r in conn.execute(
                "SELECT dst_ref FROM links WHERE src_type='commit' "
                "AND src_ref=? AND dst_type='ticket'", (sha,)):
            refs._add(refs.tickets, r["dst_ref"], sha)
        # commit -> PR. merge_sha is the reliable path on squash-merging
        # repos; pr_commits covers repos that keep branch commits.
        pr_numbers = {r["src_ref"] for r in conn.execute(
            "SELECT src_ref FROM links WHERE src_type='pr' "
            "AND dst_type='commit' AND dst_ref=?", (sha,))}
        pr_numbers |= {str(r["pr_number"]) for r in conn.execute(
            "SELECT pr_number FROM pr_commits WHERE sha=?", (sha,))}
        for raw in pr_numbers:
            try:
                number = int(raw)
            except (TypeError, ValueError):
                continue
            refs._add(refs.prs, number, sha)
            # A ticket named by the PR corroborates the commit that reached it.
            for r in conn.execute(
                    "SELECT dst_ref FROM links WHERE src_type='pr' "
                    "AND src_ref=? AND dst_type='ticket'", (raw,)):
                refs._add(refs.tickets, r["dst_ref"], sha)
    return refs
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_archaeology.py -v`
Expected: PASS (15 tests)

- [ ] **Step 5: Commit**

```bash
git add src/archeon/retrieval/archaeology.py tests/test_archaeology.py
git commit -m "feat(archaeology): resolve shaping commits to tickets and PRs with support counts"
```

---

### Task 4: `[why]` config block

**Files:**
- Modify: `src/archeon/config.py:12-37`
- Test: `tests/test_retrieval_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `WHY_DEFAULTS: dict` and `config.why(config: dict) -> dict` with keys `max_commits_per_span`, `token_budget`, `model`. `model` defaults to `None`, meaning "fall back to `llm.expensive_model`, then `llm.cheap_model`".

**Background:** follow `retrieval()` exactly — the block stays out of `REQUIRED` so every existing config (and the P0/spike tests) keeps validating without a `[why]` section.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_retrieval_config.py`:

```python
from archeon import config as config_mod


def test_why_defaults_apply_when_block_absent():
    w = config_mod.why({})
    assert w["max_commits_per_span"] == 50
    assert w["token_budget"] == 40000
    assert w["model"] is None


def test_why_block_overrides_defaults():
    w = config_mod.why({"why": {"token_budget": 1000}})
    assert w["token_budget"] == 1000
    assert w["max_commits_per_span"] == 50      # untouched default


def test_why_is_not_required_for_config_validation(tmp_path):
    # A config with no [why] section must still load.
    p = tmp_path / "a.toml"
    p.write_text(
        '[component]\nname="c"\ndb="e.db"\nrepo_path="."\n'
        'path_prefixes=["src/"]\n'
        '[jira]\nbase_url="u"\nproject_keys=["A"]\n'
        '[prs]\nrepo="o/r"\n[wiki]\nexport_dir="d"\n'
        '[llm]\ncheap_model="m"\n')
    assert config_mod.why(config_mod.load(p))["token_budget"] == 40000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_retrieval_config.py -v -k why`
Expected: FAIL with `AttributeError: module 'archeon.config' has no attribute 'why'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/archeon/config.py` after `RETRIEVAL_DEFAULTS`:

```python
WHY_DEFAULTS = {
    "max_commits_per_span": 50,   # cap on git log -L archaeology per span
    "token_budget": 40000,        # artifact corpus budget per cluster
    "model": None,                # falls back to llm.expensive_model
}
```

And after `retrieval()`:

```python
def why(config: dict) -> dict:
    """Merge the optional [why] block over the code defaults.

    Kept out of REQUIRED, like [retrieval], so existing configs keep
    validating without a [why] section.
    """
    merged = dict(WHY_DEFAULTS)
    merged.update(config.get("why", {}))
    return merged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_retrieval_config.py tests/test_scoped_config_loads.py -v`
Expected: PASS

- [ ] **Step 5: Document the block in the example config**

Append to `archeon.example.toml`:

```toml
# [why]                                    # optional — these are the defaults
# max_commits_per_span = 50                # cap on git log -L archaeology per span
# token_budget = 40000                     # artifact corpus budget per cluster
# model = "claude-sonnet-5"                # falls back to llm.expensive_model
```

- [ ] **Step 6: Commit**

```bash
git add src/archeon/config.py tests/test_retrieval_config.py archeon.example.toml
git commit -m "feat(config): optional [why] block with defaults"
```

---

### Task 5: Artifact corpus builder

**Files:**
- Create: `src/archeon/claims/why_corpus.py`
- Test: `tests/test_why_corpus.py`

**Interfaces:**
- Consumes: `ArtifactRefs` and `artifacts_for_commits` (Task 3); `shaping_commits`, `file_level_commits` (Task 2); `archeon.retrieval.bundle.estimate_tokens(text) -> int`; `config.why()` (Task 4).
- Produces:
  - `spans_for_claims(claims) -> list[tuple[str, int, int, str]]` — `(path, start, end, rev)` per pinned code evidence, deduped.
  - `collect_artifacts(conn, repo_path, claims, why_cfg) -> ArtifactRefs`
  - `build_corpus(conn, refs, token_budget) -> tuple[str, list[dict]]` — the rendered corpus text and a manifest of `{"ref": str, "kind": str, "support": int}`.

**Background:** artifact refs are the stable strings grounding resolves later: a ticket is its bare key (`EMB-1`), a PR is `pr:42`, a PR review comment is `pr_comment:<id>`. Ranking is by support (how many shaping commits reached the artifact), breaking ties by the artifact's own timestamp newest-first — `tickets.resolved` falling back to `tickets.created`, `prs.merged_at`, `pr_comments.created` — with a missing timestamp sorting last. A PR's review comments inherit that PR's support so a heavily-discussed PR cannot crowd out other artifacts on comment count alone (`pr_comments` holds 6,663 rows on the validation repo). Reuse `estimate_tokens` from `bundle.py` rather than re-deriving it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_why_corpus.py`:

```python
from archeon.claims.schema import Claim, Evidence
from archeon.claims.why_corpus import (
    build_corpus, collect_artifacts, spans_for_claims)
from archeon.db import connect
from archeon.retrieval.archaeology import ArtifactRefs


def _pinned(ref, sha, start, end):
    return Evidence(kind="code", ref=ref, role="primary", commit_sha=sha,
                    line_start=start, line_end=end, pin_status="pinned")


def test_spans_come_from_pinned_code_evidence_only(tmp_path):
    c = Claim(id="CLM-0001", type="threshold", statement="s", evidence=[
        _pinned("src/a.c:5-9", "sha1", 5, 9),
        Evidence(kind="code", ref="src/b.c:1", pin_status="unpinnable"),
        Evidence(kind="ticket", ref="EMB-1", role="corroborating"),
    ])
    assert spans_for_claims([c]) == [("src/a.c", 5, 9, "sha1")]


def test_spans_are_deduped_across_claims():
    ev = _pinned("src/a.c:5-9", "sha1", 5, 9)
    a = Claim(id="CLM-0001", type="threshold", statement="s", evidence=[ev])
    b = Claim(id="CLM-0002", type="invariant", statement="t", evidence=[ev])
    assert spans_for_claims([a, b]) == [("src/a.c", 5, 9, "sha1")]


def _lake(tmp_path):
    conn = connect(tmp_path / "e.db")
    conn.execute("INSERT INTO tickets(key, summary, description, status, "
                 "created, resolved) VALUES ('EMB-1', 'Sum one', "
                 "'Why one happened', 'Done', '2026-01-01', '2026-02-01')")
    conn.execute("INSERT INTO tickets(key, summary, description, status, "
                 "created, resolved) VALUES ('EMB-2', 'Sum two', "
                 "'Why two happened', 'Done', '2026-01-01', '2026-03-01')")
    conn.execute("INSERT INTO prs(number, title, body, author, branch, "
                 "merged_at, merge_sha) VALUES (42, 'PR title', 'PR body', "
                 "'a', 'b', '2026-02-15', 'sha_m')")
    conn.execute("INSERT INTO pr_comments(id, pr_number, author, body, path, "
                 "created) VALUES ('c1', 42, 'a', 'Review rationale', "
                 "'src/a.c', '2026-02-16')")
    return conn


def test_corpus_ranks_by_support_then_recency(tmp_path):
    conn = _lake(tmp_path)
    refs = ArtifactRefs(tickets={"EMB-1": {"s1"}, "EMB-2": {"s1", "s2"}},
                        prs={}, unknown=set())
    _text, manifest = build_corpus(conn, refs, token_budget=10000)
    # EMB-2 has support 2 and outranks EMB-1's support 1.
    assert [m["ref"] for m in manifest] == ["EMB-2", "EMB-1"]
    assert manifest[0]["support"] == 2


def test_corpus_includes_pr_body_and_its_comments(tmp_path):
    conn = _lake(tmp_path)
    refs = ArtifactRefs(tickets={}, prs={42: {"s1"}}, unknown=set())
    text, manifest = build_corpus(conn, refs, token_budget=10000)
    kinds = {m["kind"] for m in manifest}
    assert kinds == {"pr", "pr_comment"}
    assert "pr:42" in {m["ref"] for m in manifest}
    assert "pr_comment:c1" in {m["ref"] for m in manifest}
    assert "PR body" in text and "Review rationale" in text


def test_pr_comment_inherits_its_prs_support(tmp_path):
    conn = _lake(tmp_path)
    refs = ArtifactRefs(tickets={}, prs={42: {"s1", "s2"}}, unknown=set())
    _text, manifest = build_corpus(conn, refs, token_budget=10000)
    by_ref = {m["ref"]: m for m in manifest}
    assert by_ref["pr_comment:c1"]["support"] == 2


def test_corpus_truncates_to_token_budget(tmp_path):
    conn = _lake(tmp_path)
    refs = ArtifactRefs(tickets={"EMB-1": {"s1"}, "EMB-2": {"s1", "s2"}},
                        prs={}, unknown=set())
    _text, manifest = build_corpus(conn, refs, token_budget=1)
    # Always emits at least the top-ranked artifact, never more here.
    assert [m["ref"] for m in manifest] == ["EMB-2"]


def test_missing_artifact_rows_are_skipped_not_fatal(tmp_path):
    conn = _lake(tmp_path)
    refs = ArtifactRefs(tickets={"NOPE-9": {"s1"}}, prs={999: {"s1"}},
                        unknown=set())
    text, manifest = build_corpus(conn, refs, token_budget=10000)
    assert manifest == []
    assert text == ""


def test_collect_artifacts_walks_spans_to_artifacts(tmp_path, monkeypatch):
    import archeon.claims.why_corpus as mod
    conn = _lake(tmp_path)
    conn.execute("INSERT INTO commits(sha, author, date, message) "
                 "VALUES ('sha_x', 'a', '2026-01-01', 'm')")
    conn.execute("INSERT INTO links(src_type, src_ref, dst_type, dst_ref, "
                 "method, confidence) VALUES "
                 "('commit','sha_x','ticket','EMB-1','key_regex',1.0)")
    monkeypatch.setattr(mod, "shaping_commits",
                        lambda *a, **k: ["sha_x"])
    c = Claim(id="CLM-0001", type="threshold", statement="s",
              evidence=[_pinned("src/a.c:5-9", "sha1", 5, 9)])
    refs = collect_artifacts(conn, tmp_path, [c],
                             {"max_commits_per_span": 50})
    assert refs.tickets == {"EMB-1": {"sha_x"}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_why_corpus.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'archeon.claims.why_corpus'`

- [ ] **Step 3: Write minimal implementation**

Create `src/archeon/claims/why_corpus.py`:

```python
"""Assemble the why-layer artifact corpus for a set of what-claims.

Pure git + SQL: no LLM anywhere in this module, so the whole retrieval
half of Pass 2 is testable without a model.
"""

from pathlib import Path

from archeon.retrieval.archaeology import (
    artifacts_for_commits, file_level_commits, shaping_commits)
from archeon.retrieval.bundle import estimate_tokens

# Sorts last when an artifact has no usable timestamp.
_NO_TS = ""


def spans_for_claims(claims) -> list:
    """Deduped (path, start, end, rev) for every pinned code evidence.

    Only pinned/dirty anchors carry trustworthy line numbers; anything else
    has no span to walk and is handled by the file-level fallback.
    """
    out, seen = [], set()
    for c in claims:
        for e in c.evidence:
            if e.kind != "code" or e.pin_status not in ("pinned", "dirty"):
                continue
            if not e.commit_sha or e.line_start is None or e.line_end is None:
                continue
            path = e.ref.rsplit(":", 1)[0]
            key = (path, e.line_start, e.line_end, e.commit_sha)
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def collect_artifacts(conn, repo_path, claims, why_cfg):
    """Walk every claim's pinned spans out to tickets and PRs."""
    cap = why_cfg.get("max_commits_per_span", 50)
    shas = []
    for path, start, end, rev in spans_for_claims(claims):
        found = shaping_commits(repo_path, path, start, end, rev=rev,
                                max_commits=cap)
        if not found:
            # Renamed, deleted, or otherwise unresolvable at that rev — fall
            # back to the file's own history rather than losing the claim.
            found = file_level_commits(repo_path, path, max_commits=cap)
        shas.extend(found)
    return artifacts_for_commits(conn, shas)


def _ticket_entries(conn, tickets: dict) -> list:
    out = []
    for key, shas in tickets.items():
        row = conn.execute(
            "SELECT key, summary, description, status, created, resolved "
            "FROM tickets WHERE key=?", (key,)).fetchone()
        if row is None:
            continue        # link points at a ticket never ingested
        body = f"{row['summary'] or ''}\n\n{row['description'] or ''}".strip()
        out.append({
            "ref": key, "kind": "ticket", "support": len(shas),
            "ts": row["resolved"] or row["created"] or _NO_TS,
            "text": f"=== ticket {key} ({row['status'] or 'unknown'}) ===\n"
                    f"{body}\n",
        })
    return out


def _pr_entries(conn, prs: dict) -> list:
    out = []
    for number, shas in prs.items():
        row = conn.execute(
            "SELECT number, title, body, merged_at FROM prs WHERE number=?",
            (number,)).fetchone()
        if row is None:
            continue
        body = f"{row['title'] or ''}\n\n{row['body'] or ''}".strip()
        out.append({
            "ref": f"pr:{number}", "kind": "pr", "support": len(shas),
            "ts": row["merged_at"] or _NO_TS,
            "text": f"=== pr {number} ===\n{body}\n",
        })
        # Review comments inherit the PR's support so a heavily-discussed PR
        # cannot crowd out other artifacts on comment count alone.
        for cm in conn.execute(
                "SELECT id, author, body, created FROM pr_comments "
                "WHERE pr_number=? ORDER BY created", (number,)):
            if not (cm["body"] or "").strip():
                continue
            out.append({
                "ref": f"pr_comment:{cm['id']}", "kind": "pr_comment",
                "support": len(shas), "ts": cm["created"] or _NO_TS,
                "text": f"=== pr_comment {cm['id']} on pr {number} "
                        f"(by {cm['author'] or 'unknown'}) ===\n"
                        f"{cm['body'].strip()}\n",
            })
    return out


def build_corpus(conn, refs, token_budget: int):
    """Render a token-bounded artifact corpus, best-supported first.

    Returns (text, manifest). The manifest's `ref` strings are exactly what
    the synthesizer must cite and what grounding resolves back.
    """
    entries = _ticket_entries(conn, refs.tickets) + \
        _pr_entries(conn, refs.prs)
    # Two stable passes, so the second key dominates: support DESC, then
    # timestamp DESC. An empty timestamp is the smallest string, so
    # reverse=True naturally sorts unknown-date artifacts last within their
    # support group, and the ref pass keeps full ties deterministic.
    entries.sort(key=lambda e: e["ref"])
    entries.sort(key=lambda e: (e["support"], e["ts"]), reverse=True)
    parts, manifest, total = [], [], 0
    for e in entries:
        t = estimate_tokens(e["text"])
        if manifest and total + t > token_budget:
            break
        parts.append(e["text"])
        manifest.append({"ref": e["ref"], "kind": e["kind"],
                         "support": e["support"]})
        total += t
    return "\n".join(parts), manifest
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_why_corpus.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add src/archeon/claims/why_corpus.py tests/test_why_corpus.py
git commit -m "feat(claims): token-bounded why-layer artifact corpus ranked by support"
```

---

### Task 6: Deterministic citation grounding

**Files:**
- Create: `src/archeon/claims/why.py`
- Test: `tests/test_why.py`

**Interfaces:**
- Consumes: `Claim`, `Evidence`, `CODE_INFERRED_MAX_CONFIDENCE` (Task 1).
- Produces: `ground_citations(claims, conn) -> dict` mutating claims in place and returning counts `{"grounded": int, "dropped": int, "code_inferred": int}`; plus `normalize_text(s) -> str` and `artifact_body(conn, ref) -> str | None`.

**Why this comes before synthesis:** it is the highest-value piece and entirely deterministic. A fabricated ticket quote dies here for free, with no model in the loop — the same philosophy as Spec B's content hashes. Build it first so synthesis is landing into a net that already works.

**Background:** grounding must normalize before comparing. Real PR bodies in the lake contain literal CRLF (`\r\n`), and models routinely reflow whitespace when quoting. Normalize by converting CRLF/CR to LF, collapsing every whitespace run to a single space, and casefolding. A claim that loses all artifact evidence keeps its code hypothesis and becomes `code_inferred`: confidence capped at `CODE_INFERRED_MAX_CONFIDENCE`, status left at `recovered`, never verified.

- [ ] **Step 1: Write the failing test**

Create `tests/test_why.py`:

```python
from archeon.claims.schema import (
    CODE_INFERRED_MAX_CONFIDENCE, Claim, Evidence)
from archeon.claims.why import artifact_body, ground_citations, normalize_text
from archeon.db import connect


def _lake(tmp_path):
    conn = connect(tmp_path / "e.db")
    conn.execute("INSERT INTO tickets(key, summary, description, status, "
                 "created, resolved) VALUES ('EMB-1', 'Smoothen puck', "
                 "'Statuses arrive irregularly, so the puck jitters.', "
                 "'Done', '2026-01-01', '2026-02-01')")
    # Real PR bodies in the lake contain literal CRLF. This MUST be bound as
    # a parameter: SQLite does not interpret backslash escapes inside a SQL
    # string literal, so an inline '...\r\n...' would store two literal
    # backslashes and would not exercise the CRLF path at all.
    conn.execute("INSERT INTO prs(number, title, body, author, branch, "
                 "merged_at, merge_sha) VALUES (42, 'Improve smoothness', "
                 "?, 'a', 'b', '2026-02-15', 'sha_m')",
                 ("Fixes jitter\r\non slow devices",))
    conn.execute("INSERT INTO pr_comments(id, pr_number, author, body, path, "
                 "created) VALUES ('c1', 42, 'a', 'We chose lerp for cost', "
                 "'src/a.c', '2026-02-16')")
    return conn


def _why(evidence, cid="WHY-0001"):
    return Claim(id=cid, type="rationale", statement="s", layer="why",
                 confidence=0.7, explains=["CLM-0001"], evidence=[
                     Evidence(kind="code", ref="src/a.c:5", role="primary"),
                     *evidence])


def test_normalize_text_folds_crlf_whitespace_and_case():
    assert normalize_text("Fixes jitter\r\non  slow\tdevices") == \
        normalize_text("fixes JITTER on slow devices")


def test_artifact_body_resolves_each_ref_kind(tmp_path):
    conn = _lake(tmp_path)
    assert "jitters" in artifact_body(conn, "EMB-1")
    assert "jitter" in artifact_body(conn, "pr:42")
    assert "lerp" in artifact_body(conn, "pr_comment:c1")
    assert artifact_body(conn, "NOPE-1") is None
    assert artifact_body(conn, "pr:999") is None
    assert artifact_body(conn, "garbage") is None


def test_verbatim_excerpt_is_grounded(tmp_path):
    conn = _lake(tmp_path)
    c = _why([Evidence(kind="ticket", ref="EMB-1", role="corroborating",
                       excerpt="Statuses arrive irregularly")])
    stats = ground_citations([c], conn)
    assert stats["grounded"] == 1 and stats["dropped"] == 0
    assert c.corroboration == "corroborated"
    assert c.confidence == 0.7          # untouched


def test_excerpt_surviving_crlf_and_reflow_is_grounded(tmp_path):
    conn = _lake(tmp_path)
    c = _why([Evidence(kind="pr", ref="pr:42", role="corroborating",
                       excerpt="Fixes jitter on slow devices")])
    ground_citations([c], conn)
    assert c.corroboration == "corroborated"


def test_fabricated_excerpt_is_dropped(tmp_path):
    conn = _lake(tmp_path)
    c = _why([Evidence(kind="ticket", ref="EMB-1", role="corroborating",
                       excerpt="The team agreed to a 50ms budget")])
    stats = ground_citations([c], conn)
    assert stats["dropped"] == 1
    assert [e.kind for e in c.evidence] == ["code"]   # only hypothesis left
    assert c.corroboration == "code_inferred"


def test_citation_to_a_missing_artifact_is_dropped(tmp_path):
    conn = _lake(tmp_path)
    c = _why([Evidence(kind="ticket", ref="NOPE-1", role="corroborating",
                       excerpt="anything")])
    ground_citations([c], conn)
    assert c.corroboration == "code_inferred"


def test_code_inferred_claim_is_capped_and_never_verified(tmp_path):
    conn = _lake(tmp_path)
    c = _why([])                     # no artifact evidence at all
    c.confidence = 0.9
    stats = ground_citations([c], conn)
    assert stats["code_inferred"] == 1
    assert c.corroboration == "code_inferred"
    assert c.confidence == CODE_INFERRED_MAX_CONFIDENCE
    assert c.status == "recovered"


def test_grounding_keeps_the_code_hypothesis_and_never_deletes_claims(
        tmp_path):
    conn = _lake(tmp_path)
    c = _why([Evidence(kind="ticket", ref="NOPE-1", role="corroborating",
                       excerpt="x")])
    ground_citations([c], conn)
    # The claim survives with its code evidence -> "no valid evidence" holds.
    assert any(e.kind == "code" and e.role == "primary" for e in c.evidence)


def test_partial_grounding_keeps_only_the_real_citation(tmp_path):
    conn = _lake(tmp_path)
    c = _why([
        Evidence(kind="ticket", ref="EMB-1", role="corroborating",
                 excerpt="Statuses arrive irregularly"),
        Evidence(kind="ticket", ref="EMB-1", role="corroborating",
                 excerpt="invented sentence"),
    ])
    stats = ground_citations([c], conn)
    assert stats["grounded"] == 1 and stats["dropped"] == 1
    assert c.corroboration == "corroborated"
    assert len([e for e in c.evidence if e.kind == "ticket"]) == 1


def test_empty_excerpt_cannot_ground(tmp_path):
    conn = _lake(tmp_path)
    c = _why([Evidence(kind="ticket", ref="EMB-1", role="corroborating",
                       excerpt="   ")])
    ground_citations([c], conn)
    assert c.corroboration == "code_inferred"


def test_what_layer_claims_are_left_alone(tmp_path):
    conn = _lake(tmp_path)
    what = Claim(id="CLM-0001", type="threshold", statement="s", layer="what",
                 status="machine_verified", confidence=0.9)
    ground_citations([what], conn)
    assert what.corroboration is None
    assert what.confidence == 0.9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_why.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'archeon.claims.why'`

- [ ] **Step 3: Write minimal implementation**

Create `src/archeon/claims/why.py`:

```python
"""Why-layer claim recovery (Pass 2).

Mirrors ``claims/recover.py``: prompts as module constants, an injected
``ask`` callable, and in-place mutation of Claim objects. The cost meter is
attached to the AgentClassifier by the CLI, so nothing here knows about it.
"""

import re

from archeon.claims.schema import CODE_INFERRED_MAX_CONFIDENCE

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_why.py -v`
Expected: PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
git add src/archeon/claims/why.py tests/test_why.py
git commit -m "feat(claims/why): deterministic citation grounding against the lake"
```

---

### Task 7: Why-claim synthesis

**Files:**
- Modify: `src/archeon/claims/why.py`
- Test: `tests/test_why.py`

**Interfaces:**
- Consumes: `WHY_CLAIM_TYPES`, `Claim`, `Evidence` (Task 1).
- Produces: `WHY_SYNTH_SYSTEM: str`, `WHY_SYNTH_PROMPT: str`, and `synthesize_why_claims(feature, what_claims, corpus, ask) -> list[Claim]`.

**Background:** the model emits **only** artifact evidence plus an `explains` list. Primary code evidence is copied mechanically off each explained what-claim — Spec B already pinned it and Pass 1 already verified it — so the model cannot invent a code ref at all. `explains` entries naming no supplied claim are dropped, and a claim left with an empty `explains` is discarded, since it would have no code hypothesis and no traceability. Reuse `_strip_fence` from `recover.py` rather than duplicating fence handling.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_why.py`:

```python
import json

from archeon.claims.why import synthesize_why_claims


def _what(cid="CLM-0001"):
    return Claim(id=cid, type="threshold", statement="Puck lerps to target",
                 layer="what", status="machine_verified", symbols=["lerp"],
                 evidence=[Evidence(kind="code", ref="src/a.c:5-9",
                                    role="primary", excerpt="lerp(a,b,t);",
                                    commit_sha="sha1", line_start=5,
                                    line_end=9, pin_status="pinned")])


def _ask_returning(payload):
    return lambda _prompt: json.dumps(payload)


def test_synthesis_builds_why_claims_with_artifact_evidence():
    claims = synthesize_why_claims("nav", [_what()], "corpus", _ask_returning([
        {"type": "rationale", "statement": "Lerp was chosen to stop jitter",
         "explains": ["CLM-0001"],
         "evidence": [{"ref": "EMB-1", "excerpt": "puck jitters"}]}]))
    assert len(claims) == 1
    c = claims[0]
    assert c.layer == "why" and c.type == "rationale"
    assert c.id == "WHY-0001"
    assert c.explains == ["CLM-0001"]
    art = [e for e in c.evidence if e.kind == "ticket"]
    assert art[0].role == "corroborating" and art[0].ref == "EMB-1"


def test_code_hypothesis_is_copied_from_the_explained_what_claim():
    claims = synthesize_why_claims("nav", [_what()], "corpus", _ask_returning([
        {"type": "intent", "statement": "s", "explains": ["CLM-0001"],
         "evidence": [{"ref": "EMB-1", "excerpt": "x"}]}]))
    code = [e for e in claims[0].evidence if e.kind == "code"]
    assert len(code) == 1
    # Copied verbatim off the what-claim, including Spec B's anchor.
    assert code[0].ref == "src/a.c:5-9"
    assert code[0].commit_sha == "sha1"
    assert code[0].role == "primary"


def test_evidence_ref_kinds_are_classified_from_the_ref_form():
    claims = synthesize_why_claims("nav", [_what()], "corpus", _ask_returning([
        {"type": "rationale", "statement": "s", "explains": ["CLM-0001"],
         "evidence": [{"ref": "EMB-1", "excerpt": "a"},
                      {"ref": "pr:42", "excerpt": "b"},
                      {"ref": "pr_comment:c1", "excerpt": "c"}]}]))
    kinds = {e.ref: e.kind for e in claims[0].evidence if e.kind != "code"}
    assert kinds == {"EMB-1": "ticket", "pr:42": "pr",
                     "pr_comment:c1": "pr_comment"}


def test_unknown_explains_ids_are_dropped():
    claims = synthesize_why_claims("nav", [_what()], "corpus", _ask_returning([
        {"type": "rationale", "statement": "s",
         "explains": ["CLM-0001", "CLM-9999"],
         "evidence": [{"ref": "EMB-1", "excerpt": "x"}]}]))
    assert claims[0].explains == ["CLM-0001"]


def test_claim_explaining_nothing_real_is_discarded():
    claims = synthesize_why_claims("nav", [_what()], "corpus", _ask_returning([
        {"type": "rationale", "statement": "s", "explains": ["CLM-9999"],
         "evidence": [{"ref": "EMB-1", "excerpt": "x"}]}]))
    assert claims == []


def test_unknown_why_type_is_rejected():
    claims = synthesize_why_claims("nav", [_what()], "corpus", _ask_returning([
        {"type": "threshold", "statement": "s", "explains": ["CLM-0001"],
         "evidence": []}]))
    assert claims == []


def test_unparsable_reply_yields_no_claims():
    assert synthesize_why_claims("nav", [_what()], "c",
                                 lambda _p: "not json") == []
    assert synthesize_why_claims("nav", [_what()], "c",
                                 lambda _p: '{"a": 1}') == []


def test_model_supplied_code_refs_are_ignored():
    # The model must not be able to introduce a code ref.
    claims = synthesize_why_claims("nav", [_what()], "corpus", _ask_returning([
        {"type": "rationale", "statement": "s", "explains": ["CLM-0001"],
         "evidence": [{"ref": "src/evil.c:1", "excerpt": "invented"}]}]))
    refs = {e.ref for e in claims[0].evidence}
    assert refs == {"src/a.c:5-9"}      # only the copied hypothesis


def test_ids_are_sequential_why_ids():
    payload = [
        {"type": "rationale", "statement": "a", "explains": ["CLM-0001"],
         "evidence": [{"ref": "EMB-1", "excerpt": "x"}]},
        {"type": "intent", "statement": "b", "explains": ["CLM-0001"],
         "evidence": [{"ref": "EMB-1", "excerpt": "y"}]}]
    claims = synthesize_why_claims("nav", [_what()], "c",
                                   _ask_returning(payload))
    assert [c.id for c in claims] == ["WHY-0001", "WHY-0002"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_why.py -v -k synthes`
Expected: FAIL with `ImportError: cannot import name 'synthesize_why_claims'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/archeon/claims/why.py` (extend imports with `import json`, `from archeon.claims.recover import _strip_fence`, and `from archeon.claims.schema import WHY_CLAIM_TYPES, Claim, Evidence`):

```python
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
    from dataclasses import replace
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_why.py -v`
Expected: PASS (21 tests)

- [ ] **Step 5: Commit**

```bash
git add src/archeon/claims/why.py tests/test_why.py
git commit -m "feat(claims/why): why-claim synthesis with a copied code hypothesis"
```

---

### Task 8: Adversarial why-claim verification

**Files:**
- Modify: `src/archeon/claims/why.py`
- Test: `tests/test_why.py`

**Interfaces:**
- Consumes: `ground_citations` (Task 6), `synthesize_why_claims` (Task 7).
- Produces: `WHY_VERIFY_SYSTEM: str`, `WHY_VERIFY_PROMPT: str`, and `verify_why_claims(claims, corpus, ask) -> None` mutating in place.

**Background:** this pass runs **only** on `corroboration == "corroborated"` claims — a code-inferred claim is never auto-verified, per the design. The failure mode to catch is *topical drift*: a genuine quote pulled from an artifact that discusses the same area but does not state this rationale. Grounding already guarantees the quote is real, so the verifier's only job is whether it *means* what the claim says. Mirror `verify_claims`' per-claim `try/except`: that guard exists because an uncaught error once discarded every prior claim's progress, since saving happens only after the loop returns.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_why.py`:

```python
from archeon.claims.why import verify_why_claims


def _corroborated(cid="WHY-0001"):
    c = Claim(id=cid, type="rationale", statement="s", layer="why",
              confidence=0.5, explains=["CLM-0001"],
              corroboration="corroborated", evidence=[
                  Evidence(kind="code", ref="src/a.c:5", role="primary"),
                  Evidence(kind="ticket", ref="EMB-1", role="corroborating",
                           excerpt="puck jitters")])
    return c


def test_supported_claim_becomes_machine_verified():
    c = _corroborated()
    verify_why_claims([c], "corpus",
                      lambda _p: '{"supported": true, "confidence": 0.85}')
    assert c.status == "machine_verified"
    assert c.confidence == 0.85
    assert c.corroboration == "corroborated"


def test_refuted_claim_becomes_contested_with_counter_evidence():
    c = _corroborated()
    verify_why_claims([c], "corpus", lambda _p: json.dumps(
        {"supported": False, "confidence": 0.2,
         "counter": "the ticket never states this"}))
    assert c.status == "contested"
    assert c.counter_evidence == ["the ticket never states this"]
    # Still corroborated: a real artifact was cited, it just does not support.
    assert c.corroboration == "corroborated"


def test_code_inferred_claims_are_never_verified():
    c = _corroborated()
    c.corroboration = "code_inferred"
    c.confidence = 0.4
    calls = []

    def ask(prompt):
        calls.append(prompt)
        return '{"supported": true, "confidence": 0.9}'

    verify_why_claims([c], "corpus", ask)
    assert calls == []                       # no model call at all
    assert c.status == "recovered"
    assert c.confidence == 0.4


def test_verification_failure_contests_that_claim_only():
    # The first claim verifies, the second's call raises. The first must keep
    # its result: an uncaught error here would lose all prior progress,
    # because saving happens only after the whole loop returns.
    good, bad = _corroborated("WHY-0001"), _corroborated("WHY-0002")
    calls = {"n": 0}

    def flaky(_prompt):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("backend down")
        return '{"supported": true, "confidence": 0.8}'

    verify_why_claims([good, bad], "corpus", flaky)
    assert good.status == "machine_verified"
    assert bad.status == "contested"
    assert bad.confidence == 0.0
    assert "backend down" in bad.counter_evidence[0]


def test_unparsable_verifier_reply_contests():
    c = _corroborated()
    verify_why_claims([c], "corpus", lambda _p: "garbage")
    assert c.status == "contested"
    assert c.confidence == 0.3


def test_what_layer_claims_are_not_touched_by_why_verification():
    what = Claim(id="CLM-0001", type="threshold", statement="s", layer="what",
                 status="machine_verified", confidence=0.9)
    verify_why_claims([what], "corpus", lambda _p: '{"supported": false}')
    assert what.status == "machine_verified"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_why.py -v -k verify`
Expected: FAIL with `ImportError: cannot import name 'verify_why_claims'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/archeon/claims/why.py`:

```python
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
            c.confidence = float(v.get("confidence", 0.8))
        else:
            c.status = "contested"
            c.confidence = float(v.get("confidence", 0.3)) if isinstance(
                v, dict) else 0.3
            counter = v.get("counter") if isinstance(v, dict) else None
            if counter:
                c.counter_evidence = [counter]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_why.py -v`
Expected: PASS (27 tests)

- [ ] **Step 5: Run the full why suite for regressions**

Run: `uv run pytest tests/test_why.py tests/test_claims_recover.py -v`
Expected: PASS — the new verification path must not disturb `recover.verify_claims`.

- [ ] **Step 6: Commit**

```bash
git add src/archeon/claims/why.py tests/test_why.py
git commit -m "feat(claims/why): adversarial verification that skips code-inferred claims"
```

---

### Task 9: Corroborated precision metric

**Files:**
- Modify: `src/archeon/claims/claim_eval.py:18-51`
- Test: `tests/test_claim_eval.py`

**Interfaces:**
- Consumes: `Claim.corroboration` (Task 1).
- Produces: `evaluate_claims` per-layer dicts gain `corroborated_n`, `corroborated_correct`, `corroborated_precision`.

**Background:** the gate is *corroborated* why-layer precision ≥ 0.80. It counts only claims whose rationale rests on a real artifact, so a large code-inferred tail cannot dilute or inflate it. A claim counts as corroborated when `corroboration == "corroborated"`, independent of `status` — the two axes are orthogonal.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_claim_eval.py`:

```python
from archeon.claims.claim_eval import evaluate_claims
from archeon.claims.schema import Claim


def _why(cid, corroboration, status="machine_verified"):
    return Claim(id=cid, type="rationale", statement="s", layer="why",
                 status=status, corroboration=corroboration)


def test_corroborated_precision_excludes_code_inferred():
    claims = [_why("WHY-0001", "corroborated"),
              _why("WHY-0002", "corroborated"),
              _why("WHY-0003", "code_inferred")]
    labels = {"WHY-0001": True, "WHY-0002": False, "WHY-0003": True}
    s = evaluate_claims(claims, labels)["why"]
    assert s["n"] == 3                       # all labeled claims
    assert s["corroborated_n"] == 2          # code-inferred excluded
    assert s["corroborated_correct"] == 1
    assert s["corroborated_precision"] == 0.5


def test_corroborated_counts_are_independent_of_status():
    # A contested-but-corroborated claim still counts in the denominator.
    claims = [_why("WHY-0001", "corroborated", status="contested"),
              _why("WHY-0002", "corroborated")]
    labels = {"WHY-0001": True, "WHY-0002": True}
    s = evaluate_claims(claims, labels)["why"]
    assert s["corroborated_n"] == 2
    assert s["corroborated_precision"] == 1.0
    assert s["verified_n"] == 1               # only one reached verified


def test_corroborated_precision_is_zero_when_none_corroborated():
    s = evaluate_claims([_why("WHY-0001", "code_inferred")],
                        {"WHY-0001": True})["why"]
    assert s["corroborated_n"] == 0
    assert s["corroborated_precision"] == 0.0


def test_what_layer_reports_zero_corroborated():
    what = Claim(id="CLM-0001", type="threshold", statement="s",
                 layer="what", status="machine_verified")
    s = evaluate_claims([what], {"CLM-0001": True})["what"]
    assert s["corroborated_n"] == 0
    assert s["precision"] == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_claim_eval.py -v -k corroborated`
Expected: FAIL with `KeyError: 'corroborated_n'`

- [ ] **Step 3: Write minimal implementation**

In `src/archeon/claims/claim_eval.py`, extend the `setdefault` initialiser:

```python
        s = by_layer.setdefault(c.layer, {"n": 0, "correct": 0,
                                          "verified_n": 0,
                                          "verified_correct": 0,
                                          "corroborated_n": 0,
                                          "corroborated_correct": 0})
```

Add inside the loop, after the `verified_n` block:

```python
        # Corroborated = rationale rests on a real artifact. Independent of
        # status, and the denominator the design's why-layer gate uses.
        if getattr(c, "corroboration", None) == "corroborated":
            s["corroborated_n"] += 1
            if correct:
                s["corroborated_correct"] += 1
```

Add to the final normalising loop:

```python
        s["corroborated_precision"] = (
            s["corroborated_correct"] / s["corroborated_n"]
            if s["corroborated_n"] else 0.0)
```

Extend the docstring's returns line to mention the new keys:

```python
    Returns {layer: {n, correct, precision, verified_n, verified_correct,
    verified_precision, corroborated_n, corroborated_correct,
    corroborated_precision}}. The corroborated_* numbers count only claims
    whose rationale rests on a real artifact — the design's why-layer gate
    denominator, kept separate so a large code-inferred tail can neither
    dilute nor inflate it.
    """
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_claim_eval.py -v`
Expected: PASS

- [ ] **Step 5: Surface the new numbers in the CLI**

In `src/archeon/cli.py`'s `cli_claims_eval`, change the gate constants line (currently `PRE_GATE, POST_GATE = 0.85, 0.95` at `cli.py:330`) to add the why-layer gate:

```python
    PRE_GATE, POST_GATE, CORROBORATED_GATE = 0.85, 0.95, 0.80
```

Then, inside the existing `for layer, s in sorted(result.items()):` loop, after the `if s["verified_n"]:` block, append:

```python
        # The why-layer's own gate: corroborated claims only, so a large
        # code-inferred tail can neither dilute nor inflate it. Printed
        # separately because the design bans a blended what+why number.
        if s["corroborated_n"]:
            corr_mark = "PASS" if \
                s["corroborated_precision"] >= CORROBORATED_GATE else "FAIL"
            click.echo(f"{layer}-layer precision (corroborated only): "
                       f"{s['corroborated_precision']:.3f} "
                       f"({s['corroborated_correct']}/{s['corroborated_n']}) "
                       f"[gate {CORROBORATED_GATE:.2f}: {corr_mark}]")

- [ ] **Step 6: Run the CLI tests**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/archeon/claims/claim_eval.py src/archeon/cli.py tests/test_claim_eval.py
git commit -m "feat(claim-eval): corroborated why-layer precision for the P1 gate"
```

---

### Task 10: `archeon why` command

**Files:**
- Modify: `src/archeon/cli.py` (add after `cli_synthesize`, which ends at line 288)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 1–9 — `config.why`, `collect_artifacts`, `build_corpus`, `synthesize_why_claims`, `ground_citations`, `verify_why_claims`, `load_claims`, `save_claims`, `pin_claims`, `CostMeter`, `AgentClassifier`.
- Produces: the `why` CLI command and `<out_dir>/why_cost.json`.

**Background — the three constraints that matter most here:**
1. Both hard-fails happen **before** any classifier is constructed, so a misconfigured run costs nothing.
2. Write `why_cost.json`, **never** `run_cost.json` — `cli_synthesize` owns that filename in the same directory and clobbering it would destroy the what-layer run's cost record.
3. Use one `CostMeter()` shared by both classifiers, with `stage="why-synth"` / `stage="why-verify"`, and one `summary_dict("why")` feeding both the echoed block and the JSON file so the two surfaces cannot disagree about the billing route.

Claims are grouped by their `feature` field, which `synthesize` sets from the cluster label (`cli.py:257` passes `label` into `synthesize_claims`, and `recover.py:86` stores it as `feature`) — so no new join is needed and `--feature` runs group correctly too.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py` (match the existing file's `CliRunner` conventions):

```python
import json

import yaml
from click.testing import CliRunner

from archeon.cli import main
from archeon.db import connect


def _cfg(tmp_path, db_path, repo):
    p = tmp_path / "a.toml"
    p.write_text(
        f'[component]\nname="c"\ndb="{db_path.as_posix()}"\n'
        f'repo_path="{repo.as_posix()}"\npath_prefixes=["src/"]\n'
        '[jira]\nbase_url="u"\nproject_keys=["EMB"]\n'
        '[prs]\nrepo="o/r"\n[wiki]\nexport_dir="d"\n'
        '[llm]\ncheap_model="m"\n')
    return p


def _claim_file(claims_dir, cid="CLM-0001"):
    claims_dir.mkdir(parents=True, exist_ok=True)
    (claims_dir / f"{cid}.yaml").write_text(yaml.safe_dump({
        "id": cid, "type": "threshold", "statement": "s", "feature": "nav",
        "layer": "what", "status": "machine_verified", "confidence": 0.9,
        "symbols": ["f"], "evidence": [
            {"kind": "code", "ref": "src/a.c:1-2", "role": "primary",
             "excerpt": "x", "commit_sha": "sha1", "blob_sha": None,
             "line_start": 1, "line_end": 2, "content_hash": "h",
             "pin_status": "pinned"}],
        "counter_evidence": [], "corroboration": None, "explains": [],
    }, sort_keys=False))


def test_why_hard_fails_when_the_lake_has_no_artifacts(tmp_path):
    db = tmp_path / "e.db"
    connect(db)                              # empty lake
    repo = tmp_path / "repo"
    repo.mkdir()
    claims = tmp_path / "claims"
    _claim_file(claims)
    r = CliRunner().invoke(main, [
        "why", "--config", str(_cfg(tmp_path, db, repo)),
        "--claims", str(claims)])
    assert r.exit_code != 0
    assert "ingest-prs" in r.output


def test_why_hard_fails_when_no_claims_exist(tmp_path):
    db = tmp_path / "e.db"
    conn = connect(db)
    conn.execute("INSERT INTO tickets(key, summary) VALUES ('EMB-1', 's')")
    conn.commit()
    repo = tmp_path / "repo"
    repo.mkdir()
    r = CliRunner().invoke(main, [
        "why", "--config", str(_cfg(tmp_path, db, repo)),
        "--claims", str(tmp_path / "missing")])
    assert r.exit_code != 0
    assert "synthesize" in r.output


def test_why_preserves_an_existing_run_cost_json(tmp_path):
    db = tmp_path / "e.db"
    conn = connect(db)
    conn.execute("INSERT INTO tickets(key, summary) VALUES ('EMB-1', 's')")
    conn.commit()
    repo = tmp_path / "repo"
    repo.mkdir()                 # deliberately NOT a git repo
    claims = tmp_path / "claims"
    _claim_file(claims)
    # synthesize's cost report already lives in this directory.
    (claims / "run_cost.json").write_text('{"command": "synthesize"}')

    r = CliRunner().invoke(main, [
        "why", "--config", str(_cfg(tmp_path, db, repo)),
        "--claims", str(claims)])

    # No LLM is stubbed because none is reached: `repo` is not a git repo, so
    # archaeology yields no shaping commits, the corpus is empty, and the
    # group is skipped before any classifier is constructed. That is exactly
    # the "never invent a rationale without artifacts" path.
    assert r.exit_code == 0, r.output
    assert "groups without artifacts: 1" in r.output
    # synthesize's report survives untouched, and why wrote its own.
    assert json.loads((claims / "run_cost.json").read_text())["command"] == \
        "synthesize"
    assert json.loads(
        (claims / "why_cost.json").read_text())["command"] == "why"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v -k why`
Expected: FAIL with `No such command 'why'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/archeon/cli.py` after `cli_synthesize`:

```python
@main.command("why")
@config_option
@click.option("--claims", "claims_dir", default="claims", show_default=True,
              help="directory of what-layer claim YAML to enrich")
@click.option("--feature", "feature", default=None,
              help="only enrich claims whose feature label matches")
def cli_why(config_path, claims_dir, feature):
    """Recover why-layer claims (Pass 2) from tickets and PRs.

    Walks each what-claim's commit-pinned span back through git history to
    the commits that shaped it, resolves those to tickets and PRs, then
    synthesizes, mechanically grounds, and adversarially verifies the
    rationale. Run `synthesize` and the ingest commands first.
    """
    from archeon.claims.pin import pin_claims
    from archeon.claims.schema import load_claims, save_claims
    from archeon.claims.why import (
        WHY_SYNTH_SYSTEM, WHY_VERIFY_SYSTEM, ground_citations,
        synthesize_why_claims, verify_why_claims)
    from archeon.claims.why_corpus import build_corpus, collect_artifacts
    from archeon.cost import CostMeter
    from archeon.llm import AgentClassifier

    cfg, conn = _load(config_path)
    why_cfg = config_mod.why(cfg)
    repo = Path(cfg["component"]["repo_path"])
    out = Path(claims_dir)

    # Both preconditions are checked before any classifier is built, so a
    # misconfigured run cannot spend anything.
    if not out.is_dir() or not any(out.glob("*.yaml")):
        raise click.ClickException(
            f"no claim YAML in {out}/; run `synthesize` first")
    tickets = conn.execute("SELECT COUNT(*) AS n FROM tickets").fetchone()["n"]
    prs = conn.execute("SELECT COUNT(*) AS n FROM prs").fetchone()["n"]
    if not tickets and not prs:
        raise click.ClickException(
            "evidence lake has no tickets or PRs; the why-layer has nothing "
            "to corroborate against — run `ingest-git`, `ingest-prs` and "
            "`ingest-jira` first")

    existing = load_claims(out)
    what = [c for c in existing if c.layer == "what"]
    if feature:
        what = [c for c in what if c.feature == feature]
    if not what:
        raise click.ClickException("no what-layer claims to enrich")

    model = why_cfg["model"] or cfg["llm"].get("expensive_model",
                                               cfg["llm"]["cheap_model"])
    meter = CostMeter()
    groups: dict = {}
    for c in what:
        groups.setdefault(c.feature or "(unlabelled)", []).append(c)

    all_why, unknown_shas, uncorroborated = [], set(), 0
    for label, members in sorted(groups.items()):
        refs = collect_artifacts(conn, repo, members, why_cfg)
        unknown_shas |= refs.unknown
        corpus, manifest = build_corpus(conn, refs, why_cfg["token_budget"])
        if not manifest:
            uncorroborated += 1
            continue        # no artifacts: never invent a rationale
        try:
            claims = synthesize_why_claims(
                label, members, corpus,
                AgentClassifier(model, WHY_SYNTH_SYSTEM, max_turns=4,
                                meter=meter, stage="why-synth").ask)
        except Exception as e:              # noqa: BLE001
            click.echo(f"warning: why-synthesis failed for {label}: {e}",
                       err=True)
            continue        # keep every other group's results
        ground_citations(claims, conn)
        verify_why_claims(
            claims, corpus,
            AgentClassifier(model, WHY_VERIFY_SYSTEM, max_turns=4,
                            meter=meter, stage="why-verify").ask)
        all_why.extend(claims)

    # Re-id uniquely across groups, then pin the copied code hypotheses.
    for i, c in enumerate(all_why, 1):
        c.id = f"WHY-{i:04d}"
    known_paths = [r["path"] for r in
                   conn.execute("SELECT DISTINCT path FROM symbols")]
    pin_claims(all_why, repo, known_paths=known_paths)
    save_claims(all_why, out)

    verified = sum(1 for c in all_why if c.status == "machine_verified")
    inferred = sum(1 for c in all_why if c.corroboration == "code_inferred")
    click.echo(f"why-claims: {len(all_why)}  machine_verified: {verified}  "
               f"code_inferred: {inferred}  -> {out}/")
    click.echo(f"  groups without artifacts: {uncorroborated}  "
               f"commits outside the lake: {len(unknown_shas)}")
    # One summary_dict feeds both surfaces so they share a single
    # billing-route probe. NOT run_cost.json: synthesize owns that name here.
    cost_summary = meter.summary_dict("why")
    click.echo(meter.format_summary("why", cost_summary))
    (out / "why_cost.json").write_text(json.dumps(cost_summary, indent=2),
                                       encoding="utf-8")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v -k why`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS, no regressions

- [ ] **Step 6: Commit**

```bash
git add src/archeon/cli.py tests/test_cli.py
git commit -m "feat(cli): archeon why runs why-layer recovery with cost accounting"
```

---

### Task 11: Runbook documentation

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the `why` command (Task 10) and the corroborated metric (Task 9).
- Produces: no code.

- [ ] **Step 1: Add the why-layer section**

Insert into `README.md` after the "P1 spike: recover what-layer claims from code" section:

```markdown
## P1: recover why-layer claims from artifacts (Pass 2)

The why-layer — intent and rationale — cannot be settled by code. `why` walks
each what-claim's commit-pinned span back through git history to the commits
that shaped it, resolves those to their PRs and tickets, and uses those
artifacts as corroborating evidence. It needs the artifact ingest commands to
have run, not just `scan`:

    uv run archeon ingest-git
    uv run archeon ingest-prs
    uv run archeon ingest-jira
    uv run archeon synthesize --all-clusters --out claims
    uv run archeon why --claims claims
    # writes WHY-*.yaml beside the CLM-*.yaml, plus why_cost.json

Each why-claim cites at least one ticket or PR excerpt, and its quote is
checked **mechanically** against the stored artifact text before any model
judges it — a fabricated citation is dropped without an LLM call. A claim left
with no surviving artifact keeps its code hypothesis, is marked
`corroboration: code_inferred`, capped at confidence 0.4, and is never
auto-verified.

Measure the gate (corroborated why-layer precision >= 0.80) by labeling the
why-claims in a CSV (`claim_id,correct`) and reusing `claims-eval`:

    uv run archeon claims-eval --claims claims --labels why_labels.csv

Label the *rationale*, not whether the cited artifact exists — grounding
already guarantees existence. A labels file containing only `WHY-` ids reports
the why layer alone, even though the directory holds both layers.

Optional `[why]` config: `max_commits_per_span`, `token_budget`, `model`.
```

- [ ] **Step 2: Verify the documented commands match the implementation**

Run: `uv run archeon why --help`
Expected: help text lists `--config`, `--claims`, `--feature`

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): why-layer Pass 2 runbook and gate"
```

---

### Task 12: Validation run and the gate

**Files:**
- Create: `docs/research/2026-07-25-p1-why-layer-validation.md`
- Create: `why_labels.csv` (gitignored working file if the repo ignores label CSVs; otherwise commit it)

**Interfaces:**
- Consumes: the whole pipeline.
- Produces: the measured corroborated why-layer precision.

**Background:** the scoped DB holds the clusters from the A+B acceptance run but has **zero** artifacts (0 commits, 0 PRs, 0 tickets), so the ingest commands must run against the scoped config first. The full-scope DB already has 2,046 commits, 1,569 PRs, 6,663 PR comments, and 757 tickets, which is the evidence this run depends on.

- [ ] **Step 1: Ingest artifacts into the scoped DB**

```bash
uv run archeon ingest-git --config archeon.example.scoped.toml
```

Then PRs and Jira:

```bash
uv run archeon ingest-prs --config archeon.example.scoped.toml
```

```bash
uv run archeon ingest-jira --config archeon.example.scoped.toml
```

- [ ] **Step 2: Confirm the lake is populated**

```bash
sqlite3 motor-ctrl-scoped.db "select 'commits',count(*) from commits union all select 'prs',count(*) from prs union all select 'tickets',count(*) from tickets;"
```

Expected: all three non-zero. If PRs or tickets are zero, stop — `why` will hard-fail by design.

- [ ] **Step 3: Link commits to tickets**

```bash
uv run archeon link --config archeon.example.scoped.toml
```

- [ ] **Step 4: Run the why stage**

```bash
uv run archeon why --config archeon.example.scoped.toml --claims claims_scoped
```

Record the printed counts and the cost block.

- [ ] **Step 5: Hand-label the why-claims**

Read each `claims_scoped/WHY-*.yaml` and write `why_labels.csv`:

```csv
claim_id,correct
WHY-0001,yes
WHY-0002,no
```

Judge only whether the stated rationale is true of the code and supported by the cited artifact. Do not credit a claim for citing a real artifact that does not state its reason — that is exactly the topical drift the gate exists to catch.

- [ ] **Step 6: Measure the gate**

```bash
uv run archeon claims-eval --claims claims_scoped --labels why_labels.csv
```

Expected: a `why` layer block including `corroborated: <n>/<d> = <precision>`. The gate is ≥ 0.800.

- [ ] **Step 7: Write up the findings**

Create `docs/research/2026-07-25-p1-why-layer-validation.md` following the structure of `docs/research/2026-07-25-p1-hardening-ab-acceptance-motor-ctrl.md`: the config and commands used, the counts (`why-claims`, `machine_verified`, `code_inferred`, groups without artifacts, commits outside the lake), the measured corroborated precision against the 0.80 gate, the observed cost from `why_cost.json`, and every defect found while labeling with its claim id.

State the outcome honestly. If the gate fails, record the precision and the failure patterns rather than adjusting the gate.

- [ ] **Step 8: Commit**

```bash
git add docs/research/2026-07-25-p1-why-layer-validation.md why_labels.csv
git commit -m "docs(research): why-layer Pass 2 validation run and gate measurement"
```

---

## Plan Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §4.1 archaeology (`shaping_commits`, `file_level_commits`) | 2 |
| §4.1 `ArtifactRefs`, `artifacts_for_commits`, merge_sha vs pr_commits | 3 |
| §4.2 corpus, support ranking, timestamp tie-break, PR-comment support inheritance | 5 |
| §4.3 synthesis, copied code hypothesis, `explains` filtering | 7 |
| §4.3 grounding (CRLF/whitespace normalization) | 6 |
| §4.3 adversarial verification | 8 |
| §5 data flow, three status/corroboration outcomes | 6, 8, 10 |
| §6 all degradation rows | 2 (git failures), 3 (unknown shas), 5 (missing rows), 6 (grounding), 8 (verify failure), 10 (hard-fails, per-group isolation) |
| §7 schema fields | 1 |
| §8 testing | every task |
| §9 gate metric + procedure | 9, 12 |
| §10 config | 4 |
| §11 cost (stages, `why_cost.json`, no-meter-param) | 10 |
| §12 rollout order | task order matches |

**Type consistency:** `ArtifactRefs(tickets, prs, unknown)` is defined in Task 3 and consumed with those exact field names in Tasks 5 and 10. `build_corpus` returns `(text, manifest)` in Task 5 and is unpacked as such in Task 10. `ground_citations` returns a stats dict in Task 6 and its return value is intentionally unused in Task 10. `why_cfg` keys `max_commits_per_span` / `token_budget` / `model` are defined in Task 4 and read in Tasks 5 and 10. `stage` strings are `why-synth` / `why-verify` in both Task 10 and the Global Constraints.

**Note for the implementer:** Task 5's two-pass sort relies on Python's sort stability and on the connectors storing fixed-format ISO-8601 timestamps (`jira_connector._normalize_ts`), so lexicographic order equals chronological order. It is not a general-purpose date comparator.
