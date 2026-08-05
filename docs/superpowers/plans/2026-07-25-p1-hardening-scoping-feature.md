# P1 Hardening — Scoping Glob Filters + `--feature` Prefix-Bounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `include`/`exclude` glob filters to the scan config and bound `synthesize --feature` to the requested prefix, then validate on the scoped motor-ctrl config.

**Architecture:** Filtering happens once at scan (in `scan_component`), so it flows through embed/cluster/synthesize which all read the `symbols` table — no downstream changes. `--feature` gets a new prefix-scoped bundler (`bundle_for_prefix`) that mirrors `bundle_for_cluster` but is bounded to symbols under the prefix, replacing the old "bundle every overlapping cluster whole" path.

**Tech Stack:** Python 3.13+, `click`, `numpy`, `sqlite3`, `pytest`, stdlib `pathlib.PurePosixPath.full_match` for glob matching.

**Spec:** [docs/superpowers/specs/2026-07-25-p1-hardening-scoping-feature-design.md](../specs/2026-07-25-p1-hardening-scoping-feature-design.md)

## Global Constraints

- `requires-python = ">=3.13"` — required for `PurePosixPath.full_match`. Bump from `>=3.12` in `pyproject.toml`.
- Glob matching: `PurePosixPath(rel).full_match(glob)` against the repo-root-relative POSIX path. No new dependency.
- `include`/`exclude` stay **out of** `config.REQUIRED` — existing configs with no filters must keep loading and scan identically.
- Keep semantics: file kept iff `(include empty OR matches any include) AND (matches no exclude)`. **Exclude wins.**
- TDD: failing test first, minimal implementation, frequent commits.

---

### Task 1: Scan `include`/`exclude` glob filters

**Files:**
- Modify: `pyproject.toml` (bump `requires-python`)
- Modify: `src/archaeon/codegraph/scan.py` (add `_keep`, extend `scan_component` signature + walk loop)
- Modify: `src/archaeon/cli.py:117-119` (pass filters from config)
- Test: `tests/test_scan_filters.py` (new)

**Interfaces:**
- Produces: `scan_component(conn, root, path_prefixes, compile_db_dir, include=None, exclude=None)` — `include`/`exclude` are `list[str] | None`; `None` ⇒ no filtering (identical to today).
- Produces: `_keep(rel: str, include: list[str] | None, exclude: list[str] | None) -> bool` (module-private helper in `scan.py`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scan_filters.py`:

```python
from archaeon.codegraph.scan import scan_component, _keep
from archaeon.db import connect

FN = "int {name}_fn(void) {{ return 1; }}\n"


def _tree(root):
    for rel in ("modules/nav/src/impl.cpp",
                "modules/nav/include/api.hpp",
                "modules/nav/generated/public/cpp/model.hpp",
                "modules/nav/generated/public/cpp/model.cpp",
                "thirdparty/imgui/demo.cpp",
                "tests/t.cpp"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(FN.format(name=rel.split("/")[-1].split(".")[0]))


def _paths(conn):
    return {r["path"] for r in conn.execute("SELECT DISTINCT path FROM symbols")}


def test_keep_predicate_exclude_wins_over_include():
    assert _keep("a/generated/x.cpp", ["**/*.cpp"], ["**/generated/**/*.cpp"]) is False
    assert _keep("a/src/x.cpp", ["**/*.cpp"], ["**/generated/**/*.cpp"]) is True
    assert _keep("a/src/x.hpp", ["**/*.cpp"], None) is False  # include-gated out
    assert _keep("a/src/x.cpp", None, None) is True           # no filters


def test_exclude_drops_generated_cpp_thirdparty_tests(tmp_path):
    root = tmp_path / "repo"
    _tree(root)
    conn = connect(tmp_path / "e.db")
    scan_component(conn, root, ["modules/", "thirdparty/", "tests/"],
                   compile_db_dir=None,
                   exclude=["**/generated/**/*.cpp", "**/thirdparty/**",
                            "**/tests/**"])
    paths = _paths(conn)
    assert "modules/nav/src/impl.cpp" in paths
    assert "modules/nav/include/api.hpp" in paths
    assert "modules/nav/generated/public/cpp/model.hpp" in paths
    assert "modules/nav/generated/public/cpp/model.cpp" not in paths
    assert "thirdparty/imgui/demo.cpp" not in paths
    assert "tests/t.cpp" not in paths


def test_include_gate_keeps_only_headers(tmp_path):
    root = tmp_path / "repo"
    _tree(root)
    conn = connect(tmp_path / "e.db")
    scan_component(conn, root, ["modules/"], compile_db_dir=None,
                   include=["**/*.hpp"])
    paths = _paths(conn)
    assert all(p.endswith(".hpp") for p in paths)
    assert "modules/nav/include/api.hpp" in paths


def test_no_filters_matches_unfiltered_scan(tmp_path):
    root = tmp_path / "repo"
    _tree(root)
    a = connect(tmp_path / "a.db")
    b = connect(tmp_path / "b.db")
    scan_component(a, root, ["modules/"], compile_db_dir=None)
    scan_component(b, root, ["modules/"], compile_db_dir=None,
                   include=None, exclude=None)
    assert _paths(a) == _paths(b)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_scan_filters.py -v`
Expected: FAIL — `ImportError: cannot import name '_keep'` (and `scan_component` rejecting `include`/`exclude` kwargs).

- [ ] **Step 3: Implement `_keep` and thread filters through `scan_component`**

In `src/archaeon/codegraph/scan.py`, add the import and helper near the top (after existing imports):

```python
from pathlib import PurePosixPath


def _keep(rel: str, include: list[str] | None,
          exclude: list[str] | None) -> bool:
    p = PurePosixPath(rel)
    if include and not any(p.full_match(g) for g in include):
        return False
    if exclude and any(p.full_match(g) for g in exclude):
        return False
    return True
```

Change the `scan_component` signature to:

```python
def scan_component(conn: sqlite3.Connection, root: Path,
                   path_prefixes: list[str],
                   compile_db_dir: Path | None,
                   include: list[str] | None = None,
                   exclude: list[str] | None = None) -> dict:
```

Inside the walk loop, after computing `rel` and before any insert, skip filtered files. Replace the body starting at the `for f in sorted(...)` block with:

```python
        for f in sorted(p for p in base.rglob("*")
                        if p.suffix in SOURCE_SUFFIXES and p.is_file()):
            rel = f.relative_to(root).as_posix()
            if not _keep(rel, include, exclude):
                continue
            if compile_db_dir is not None:
                try:
                    _insert_symbols(conn, rel,
                                    clang_symbols(f, compile_db_dir), "clang")
                    stats["clang"] += 1
                    continue
                except RuntimeError:
                    pass  # fall through to tree-sitter
            try:
                _insert_symbols(conn, rel, ts_symbols(f), "tree-sitter")
                stats["tree_sitter"] += 1
            except (ValueError, OSError) as e:
                conn.execute(
                    "INSERT OR REPLACE INTO scan_gaps(path, reason) "
                    "VALUES (?, ?)", (rel, str(e)))
                stats["gaps"] += 1
```

- [ ] **Step 4: Wire filters from the CLI**

In `src/archaeon/cli.py`, change the `cli_scan` body (lines 115-119) to pass the optional filters:

```python
    cfg, conn = _load(config_path)
    compile_db = cfg["component"].get("compile_db_dir")
    stats = scan_component(conn, Path(cfg["component"]["repo_path"]),
                           cfg["component"]["path_prefixes"],
                           Path(compile_db) if compile_db else None,
                           include=cfg["component"].get("include"),
                           exclude=cfg["component"].get("exclude"))
```

- [ ] **Step 5: Bump `requires-python`**

In `pyproject.toml`, change:

```toml
requires-python = ">=3.13"
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_scan_filters.py tests/test_scan_merge.py -v`
Expected: PASS (new filter tests + existing scan tests all green — confirms backward compatibility).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/archaeon/codegraph/scan.py src/archaeon/cli.py tests/test_scan_filters.py
git commit -m "feat(scan): include/exclude glob filters for scan scoping

Adds optional [component] include/exclude glob lists, matched via
PurePosixPath.full_match (needs py>=3.13). Filtering happens once at scan
so it flows through embed/cluster/synthesize. Exclude wins; filters
absent = unchanged behavior. Closes finding 4 mechanism gap.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Apply the exclude glob to the scoped config

**Files:**
- Modify: `archaeon.example.scoped.toml` (add `exclude`, rewrite caveat header)
- Test: `tests/test_scoped_config_loads.py` (new)

**Interfaces:**
- Consumes: `config.load` (existing) and the `include`/`exclude` keys from Task 1.

- [ ] **Step 1: Write the failing test**

Create `tests/test_scoped_config_loads.py`:

```python
from pathlib import Path

from archaeon import config as config_mod


def test_scoped_config_loads_with_exclude():
    cfg = config_mod.load(Path("archaeon.example.scoped.toml"))
    excl = cfg["component"].get("exclude")
    assert excl is not None
    assert "**/generated/**/*.cpp" in excl
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_scoped_config_loads.py -v`
Expected: FAIL — `assert None is not None` (no `exclude` key yet).

- [ ] **Step 3: Add the exclude glob and rewrite the caveat**

In `archaeon.example.scoped.toml`, add inside the `[component]` block, immediately after the `path_prefixes = [ ... ]` list closes:

```toml
# Glob exclude (scan include/exclude filters, py>=3.13). Drops the ~267
# mechanical toCpp/toJava converter .cpp files that generated/public/cpp/
# otherwise drags in, plus defense-in-depth against vendored/test leakage.
exclude = [
  "**/generated/**/*.cpp",
  "**/thirdparty/**",
  "**/tests/**",
]
```

Then rewrite the `KNOWN CAVEAT` paragraph in the header comment (the block starting `# KNOWN CAVEAT: path_prefixes are directory-prefix only ...`) to:

```toml
# NOTE: generated/public/cpp/ still drags in mechanical toCpp/toJava .cpp
# converters by directory prefix, but the `exclude` glob below now removes
# them (previously an unavoidable ~267-symbol caveat). Scope is ~6k symbols.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_scoped_config_loads.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add archaeon.example.scoped.toml tests/test_scoped_config_loads.py
git commit -m "config(motor-ctrl): exclude glob drops generated .cpp converters

Uses the new scan exclude filter to remove the ~267 mechanical converter
.cpp files; rewrites the now-resolved directory-prefix caveat.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `bundle_for_prefix` + `--feature` prefix-bounding

**Files:**
- Modify: `src/archaeon/retrieval/bundle.py` (add `bundle_for_prefix`)
- Modify: `src/archaeon/cli.py:242-265` (rewrite `--feature` branch)
- Test: `tests/test_bundle.py` (add `bundle_for_prefix` test)

**Interfaces:**
- Consumes: `symbol_rows(conn, repo_path, prefix=...)`, `load_vectors(conn, model, dims)` (dict `id -> np.ndarray`), `rank_symbols`, `pack_symbols` (all existing in `bundle.py`).
- Produces: `bundle_for_prefix(conn, repo_path, prefix, retr) -> (bundle_str, manifest)` — same return shape as `bundle_for_cluster`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bundle.py`:

```python
def test_bundle_for_prefix_bounds_to_prefix_not_cluster(tmp_path):
    from archaeon.db import connect
    from archaeon import config as config_mod
    from archaeon.retrieval.bundle import bundle_for_prefix

    (tmp_path / "nav").mkdir()
    (tmp_path / "vendor").mkdir()
    (tmp_path / "nav" / "a.c").write_text(
        "int in1(void){return 1;}\nint in2(void){return 2;}\n")
    (tmp_path / "vendor" / "b.c").write_text(
        "int out(void){return 3;}\n")
    conn = connect(tmp_path / "e.db")

    def ins(name, path, line):
        conn.execute(
            "INSERT INTO symbols(name,kind,path,line,end_line,signature,source)"
            " VALUES (?, 'function',?,?,?, '', 'tree-sitter')",
            (name, path, line, line))
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    in1 = ins("in1", "nav/a.c", 1)
    in2 = ins("in2", "nav/a.c", 2)
    out = ins("out", "vendor/b.c", 1)

    # A mega-cluster that spans the prefix AND vendored code.
    cur = conn.execute(
        "INSERT INTO clusters(component,label,candidate_types) "
        "VALUES('demo','mega','')")
    cid = cur.lastrowid
    conn.executemany(
        "INSERT INTO cluster_members(cluster_id,symbol_id) VALUES (?,?)",
        [(cid, in1), (cid, in2), (cid, out)])
    conn.commit()

    retr = config_mod.retrieval({"retrieval": {"embed_model": "m",
                                               "embed_dims": 2}})
    bundle, manifest = bundle_for_prefix(conn, tmp_path, "nav/", retr)

    ids = {e["id"] for e in manifest}
    assert ids == {in1, in2}          # bounded to prefix, not the cluster
    assert out not in ids
    assert "in1" in bundle and "out" not in bundle
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bundle.py::test_bundle_for_prefix_bounds_to_prefix_not_cluster -v`
Expected: FAIL — `ImportError: cannot import name 'bundle_for_prefix'`.

- [ ] **Step 3: Implement `bundle_for_prefix`**

Add to `src/archaeon/retrieval/bundle.py` (after `bundle_for_cluster`):

```python
def bundle_for_prefix(conn, repo_path, prefix, retr):
    """Bundle exactly the symbols under `prefix`, ranked by their own centroid.

    Prefix-faithful: never expands past the prefix (unlike bundling whole
    clusters that merely overlap it). Degrades to scan order when no vectors.
    """
    rows = symbol_rows(conn, repo_path, prefix=prefix)
    vectors = load_vectors(conn, retr["embed_model"], retr["embed_dims"])
    vecs = [vectors[r["id"]] for r in rows if r["id"] in vectors]
    centroid = np.mean(np.vstack(vecs), axis=0) if vecs else None
    ranked = rank_symbols(rows, vectors, centroid)
    return pack_symbols(ranked, retr["token_budget"])
```

Add the import at the top of `bundle.py` (it already imports `symbol_rows`; confirm `symbol_rows` and `load_vectors` are both imported — they are). No other import change needed.

- [ ] **Step 4: Run the unit test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bundle.py -v`
Expected: PASS (new test + all existing bundle tests green).

- [ ] **Step 5: Rewrite the `--feature` branch in the CLI**

In `src/archaeon/cli.py`, update the import line (currently line 216-217) to include the new helper:

```python
    from archaeon.retrieval.bundle import (
        bundle_for_cluster, bundle_for_prefix, pack_symbols, rank_symbols)
```

Replace the `else:` branch that resolves overlapping clusters (currently lines 242-250):

```python
    else:
        targets = [(feature, None)]
```

And replace the per-target bundling for the `cid is None` case (currently lines 257-265) so `--feature` uses `bundle_for_prefix`:

```python
    for label, cid in targets:
        if cid is not None:
            bundle, _ = bundle_for_cluster(conn, repo, cid, retr)
        else:
            bundle, _ = bundle_for_prefix(conn, repo, feature, retr)
            if not bundle:
                raise click.ClickException(
                    "no parsed files under that prefix; run scan first")
```

Note: `symbol_rows` may no longer be needed in the import at line 218 if it was only used by the old fallback — leave the import if any other code in the function uses it; otherwise remove it. Verify with a grep before removing.

- [ ] **Step 6: Run the CLI synthesize tests**

Run: `.venv/bin/python -m pytest tests/ -k "synth or bundle" -v`
Expected: PASS. If a synthesize CLI test asserted the old overlap-cluster behavior, update it to assert the bundle is bounded to the prefix (the spec's intended behavior).

- [ ] **Step 7: Commit**

```bash
git add src/archaeon/retrieval/bundle.py src/archaeon/cli.py tests/test_bundle.py
git commit -m "fix(synthesize): --feature bounds bundle to the prefix

Replaces 'bundle every overlapping cluster whole' (which ballooned
--feature navigator/ to the vendored mega-clusters) with a prefix-scoped
bundler ranked by the prefix symbols' own centroid. Closes finding 3.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Scoped validation re-run + research write-up

**Files:**
- Create: `docs/research/2026-07-25-p1-hardening-scoped-rerun.md`

**Interfaces:**
- Consumes: the scoped config (Task 2), the filtered scan (Task 1), the bounded `--feature` (Task 3).

This task is a manual, documented run — no unit test. Each step records real output into the research doc.

- [ ] **Step 1: Confirm the 4b embedding model is available**

Run: `ollama list | grep qwen3-embedding`
Expected: `qwen3-embedding:4b` present. If only `0.6b` is present, run `ollama pull qwen3-embedding:4b` first. Ensure the scoped config's `[retrieval]` block is either default (4b) or explicitly set to 4b with `embed_dims` matching that model.

- [ ] **Step 2: Run the scoped pipeline**

Run each and capture symbol/cluster counts:

```bash
.venv/bin/archaeon scan --config archaeon.example.scoped.toml
.venv/bin/archaeon embed --config archaeon.example.scoped.toml
.venv/bin/archaeon cluster --config archaeon.example.scoped.toml
```

Expected signals: symbol count near ~5.7k (the ~267 generated `.cpp` now excluded vs the earlier ~6,003), and clusters that are **not** 3 mega-clusters holding ~99% of symbols.

- [ ] **Step 3: Synthesize the navigator slice with the bounded `--feature`**

Run (use the navigator prefix under the scoped modules, e.g. `projects/motor-ctrl/modules/motor/src/controller/`):

```bash
.venv/bin/archaeon synthesize --config archaeon.example.scoped.toml \
  --feature projects/motor-ctrl/modules/motor/src/controller/ \
  --out claims_scoped
```

Expected: the bundle is bounded to the navigator prefix's symbols (no vendored/generated expansion); record claim count + machine_verified/contested split.

- [ ] **Step 4: Baseline staleness (0-stale) then a live stale catch**

```bash
.venv/bin/archaeon check-staleness --config archaeon.example.scoped.toml --claims claims_scoped
```

Then edit one pinned source line in the real checkout (a line cited by a synthesized claim), re-run `check-staleness`, and confirm it flags exactly the affected claim(s) as stale. **Revert the edit afterward** (`git -C /work/monorepo checkout -- <file>`).

- [ ] **Step 5: Write the research doc**

Create `docs/research/2026-07-25-p1-hardening-scoped-rerun.md` with: the pipeline table (symbols/clusters/claims), whether findings 2–4 are now closed (cluster quality under scoped 4b, `--feature` bounding), the live stale-catch evidence (before/after `check-staleness` output), and inspection notes on a handful of claims. Cross-link the acceptance run and this plan's spec.

- [ ] **Step 6: Commit**

```bash
git add docs/research/2026-07-25-p1-hardening-scoped-rerun.md
git commit -m "docs(research): scoped 4b re-run validates findings 2-4 + live staleness

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Part A (include/exclude globs, `_keep`, scan signature, CLI wiring, requires-python bump, config payoff) → Tasks 1 + 2. ✅
- Part B (`bundle_for_prefix`, `--feature` rewrite) → Task 3. ✅
- Part C (scoped 4b run, live staleness, research doc, inspection-only) → Task 4. ✅
- Testing section (scan filters incl. backward-compat, `bundle_for_prefix` prefix-bounding, existing cluster tests green) → Tasks 1 & 3. ✅

**Placeholder scan:** No TBD/TODO; every code step shows full code; the navigator prefix in Task 4 is a real runtime path, not an unresolved decision.

**Type consistency:** `scan_component(..., include=None, exclude=None)` and `_keep(rel, include, exclude)` consistent across Tasks 1 and its tests. `bundle_for_prefix(conn, repo_path, prefix, retr)` returns `(bundle_str, manifest)` matching `bundle_for_cluster`'s shape, consistent between Task 3 impl, test, and CLI call site.
