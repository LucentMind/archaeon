# P1 Hardening A — Retrieval + Clustering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the spike's whole-file inlining with a hybrid graph + embedding pipeline that clusters a component's symbols into feature areas and packs a token-bounded, relevance-ranked source bundle per area, leaving the synthesize/verify path unchanged.

**Architecture:** Scan gains symbol→symbol reference edges and file→file include edges, plus a local-Ollama embedding index over each symbol's `signature + source`. A clusterer builds one weighted symbol graph (references + file coupling/includes + embedding cosine) and runs greedy-modularity community detection to produce feature-area clusters, each labelled by a cheap-model pass. A new bundle builder ranks a cluster's member symbols against the cluster centroid and packs whole-symbol, line-numbered spans up to a token budget. Everything degrades to graph-only when Ollama is unreachable.

**Tech Stack:** Python 3.12, SQLite (additive schema), `requests` (Ollama `/api/embed`), `numpy` (vectors/cosine), `networkx` (community detection), `click` (CLI), `pytest`, Claude Agent SDK (existing `AgentClassifier` for cheap-model cluster labels).

## Global Constraints

- **Python** `>=3.12` (matches `pyproject.toml`).
- **Local-first embeddings only.** Vectors come from the local Ollama HTTP endpoint (`POST {endpoint}/api/embed`). No hosted embedding API, no key, no in-process `sentence-transformers`/torch dependency. Use `requests` (already a dependency) for HTTP.
- **Embedding models:** primary `qwen3-embedding:4b`; CI/low-resource fallback `qwen3-embedding:0.6b`. Both configurable.
- **Vectors are not interchangeable across model sizes.** `symbol_vectors` records the producing `(model, dims)`; embedding is idempotent per `(model, dims)` and re-runs only when either changes.
- **All schema changes are additive** — new tables only, never alter/drop existing ones.
- **The downstream synthesize/verify path (`synthesize_claims`, `verify_claims`) is unchanged.** This track only changes how the bundle and the feature-area list are produced.
- **Graceful degradation is a tested path, never a crash.** Ollama unreachable or model missing → graph-only retrieval + clustering, logged as a coverage note.
- **Config** lives in a new optional `[retrieval]` block in `archaeon.toml`, mirroring `[llm]`. It has code defaults and is NOT added to `config.REQUIRED` (existing configs and tests must keep working).
- **Symbols table does not store code text.** The `symbols.source` column holds the scanner name (`"clang"`/`"tree-sitter"`), not source. A symbol's code is read from disk via `repo_path / path` sliced to `[line, end_line]`. All retrieval code uses the shared loader from Task 2 for this.
- **TDD, DRY, YAGNI, frequent commits.** One test-cycle per task; commit at the end of each task.

**Ambiguity resolved (spec §4.1 vs §8):** the spec lists `includes` as *file→file* edges but the `symbol_edges` schema uses integer `src_id`/`dst_id` (symbol ids), which cannot hold file paths. This plan stores symbol→symbol `references` in `symbol_edges` (per spec) and file→file `includes` in a new additive `file_edges(src_path, dst_path, kind, weight)` table. The clusterer folds both `file_edges` (includes) and the existing `coupling` table into the graph as file-level signals. This is the one deviation from the spec's four-table list; it is additive and breaks nothing.

**Run tests with:** `uv run pytest <path> -v`. Run the full suite with `uv run pytest -q` before the final commit of each task.

---

## File Structure

**New files**
- `src/archaeon/codegraph/symsource.py` — shared loader: symbol rows joined with their on-disk source text (Task 2).
- `src/archaeon/codegraph/edges.py` — reference + include edge extraction (Task 3).
- `src/archaeon/retrieval/__init__.py` — new package (Task 4).
- `src/archaeon/retrieval/embed.py` — Ollama embedding index + cosine helpers (Task 4).
- `src/archaeon/retrieval/cluster.py` — weighted graph + community detection + cheap-model labels (Task 5).
- `src/archaeon/retrieval/bundle.py` — token-bounded, ranked, whole-symbol bundle builder (Task 6).
- Tests: `tests/test_symsource.py`, `tests/test_edges.py`, `tests/test_embed.py`, `tests/test_cluster.py`, `tests/test_bundle.py`, `tests/test_retrieval_config.py`.

**Modified files**
- `src/archaeon/schema.sql` — five additive tables (Task 1).
- `src/archaeon/config.py` — `retrieval()` accessor + defaults (Task 1).
- `pyproject.toml` — add `numpy`, `networkx` (Task 1).
- `src/archaeon/codegraph/scan.py` — call `extract_edges` at end of `scan_component` (Task 3).
- `src/archaeon/cli.py` — new `embed` and `cluster` commands; rework `synthesize` (Task 7).
- `archaeon.example.toml` — document `[retrieval]` (Task 7).

**Unchanged (do not touch the logic)**
- `src/archaeon/claims/recover.py` — `synthesize_claims` / `verify_claims` stay exactly as-is. `build_feature_bundle` also stays (legacy file-level packer; its test must keep passing). The new symbol-level packer lives in `retrieval/bundle.py`.

---

## Task 1: Additive schema, retrieval config, dependencies

**Files:**
- Modify: `src/archaeon/schema.sql` (append five tables)
- Modify: `src/archaeon/config.py`
- Modify: `pyproject.toml:6-15` (dependencies)
- Test: `tests/test_retrieval_config.py`, extend `tests/test_db.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - Tables `symbol_edges(src_id INTEGER, dst_id INTEGER, kind TEXT, weight REAL, PK(src_id,dst_id,kind))`, `file_edges(src_path TEXT, dst_path TEXT, kind TEXT, weight REAL, PK(src_path,dst_path,kind))`, `symbol_vectors(symbol_id INTEGER, model TEXT, dims INTEGER, vec BLOB, PK(symbol_id,model,dims))`, `clusters(id INTEGER PK AUTOINCREMENT, component TEXT, label TEXT, candidate_types TEXT)`, `cluster_members(cluster_id INTEGER, symbol_id INTEGER, PK(cluster_id,symbol_id))`.
  - `config.retrieval(cfg: dict) -> dict` returning the merged retrieval settings. Keys and defaults: `embed_model="qwen3-embedding:4b"`, `embed_endpoint="http://localhost:11434"`, `embed_dims=1024`, `token_budget=60000`, `w_references=1.0`, `w_includes=0.5`, `w_coupling=0.5`, `w_embedding=1.0`, `sim_top_k=10`, `max_cross_file_pairs=400`.
  - `config.RETRIEVAL_DEFAULTS: dict` (the defaults above).

- [ ] **Step 1: Add numpy + networkx to dependencies**

Edit `pyproject.toml`, the `dependencies` list (currently `pyproject.toml:6-15`), adding two entries:

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
    "numpy>=2",
    "networkx>=3",
]
```

- [ ] **Step 2: Sync the environment**

Run: `uv sync`
Expected: resolves and installs `numpy` and `networkx` with no error.

- [ ] **Step 3: Write the failing config test**

Create `tests/test_retrieval_config.py`:

```python
from archaeon import config as config_mod


def test_retrieval_defaults_when_absent():
    r = config_mod.retrieval({})
    assert r["embed_model"] == "qwen3-embedding:4b"
    assert r["embed_endpoint"] == "http://localhost:11434"
    assert r["embed_dims"] == 1024
    assert r["token_budget"] == 60000
    assert r["sim_top_k"] == 10


def test_retrieval_overrides_merge_over_defaults():
    r = config_mod.retrieval(
        {"retrieval": {"embed_model": "qwen3-embedding:0.6b", "embed_dims": 256}})
    assert r["embed_model"] == "qwen3-embedding:0.6b"
    assert r["embed_dims"] == 256
    # untouched keys keep their defaults
    assert r["token_budget"] == 60000
    assert r["w_references"] == 1.0
```

- [ ] **Step 4: Run it to verify it fails**

Run: `uv run pytest tests/test_retrieval_config.py -v`
Expected: FAIL with `AttributeError: module 'archaeon.config' has no attribute 'retrieval'`.

- [ ] **Step 5: Implement the config accessor**

Edit `src/archaeon/config.py`, adding after the `REQUIRED` dict (do not add `retrieval` to `REQUIRED`):

```python
RETRIEVAL_DEFAULTS = {
    "embed_model": "qwen3-embedding:4b",
    "embed_endpoint": "http://localhost:11434",
    "embed_dims": 1024,
    "token_budget": 60000,
    "w_references": 1.0,
    "w_includes": 0.5,
    "w_coupling": 0.5,
    "w_embedding": 1.0,
    "sim_top_k": 10,
    "max_cross_file_pairs": 400,
}


def retrieval(config: dict) -> dict:
    """Merge the optional [retrieval] block over the code defaults.

    Kept out of REQUIRED so existing configs (and the P0/spike tests) keep
    validating without a [retrieval] section; embeddings degrade to graph-only
    at runtime anyway.
    """
    merged = dict(RETRIEVAL_DEFAULTS)
    merged.update(config.get("retrieval", {}))
    return merged
```

- [ ] **Step 6: Run the config test to verify it passes**

Run: `uv run pytest tests/test_retrieval_config.py -v`
Expected: PASS (2 passed).

- [ ] **Step 7: Add the additive schema**

Append to `src/archaeon/schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS symbol_edges(
  src_id INTEGER,
  dst_id INTEGER,
  kind TEXT,
  weight REAL,
  PRIMARY KEY (src_id, dst_id, kind)
);
CREATE TABLE IF NOT EXISTS file_edges(
  src_path TEXT,
  dst_path TEXT,
  kind TEXT,
  weight REAL,
  PRIMARY KEY (src_path, dst_path, kind)
);
CREATE TABLE IF NOT EXISTS symbol_vectors(
  symbol_id INTEGER,
  model TEXT,
  dims INTEGER,
  vec BLOB,
  PRIMARY KEY (symbol_id, model, dims)
);
CREATE TABLE IF NOT EXISTS clusters(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  component TEXT,
  label TEXT,
  candidate_types TEXT
);
CREATE TABLE IF NOT EXISTS cluster_members(
  cluster_id INTEGER,
  symbol_id INTEGER,
  PRIMARY KEY (cluster_id, symbol_id)
);
```

- [ ] **Step 8: Write the schema test**

Add to `tests/test_db.py` (create the file if the assertion style below doesn't already exist there; if it exists, append this function):

```python
from archaeon.db import connect


def test_retrieval_tables_created(tmp_path):
    conn = connect(tmp_path / "e.db")
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("symbol_edges", "file_edges", "symbol_vectors",
              "clusters", "cluster_members"):
        assert t in names
```

- [ ] **Step 9: Run the schema test**

Run: `uv run pytest tests/test_db.py -v`
Expected: PASS (including the existing `test_db.py` cases).

- [ ] **Step 10: Run the full suite to confirm nothing regressed**

Run: `uv run pytest -q`
Expected: all pre-existing tests still pass (the added `[retrieval]` is optional, so `config.load` and the CLI tests are unaffected).

- [ ] **Step 11: Commit**

```bash
git add pyproject.toml uv.lock src/archaeon/schema.sql src/archaeon/config.py tests/test_retrieval_config.py tests/test_db.py
git commit -m "feat(retrieval): additive schema, [retrieval] config defaults, numpy+networkx deps"
```

---

## Task 2: Shared symbol-source loader

The `symbols` table stores `path`, `line`, `end_line` but not code text. Every retrieval unit (edges, embed, bundle) needs each symbol's actual source. Centralize that read here so line-slicing (an off-by-one hazard) is written and tested once.

**Files:**
- Create: `src/archaeon/codegraph/symsource.py`
- Test: `tests/test_symsource.py`

**Interfaces:**
- Consumes: `symbols` table (Task 1 unchanged existing table).
- Produces: `symbol_rows(conn, repo_path, prefix: str | None = None) -> list[dict]`. Each dict has keys `id: int`, `name: str`, `kind: str`, `path: str`, `line: int`, `end_line: int`, `signature: str`, `text: str`. `text` is the file content sliced `[line, end_line]` inclusive (1-based), joined with `\n`; `""` when the file is missing. `prefix` filters `path LIKE prefix%` with `_`/`%`/`\` escaped.

- [ ] **Step 1: Write the failing test**

Create `tests/test_symsource.py`:

```python
from archaeon.codegraph.symsource import symbol_rows
from archaeon.db import connect


def _insert(conn, name, path, line, end_line):
    conn.execute(
        "INSERT INTO symbols(name, kind, path, line, end_line, signature, "
        "source) VALUES (?, 'function', ?, ?, ?, '', 'tree-sitter')",
        (name, path, line, end_line))


def test_symbol_rows_slices_source_text(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.c").write_text(
        "line1\nint f(void) {\n  return 1;\n}\nline5\n")
    conn = connect(tmp_path / "e.db")
    _insert(conn, "f", "src/a.c", 2, 4)
    rows = symbol_rows(conn, tmp_path)
    assert len(rows) == 1
    assert rows[0]["name"] == "f"
    assert rows[0]["text"] == "int f(void) {\n  return 1;\n}"


def test_symbol_rows_missing_file_yields_empty_text(tmp_path):
    conn = connect(tmp_path / "e.db")
    _insert(conn, "g", "src/gone.c", 1, 2)
    rows = symbol_rows(conn, tmp_path)
    assert rows[0]["text"] == ""


def test_symbol_rows_prefix_filter_escapes_underscore(tmp_path):
    (tmp_path / "lib_a").mkdir()
    (tmp_path / "libxa").mkdir()
    (tmp_path / "lib_a" / "a.c").write_text("int a(void){return 1;}\n")
    (tmp_path / "libxa" / "a.c").write_text("int b(void){return 2;}\n")
    conn = connect(tmp_path / "e.db")
    _insert(conn, "a", "lib_a/a.c", 1, 1)
    _insert(conn, "b", "libxa/a.c", 1, 1)
    rows = symbol_rows(conn, tmp_path, prefix="lib_a/")
    assert [r["name"] for r in rows] == ["a"]  # libxa/ not matched
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_symsource.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'archaeon.codegraph.symsource'`.

- [ ] **Step 3: Implement the loader**

Create `src/archaeon/codegraph/symsource.py`:

```python
from pathlib import Path


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace(
        "_", "\\_")
    return escaped + "%"


def symbol_rows(conn, repo_path, prefix: str | None = None) -> list[dict]:
    """Symbols joined with their on-disk source text.

    The symbols table stores path + line range but not code (the `source`
    column is the scanner name). Read each file once and slice [line,end_line].
    """
    repo_path = Path(repo_path)
    sql = ("SELECT id, name, kind, path, line, end_line, signature "
           "FROM symbols")
    params: tuple = ()
    if prefix:
        sql += " WHERE path LIKE ? ESCAPE '\\'"
        params = (_like_prefix(prefix),)
    file_lines: dict[str, list[str] | None] = {}
    out: list[dict] = []
    for r in conn.execute(sql, params):
        path = r["path"]
        if path not in file_lines:
            f = repo_path / path
            file_lines[path] = (
                f.read_text(encoding="utf-8", errors="replace").splitlines()
                if f.is_file() else None)
        lines = file_lines[path]
        if lines is None:
            text = ""
        else:
            text = "\n".join(lines[r["line"] - 1:r["end_line"]])
        out.append({
            "id": r["id"], "name": r["name"], "kind": r["kind"],
            "path": path, "line": r["line"], "end_line": r["end_line"],
            "signature": r["signature"] or "", "text": text})
    return out
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_symsource.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/archaeon/codegraph/symsource.py tests/test_symsource.py
git commit -m "feat(codegraph): shared symbol-source loader (path+line -> code text)"
```

---

## Task 3: Reference + include edge extraction

**Files:**
- Create: `src/archaeon/codegraph/edges.py`
- Modify: `src/archaeon/codegraph/scan.py` (call `extract_edges` at the end of `scan_component`)
- Test: `tests/test_edges.py`

**Interfaces:**
- Consumes: `symbol_rows` (Task 2); `symbols` table; `symbol_edges`/`file_edges` tables (Task 1).
- Produces: `extract_edges(conn, repo_path) -> dict` with keys `references: int`, `includes: int` (counts inserted). Full rebuild: deletes all rows from `symbol_edges` and `file_edges` first (the DB is per-component, so a full rebuild is correct and simpler than prefix-scoped deletes). `references` edges are symbol→symbol with `kind='references'`, `weight` = identifier-occurrence count. `file_edges` are file→file with `kind='includes'`, `weight=1.0`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_edges.py`:

```python
from archaeon.codegraph.edges import extract_edges
from archaeon.db import connect


def _insert(conn, name, path, line, end_line):
    conn.execute(
        "INSERT INTO symbols(name, kind, path, line, end_line, signature, "
        "source) VALUES (?, 'function', ?, ?, ?, '', 'tree-sitter')",
        (name, path, line, end_line))
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def test_reference_edges_match_calls_not_unknown_identifiers(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.c").write_text(
        "int helper(void) { return 1; }\n"                 # lines 1
        "int caller(void) { return helper() + external(); }\n")  # line 2
    conn = connect(tmp_path / "e.db")
    hid = _insert(conn, "helper", "src/a.c", 1, 1)
    cid = _insert(conn, "caller", "src/a.c", 2, 2)

    stats = extract_edges(conn, tmp_path)

    edges = conn.execute(
        "SELECT src_id, dst_id, kind, weight FROM symbol_edges").fetchall()
    # caller -> helper exists; the unknown identifier `external` makes no edge
    assert (cid, hid) in {(e["src_id"], e["dst_id"]) for e in edges}
    assert all(not (e["src_id"] == e["dst_id"]) for e in edges)  # no self-edge
    assert stats["references"] >= 1


def test_include_edges_are_file_to_file(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "util.h").write_text("int u(void);\n")
    (tmp_path / "src" / "a.c").write_text(
        '#include "util.h"\nint u(void) { return 1; }\n')
    conn = connect(tmp_path / "e.db")
    _insert(conn, "u", "src/util.h", 1, 1)
    _insert(conn, "u", "src/a.c", 2, 2)

    extract_edges(conn, tmp_path)

    fe = conn.execute(
        "SELECT src_path, dst_path, kind FROM file_edges").fetchall()
    assert ("src/a.c", "src/util.h", "includes") in {
        (e["src_path"], e["dst_path"], e["kind"]) for e in fe}


def test_extract_edges_is_a_full_rebuild(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.c").write_text(
        "int helper(void){return 1;}\nint caller(void){return helper();}\n")
    conn = connect(tmp_path / "e.db")
    _insert(conn, "helper", "src/a.c", 1, 1)
    _insert(conn, "caller", "src/a.c", 2, 2)
    extract_edges(conn, tmp_path)
    n1 = conn.execute("SELECT COUNT(*) AS c FROM symbol_edges").fetchone()["c"]
    extract_edges(conn, tmp_path)  # rerun must not double-count
    n2 = conn.execute("SELECT COUNT(*) AS c FROM symbol_edges").fetchone()["c"]
    assert n1 == n2 and n1 >= 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_edges.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'archaeon.codegraph.edges'`.

- [ ] **Step 3: Implement edge extraction**

Create `src/archaeon/codegraph/edges.py`:

```python
import re
from pathlib import Path

from archaeon.codegraph.symsource import symbol_rows

_IDENT = re.compile(r"[A-Za-z_]\w*")
_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE)


def extract_edges(conn, repo_path) -> dict:
    """Full rebuild of the code-graph edges for the whole component.

    references (symbol->symbol): identifiers used inside a symbol's source that
    resolve to another known symbol name (tree-sitter identifier match; works
    on both clang- and ts-scanned symbols since it reads the stored source).
    includes (file->file): `#include` directives resolved by basename to a
    known scanned file. Unresolved identifiers/includes are simply skipped.
    """
    repo_path = Path(repo_path)
    rows = symbol_rows(conn, repo_path)
    name_to_ids: dict[str, list[int]] = {}
    for r in rows:
        name_to_ids.setdefault(r["name"], []).append(r["id"])

    conn.execute("DELETE FROM symbol_edges")
    conn.execute("DELETE FROM file_edges")

    ref_edges: dict[tuple[int, int], float] = {}
    for r in rows:
        counts: dict[int, int] = {}
        for tok in _IDENT.findall(r["text"]):
            if tok == r["name"]:
                continue
            for dst in name_to_ids.get(tok, ()):
                if dst == r["id"]:
                    continue
                counts[dst] = counts.get(dst, 0) + 1
        for dst, w in counts.items():
            ref_edges[(r["id"], dst)] = float(w)
    conn.executemany(
        "INSERT OR REPLACE INTO symbol_edges(src_id, dst_id, kind, weight) "
        "VALUES (?, ?, 'references', ?)",
        [(s, d, w) for (s, d), w in ref_edges.items()])

    paths = {r["path"] for r in rows}
    base_to_paths: dict[str, list[str]] = {}
    for p in paths:
        base_to_paths.setdefault(Path(p).name, []).append(p)
    inc_edges: set[tuple[str, str]] = set()
    for p in paths:
        f = repo_path / p
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for inc in _INCLUDE.findall(text):
            for dst in base_to_paths.get(Path(inc).name, ()):
                if dst != p:
                    inc_edges.add((p, dst))
    conn.executemany(
        "INSERT OR REPLACE INTO file_edges(src_path, dst_path, kind, weight) "
        "VALUES (?, ?, 'includes', 1.0)", sorted(inc_edges))

    conn.commit()
    return {"references": len(ref_edges), "includes": len(inc_edges)}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_edges.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Wire edge extraction into the scan**

Edit `src/archaeon/codegraph/scan.py`. Add the import near the top (after the existing `from archaeon.codegraph.ts_scan import ...` line):

```python
from archaeon.codegraph.edges import extract_edges
```

Then in `scan_component`, replace the final two lines:

```python
    conn.commit()
    return stats
```

with:

```python
    conn.commit()
    edges = extract_edges(conn, root)
    stats["ref_edges"] = edges["references"]
    stats["include_edges"] = edges["includes"]
    return stats
```

(Adding keys is safe: the existing scan tests assert only `clang`/`tree_sitter`/`gaps`.)

- [ ] **Step 6: Run the scan tests to confirm wiring**

Run: `uv run pytest tests/test_scan_merge.py tests/test_edges.py -v`
Expected: PASS. `test_scan_falls_back_and_records_gaps` still passes; `extract_edges` ran on the fixture without error.

- [ ] **Step 7: Commit**

```bash
git add src/archaeon/codegraph/edges.py src/archaeon/codegraph/scan.py tests/test_edges.py
git commit -m "feat(codegraph): extract reference+include edges during scan"
```

---

## Task 4: Ollama embedding index

**Files:**
- Create: `src/archaeon/retrieval/__init__.py` (empty package marker)
- Create: `src/archaeon/retrieval/embed.py`
- Test: `tests/test_embed.py`

**Interfaces:**
- Consumes: `symbol_rows` (Task 2); `symbol_vectors` table (Task 1).
- Produces:
  - `embed_texts(texts: list[str], model: str, endpoint: str, dims: int) -> list[list[float]]` — one POST to `{endpoint}/api/embed`; returns each embedding truncated to `dims` (Matryoshka). Raises `requests.RequestException` on transport failure.
  - `build_embedding_index(conn, repo_path, model, endpoint, dims, batch=16) -> dict` with keys `embedded: int`, `skipped: int`, `ollama_available: bool`, and optional `error: str`. Idempotent: skips symbols already present in `symbol_vectors` for this `(model, dims)`. On transport failure mid-run it commits what it has and returns `ollama_available=False`.
  - `load_vectors(conn, model, dims) -> dict[int, numpy.ndarray]` — symbol_id → float32 vector.
  - `cosine(a: numpy.ndarray, b: numpy.ndarray) -> float` — 0.0 when either norm is 0.
  - Module constant `CODE_PROMPT: str` (instruction prefix prepended before embedding).

- [ ] **Step 1: Write the failing test**

Create `tests/test_embed.py`:

```python
import numpy as np
import pytest
import requests

from archaeon import retrieval
from archaeon.retrieval import embed as embed_mod
from archaeon.db import connect


def _insert(conn, name, path, line, end_line):
    conn.execute(
        "INSERT INTO symbols(name, kind, path, line, end_line, signature, "
        "source) VALUES (?, 'function', ?, ?, ?, 'sig', 'tree-sitter')",
        (name, path, line, end_line))


def _fixture(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.c").write_text(
        "int f(void){return 1;}\nint g(void){return 2;}\n")
    conn = connect(tmp_path / "e.db")
    _insert(conn, "f", "src/a.c", 1, 1)
    _insert(conn, "g", "src/a.c", 2, 2)
    return conn


def test_build_index_embeds_and_is_idempotent(tmp_path, monkeypatch):
    conn = _fixture(tmp_path)
    calls = {"n": 0}

    def fake_embed(texts, model, endpoint, dims):
        calls["n"] += 1
        return [[float(len(t))] * dims for t in texts]

    monkeypatch.setattr(embed_mod, "embed_texts", fake_embed)

    r1 = embed_mod.build_embedding_index(conn, tmp_path, "m", "http://x", 4)
    assert r1["embedded"] == 2 and r1["ollama_available"] is True
    first_calls = calls["n"]

    # rerun under the same (model, dims): everything skipped, no new calls
    r2 = embed_mod.build_embedding_index(conn, tmp_path, "m", "http://x", 4)
    assert r2["embedded"] == 0
    assert calls["n"] == first_calls

    # a different model triggers re-embedding
    r3 = embed_mod.build_embedding_index(conn, tmp_path, "m2", "http://x", 4)
    assert r3["embedded"] == 2
    assert calls["n"] > first_calls


def test_build_index_degrades_when_ollama_unreachable(tmp_path, monkeypatch):
    conn = _fixture(tmp_path)

    def boom(texts, model, endpoint, dims):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr(embed_mod, "embed_texts", boom)
    r = embed_mod.build_embedding_index(conn, tmp_path, "m", "http://x", 4)
    assert r["ollama_available"] is False
    assert "connection refused" in r["error"]
    n = conn.execute("SELECT COUNT(*) AS c FROM symbol_vectors").fetchone()["c"]
    assert n == 0


def test_load_vectors_roundtrip_and_cosine(tmp_path, monkeypatch):
    conn = _fixture(tmp_path)

    def fake_embed(texts, model, endpoint, dims):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(embed_mod, "embed_texts", fake_embed)
    embed_mod.build_embedding_index(conn, tmp_path, "m", "http://x", 4)
    vecs = embed_mod.load_vectors(conn, "m", 4)
    assert len(vecs) == 2
    a = next(iter(vecs.values()))
    assert embed_mod.cosine(a, a) == pytest.approx(1.0)
    assert embed_mod.cosine(a, np.zeros(4, dtype=np.float32)) == 0.0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_embed.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'archaeon.retrieval'`.

- [ ] **Step 3: Create the package marker**

Create `src/archaeon/retrieval/__init__.py` (empty file):

```python
```

- [ ] **Step 4: Implement the embedding index**

Create `src/archaeon/retrieval/embed.py`:

```python
import numpy as np
import requests

from archaeon.codegraph.symsource import symbol_rows

CODE_PROMPT = "Represent this C/C++ code for retrieval:\n"


def embed_texts(texts: list[str], model: str, endpoint: str,
                dims: int) -> list[list[float]]:
    resp = requests.post(
        f"{endpoint.rstrip('/')}/api/embed",
        json={"model": model, "input": texts}, timeout=120)
    resp.raise_for_status()
    embeddings = resp.json()["embeddings"]
    return [e[:dims] for e in embeddings]


def build_embedding_index(conn, repo_path, model: str, endpoint: str,
                          dims: int, batch: int = 16) -> dict:
    rows = symbol_rows(conn, repo_path)
    done = {r["symbol_id"] for r in conn.execute(
        "SELECT symbol_id FROM symbol_vectors WHERE model=? AND dims=?",
        (model, dims))}
    todo = [r for r in rows if r["id"] not in done]
    if not todo:
        return {"embedded": 0, "skipped": len(rows), "ollama_available": True}
    embedded = 0
    try:
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            texts = [CODE_PROMPT + r["signature"] + "\n" + r["text"]
                     for r in chunk]
            vecs = embed_texts(texts, model, endpoint, dims)
            conn.executemany(
                "INSERT OR REPLACE INTO symbol_vectors(symbol_id, model, "
                "dims, vec) VALUES (?, ?, ?, ?)",
                [(r["id"], model, dims,
                  np.asarray(v, dtype=np.float32).tobytes())
                 for r, v in zip(chunk, vecs)])
            embedded += len(chunk)
        conn.commit()
    except requests.RequestException as e:
        conn.commit()
        return {"embedded": embedded, "skipped": len(done),
                "ollama_available": False, "error": str(e)}
    return {"embedded": embedded, "skipped": len(done),
            "ollama_available": True}


def load_vectors(conn, model: str, dims: int) -> dict[int, np.ndarray]:
    out: dict[int, np.ndarray] = {}
    for r in conn.execute(
            "SELECT symbol_id, vec FROM symbol_vectors "
            "WHERE model=? AND dims=?", (model, dims)):
        out[r["symbol_id"]] = np.frombuffer(r["vec"], dtype=np.float32)
    return out


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_embed.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add src/archaeon/retrieval/__init__.py src/archaeon/retrieval/embed.py tests/test_embed.py
git commit -m "feat(retrieval): local Ollama embedding index with graph-only degradation"
```

---

## Task 5: Clusterer (weighted graph + community detection + labels)

**Files:**
- Create: `src/archaeon/retrieval/cluster.py`
- Test: `tests/test_cluster.py`

**Interfaces:**
- Consumes: `symbol_rows` (Task 2); `load_vectors`, `cosine` (Task 4); `symbol_edges`, `file_edges`, `coupling`, `clusters`, `cluster_members` tables; `CLAIM_TYPES` from `archaeon.claims.schema`; `_strip_fence` from `archaeon.claims.recover`; `config.retrieval` dict shape (Task 1).
- Produces:
  - `build_symbol_graph(conn, repo_path, retr: dict, vectors: dict[int, np.ndarray]) -> networkx.Graph` — nodes = all symbol ids; edge `weight` = weighted sum of reference/include/coupling/embedding signals.
  - `cluster_symbols(conn, repo_path, component: str, retr: dict, label_fn=None) -> list[dict]` — persists `clusters` + `cluster_members` (full rebuild), returns `[{"id": int, "label": str, "members": list[int], "candidate_types": str}, ...]`. Graph-only when `vectors` empty. `label_fn(member_rows: list[dict]) -> tuple[str, str]` is optional (label, comma-joined candidate types).
  - `label_cluster(member_rows: list[dict], ask) -> tuple[str, str]` — cheap-model labeller usable as `label_fn`. `ask(prompt) -> str` (the `AgentClassifier.ask` shape).
  - Module constants `LABEL_SYSTEM: str`, `LABEL_PROMPT: str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cluster.py`:

```python
import json

from archaeon.retrieval import cluster as cluster_mod
from archaeon.db import connect
from archaeon import config as config_mod


def _insert(conn, name, path, line, end_line):
    conn.execute(
        "INSERT INTO symbols(name, kind, path, line, end_line, signature, "
        "source) VALUES (?, 'function', ?, ?, ?, '', 'tree-sitter')",
        (name, path, line, end_line))
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def _edge(conn, a, b, w=5.0):
    conn.execute(
        "INSERT OR REPLACE INTO symbol_edges(src_id, dst_id, kind, weight) "
        "VALUES (?, ?, 'references', ?)", (a, b, w))
    conn.execute(
        "INSERT OR REPLACE INTO symbol_edges(src_id, dst_id, kind, weight) "
        "VALUES (?, ?, 'references', ?)", (b, a, w))


def _two_feature_fixture(tmp_path):
    # Feature 1: a1<->a2 in fileA; Feature 2: b1<->b2 in fileB. No cross edges.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.c").write_text("//a\n" * 10)
    (tmp_path / "src" / "b.c").write_text("//b\n" * 10)
    conn = connect(tmp_path / "e.db")
    a1 = _insert(conn, "a1", "src/a.c", 1, 2)
    a2 = _insert(conn, "a2", "src/a.c", 3, 4)
    b1 = _insert(conn, "b1", "src/b.c", 1, 2)
    b2 = _insert(conn, "b2", "src/b.c", 3, 4)
    _edge(conn, a1, a2)
    _edge(conn, b1, b2)
    conn.commit()
    return conn, {"a1": a1, "a2": a2, "b1": b1, "b2": b2}


def test_two_features_land_in_distinct_clusters_graph_only(tmp_path):
    conn, ids = _two_feature_fixture(tmp_path)
    retr = config_mod.retrieval({})
    result = cluster_symbols_result = cluster_mod.cluster_symbols(
        conn, tmp_path, "demo", retr, label_fn=None)
    # membership map: symbol_id -> cluster id
    member_of = {}
    for r in conn.execute("SELECT cluster_id, symbol_id FROM cluster_members"):
        member_of[r["symbol_id"]] = r["cluster_id"]
    assert member_of[ids["a1"]] == member_of[ids["a2"]]
    assert member_of[ids["b1"]] == member_of[ids["b2"]]
    assert member_of[ids["a1"]] != member_of[ids["b1"]]
    assert len(result) == 2


def test_cluster_rebuild_is_idempotent(tmp_path):
    conn, _ = _two_feature_fixture(tmp_path)
    retr = config_mod.retrieval({})
    cluster_mod.cluster_symbols(conn, tmp_path, "demo", retr)
    cluster_mod.cluster_symbols(conn, tmp_path, "demo", retr)
    n = conn.execute("SELECT COUNT(*) AS c FROM clusters").fetchone()["c"]
    assert n == 2  # not 4


def test_label_cluster_parses_model_json():
    rows = [{"name": "check_temp", "signature": "int check_temp(int)"}]
    replies = [json.dumps(
        {"label": "thermal fault path", "candidate_types": ["threshold"]})]
    ask = lambda p: replies.pop(0)
    label, ctypes = cluster_mod.label_cluster(rows, ask)
    assert label == "thermal fault path"
    assert ctypes == "threshold"


def test_label_cluster_survives_bad_json():
    label, ctypes = cluster_mod.label_cluster(
        [{"name": "x", "signature": ""}], lambda p: "not json")
    assert label == "" and ctypes == ""
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_cluster.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'archaeon.retrieval.cluster'`.

- [ ] **Step 3: Implement the clusterer**

Create `src/archaeon/retrieval/cluster.py`:

```python
import json

import networkx as nx
import numpy as np
from networkx.algorithms.community import greedy_modularity_communities

from archaeon.claims.recover import _strip_fence
from archaeon.claims.schema import CLAIM_TYPES
from archaeon.codegraph.symsource import symbol_rows
from archaeon.retrieval.embed import load_vectors

LABEL_SYSTEM = (
    "You name a cluster of related C/C++ symbols as a short feature area and "
    "guess which what-layer claim types it likely contains. Output only JSON."
)

LABEL_PROMPT = """Symbols in this cluster: {names}

Claim types to choose from: {types}

Return JSON: {{"label": "a 2-5 word feature-area name",
"candidate_types": [subset of the claim types above]}}. Output the JSON only."""


def _add_edge(g: nx.Graph, a: int, b: int, w: float) -> None:
    if a == b or w <= 0:
        return
    if g.has_edge(a, b):
        g[a][b]["weight"] += w
    else:
        g.add_edge(a, b, weight=w)


def _connect_files(g, path_to_ids, path_a, path_b, w, cap) -> None:
    ids_a = path_to_ids.get(path_a, [])
    ids_b = path_to_ids.get(path_b, [])
    if not ids_a or not ids_b or len(ids_a) * len(ids_b) > cap:
        return  # skip explosive cross-file products (coverage note)
    for a in ids_a:
        for b in ids_b:
            _add_edge(g, a, b, w)


def _add_embedding_edges(g, ids, vectors, w, k) -> None:
    vids = [i for i in ids if i in vectors]
    if len(vids) < 2:
        return
    mat = np.vstack([vectors[i] for i in vids]).astype(np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms
    sims = mat @ mat.T
    k = min(k, len(vids) - 1)
    for row, i in enumerate(vids):
        order = np.argsort(-sims[row])
        added = 0
        for col in order:
            if col == row:
                continue
            _add_edge(g, i, vids[col], w * float(sims[row, col]))
            added += 1
            if added >= k:
                break


def build_symbol_graph(conn, repo_path, retr, vectors) -> nx.Graph:
    rows = symbol_rows(conn, repo_path)
    ids = [r["id"] for r in rows]
    g = nx.Graph()
    g.add_nodes_from(ids)
    path_to_ids: dict[str, list[int]] = {}
    for r in rows:
        path_to_ids.setdefault(r["path"], []).append(r["id"])

    for r in conn.execute(
            "SELECT src_id, dst_id, weight FROM symbol_edges "
            "WHERE kind='references'"):
        _add_edge(g, r["src_id"], r["dst_id"],
                  retr["w_references"] * r["weight"])

    cap = retr["max_cross_file_pairs"]
    for r in conn.execute(
            "SELECT src_path, dst_path, weight FROM file_edges "
            "WHERE kind='includes'"):
        _connect_files(g, path_to_ids, r["src_path"], r["dst_path"],
                       retr["w_includes"] * r["weight"], cap)
    for r in conn.execute(
            "SELECT path_a, path_b, co_changes, support_a, support_b "
            "FROM coupling"):
        strength = r["co_changes"] / max(
            1, min(r["support_a"], r["support_b"]))
        _connect_files(g, path_to_ids, r["path_a"], r["path_b"],
                       retr["w_coupling"] * strength, cap)

    if vectors:
        _add_embedding_edges(g, ids, vectors, retr["w_embedding"],
                             retr["sim_top_k"])
    return g


def label_cluster(member_rows, ask):
    names = ", ".join(r["name"] for r in member_rows[:40])
    try:
        raw = ask(LABEL_PROMPT.format(names=names, types=sorted(CLAIM_TYPES)))
        d = json.loads(_strip_fence(raw))
    except Exception:
        return "", ""
    if not isinstance(d, dict):
        return "", ""
    label = str(d.get("label", "")).strip()
    types = [t for t in d.get("candidate_types", []) if t in CLAIM_TYPES]
    return label, ",".join(types)


def cluster_symbols(conn, repo_path, component, retr, label_fn=None) -> list:
    vectors = load_vectors(conn, retr["embed_model"], retr["embed_dims"])
    g = build_symbol_graph(conn, repo_path, retr, vectors)
    if g.number_of_nodes() == 0:
        return []
    if g.number_of_edges() == 0:
        communities = [{n} for n in g.nodes()]
    else:
        communities = greedy_modularity_communities(g, weight="weight")

    rows_by_id = {r["id"]: r for r in symbol_rows(conn, repo_path)}
    conn.execute("DELETE FROM cluster_members")
    conn.execute("DELETE FROM clusters")
    result = []
    for comm in communities:
        members = sorted(comm)
        label, ctypes = "", ""
        if label_fn is not None:
            label, ctypes = label_fn(
                [rows_by_id[m] for m in members if m in rows_by_id])
        cur = conn.execute(
            "INSERT INTO clusters(component, label, candidate_types) "
            "VALUES (?, ?, ?)", (component, label, ctypes))
        cid = cur.lastrowid
        conn.executemany(
            "INSERT INTO cluster_members(cluster_id, symbol_id) "
            "VALUES (?, ?)", [(cid, m) for m in members])
        result.append({"id": cid, "label": label, "members": members,
                       "candidate_types": ctypes})
    conn.commit()
    return result
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_cluster.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/archaeon/retrieval/cluster.py tests/test_cluster.py
git commit -m "feat(retrieval): hybrid graph clusterer with cheap-model cluster labels"
```

---

## Task 6: Token-bounded, ranked bundle builder

**Files:**
- Create: `src/archaeon/retrieval/bundle.py`
- Test: `tests/test_bundle.py`

**Interfaces:**
- Consumes: `symbol_rows` (Task 2); `load_vectors`, `cosine` (Task 4); `cluster_members` table; `config.retrieval` dict.
- Produces:
  - `estimate_tokens(text: str) -> int` — heuristic `ceil(len/4)`.
  - `pack_symbols(symbols: list[dict], token_budget: int) -> tuple[str, list[dict]]` — packs whole-symbol, line-numbered spans (numbered with the symbol's real file line numbers, so evidence refs match the source) in the given order until the next would exceed `token_budget`; always includes at least the first symbol. Returns `(bundle_text, manifest)`; manifest entries are `{"id", "name", "path", "line", "end_line"}`.
  - `rank_symbols(symbols: list[dict], vectors: dict[int, np.ndarray], centroid) -> list[dict]` — descending cosine-to-centroid when `centroid is not None` and vectors exist; otherwise input order (caller supplies a graph-degree order for the graph-only path).
  - `bundle_for_cluster(conn, repo_path, cluster_id: int, retr: dict) -> tuple[str, list[dict]]` — loads members, computes the centroid from member vectors (mean), ranks, and packs to `retr["token_budget"]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_bundle.py`:

```python
import numpy as np

from archaeon.retrieval import bundle as bundle_mod


def _sym(i, name, line, nlines):
    return {"id": i, "name": name, "path": "src/a.c", "line": line,
            "end_line": line + nlines - 1,
            "text": "\n".join(f"code{name}{k}" for k in range(nlines))}


def test_estimate_tokens_is_roughly_chars_over_four():
    assert bundle_mod.estimate_tokens("") == 0
    assert bundle_mod.estimate_tokens("abcd") == 1
    assert bundle_mod.estimate_tokens("abcde") == 2


def test_pack_respects_budget_and_prefers_whole_symbols():
    syms = [_sym(1, "f", 10, 3), _sym(2, "g", 20, 3), _sym(3, "h", 30, 3)]
    # budget large enough for ~1 symbol only
    one_block = bundle_mod.pack_symbols(syms, 1000)[0]
    tiny, manifest = bundle_mod.pack_symbols(syms, 8)
    assert len(manifest) == 1                      # only the first fits
    assert manifest[0]["name"] == "f"
    # whole-symbol: the included symbol's full span is present, line-numbered
    # with its real file lines (10..12), never a partial split
    assert "10: codef0" in tiny and "12: codef2" in tiny


def test_pack_always_includes_at_least_first_symbol():
    syms = [_sym(1, "big", 1, 100)]
    text, manifest = bundle_mod.pack_symbols(syms, 1)  # budget below one symbol
    assert len(manifest) == 1 and "big" in text


def test_rank_orders_by_cosine_to_centroid():
    syms = [_sym(1, "near", 1, 1), _sym(2, "far", 2, 1)]
    vectors = {1: np.array([1.0, 0.0], dtype=np.float32),
               2: np.array([0.0, 1.0], dtype=np.float32)}
    centroid = np.array([1.0, 0.0], dtype=np.float32)
    ranked = bundle_mod.rank_symbols(syms, vectors, centroid)
    assert [s["name"] for s in ranked] == ["near", "far"]


def test_rank_returns_input_order_without_centroid():
    syms = [_sym(1, "a", 1, 1), _sym(2, "b", 2, 1)]
    assert bundle_mod.rank_symbols(syms, {}, None) == syms
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_bundle.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'archaeon.retrieval.bundle'`.

- [ ] **Step 3: Implement the bundle builder**

Create `src/archaeon/retrieval/bundle.py`:

```python
import numpy as np

from archaeon.codegraph.symsource import symbol_rows
from archaeon.retrieval.embed import cosine, load_vectors


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def pack_symbols(symbols, token_budget):
    """Pack ranked whole-symbol spans up to token_budget.

    Line-numbers each span with the symbol's real file line numbers so the
    synthesizer's evidence refs (path:line) match the source. Always emits at
    least the first (highest-ranked) symbol, even if it alone exceeds budget.
    """
    parts, manifest, total = [], [], 0
    for s in symbols:
        lines = s["text"].splitlines()
        numbered = "\n".join(
            f"{s['line'] + i}: {ln}" for i, ln in enumerate(lines))
        block = (f"=== {s['path']}:{s['line']}-{s['end_line']} "
                 f"({s['name']}) ===\n{numbered}\n")
        t = estimate_tokens(block)
        if manifest and total + t > token_budget:
            break
        parts.append(block)
        manifest.append({"id": s.get("id"), "name": s["name"],
                         "path": s["path"], "line": s["line"],
                         "end_line": s["end_line"]})
        total += t
    return "\n".join(parts), manifest


def rank_symbols(symbols, vectors, centroid):
    if centroid is not None and vectors:
        def key(s):
            v = vectors.get(s["id"])
            return -cosine(v, centroid) if v is not None else 1.0
        return sorted(symbols, key=key)
    return symbols


def bundle_for_cluster(conn, repo_path, cluster_id, retr):
    member_ids = [r["symbol_id"] for r in conn.execute(
        "SELECT symbol_id FROM cluster_members WHERE cluster_id=?",
        (cluster_id,))]
    id_set = set(member_ids)
    members = [r for r in symbol_rows(conn, repo_path) if r["id"] in id_set]
    vectors = load_vectors(conn, retr["embed_model"], retr["embed_dims"])
    mem_vecs = [vectors[m] for m in member_ids if m in vectors]
    centroid = None
    if mem_vecs:
        centroid = np.mean(np.vstack(mem_vecs), axis=0)
    ranked = rank_symbols(members, vectors, centroid)
    return pack_symbols(ranked, retr["token_budget"])
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_bundle.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/archaeon/retrieval/bundle.py tests/test_bundle.py
git commit -m "feat(retrieval): token-bounded whole-symbol bundle builder with centroid ranking"
```

---

## Task 7: CLI wiring (`embed`, `cluster`, reworked `synthesize`) + example config

**Files:**
- Modify: `src/archaeon/cli.py`
- Modify: `archaeon.example.toml`
- Test: extend `tests/test_cli.py`

**Interfaces:**
- Consumes: `config.retrieval` (Task 1); `build_embedding_index` (Task 4); `cluster_symbols`, `label_cluster` (Task 5); `bundle_for_cluster` (Task 6); existing `synthesize_claims`, `verify_claims`, `save_claims`, `AgentClassifier`.
- Produces: three CLI commands — `embed`, `cluster`, and a reworked `synthesize` accepting `--feature` (unchanged), `--cluster <id>`, or `--all-clusters`.

- [ ] **Step 1: Write the failing CLI test**

Add to `tests/test_cli.py` (the `_setup` helper already builds a git repo + `archaeon.toml`). Append:

```python
def test_embed_degrades_when_ollama_down(tmp_path, monkeypatch):
    config = _setup(tmp_path)
    runner = CliRunner()
    assert runner.invoke(main, ["scan", "--config", str(config)]).exit_code == 0

    import archaeon.retrieval.embed as embed_mod
    import requests as _rq

    def boom(texts, model, endpoint, dims):
        raise _rq.RequestException("refused")
    monkeypatch.setattr(embed_mod, "embed_texts", boom)

    r = runner.invoke(main, ["embed", "--config", str(config)])
    assert r.exit_code == 0, r.output
    assert "ollama" in r.output.lower()  # degradation is reported, not a crash


def test_cluster_runs_graph_only_without_ollama(tmp_path, monkeypatch):
    config = _setup(tmp_path)
    runner = CliRunner()
    assert runner.invoke(main, ["scan", "--config", str(config)]).exit_code == 0

    import archaeon.retrieval.embed as embed_mod
    import requests as _rq
    monkeypatch.setattr(embed_mod, "embed_texts",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _rq.RequestException("refused")))
    # cheap-model labelling must not hit the network in the test
    import archaeon.retrieval.cluster as cluster_mod
    monkeypatch.setattr(cluster_mod, "label_cluster", lambda rows, ask: ("", ""))

    r = runner.invoke(main, ["cluster", "--config", str(config)])
    assert r.exit_code == 0, r.output
    assert "clusters:" in r.output
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_cli.py -k "embed or cluster" -v`
Expected: FAIL — `embed`/`cluster` are not yet commands (`Error: No such command 'embed'`).

- [ ] **Step 3: Add the `embed` and `cluster` commands**

Edit `src/archaeon/cli.py`. Add these two commands after `cli_scan` (around `cli.py:122`):

```python
@main.command("embed")
@config_option
def cli_embed(config_path):
    """Build the local embedding index for scanned symbols (idempotent)."""
    from archaeon.retrieval.embed import build_embedding_index
    cfg, conn = _load(config_path)
    retr = config_mod.retrieval(cfg)
    r = build_embedding_index(conn, Path(cfg["component"]["repo_path"]),
                              retr["embed_model"], retr["embed_endpoint"],
                              retr["embed_dims"])
    if r["ollama_available"]:
        click.echo(f"embedded: {r['embedded']}  skipped: {r['skipped']}  "
                   f"model: {retr['embed_model']} dims: {retr['embed_dims']}")
    else:
        click.echo(f"ollama unavailable ({r.get('error', 'unknown')}); "
                   f"embedded {r['embedded']} before failing — clustering "
                   f"will fall back to graph-only")


@main.command("cluster")
@config_option
def cli_cluster(config_path):
    """Cluster scanned symbols into feature areas (embeds first if possible)."""
    from archaeon.llm import AgentClassifier
    from archaeon.retrieval.cluster import (
        LABEL_SYSTEM, cluster_symbols, label_cluster)
    from archaeon.retrieval.embed import build_embedding_index
    cfg, conn = _load(config_path)
    retr = config_mod.retrieval(cfg)
    e = build_embedding_index(conn, Path(cfg["component"]["repo_path"]),
                              retr["embed_model"], retr["embed_endpoint"],
                              retr["embed_dims"])
    if not e["ollama_available"]:
        click.echo("ollama unavailable; clustering on graph signal only",
                   err=True)
    labeller = AgentClassifier(cfg["llm"]["cheap_model"], LABEL_SYSTEM,
                               max_turns=1)
    clusters = cluster_symbols(
        conn, Path(cfg["component"]["repo_path"]),
        cfg["component"]["name"], retr,
        label_fn=lambda rows: label_cluster(rows, labeller.ask))
    click.echo(f"clusters: {len(clusters)}  "
               f"(ollama: {'yes' if e['ollama_available'] else 'no'})")
    for c in clusters:
        click.echo(f"  [{c['id']}] {c['label'] or '(unlabelled)'}  "
                   f"({len(c['members'])} symbols)")
```

- [ ] **Step 4: Rework `synthesize` to consume clusters**

Replace the entire `cli_synthesize` function (`cli.py:143-175`) with:

```python
@main.command("synthesize")
@config_option
@click.option("--feature", "feature", default=None,
              help="path prefix of the feature area to synthesize claims for")
@click.option("--cluster", "cluster_id", type=int, default=None,
              help="synthesize a single cluster id (from `cluster`)")
@click.option("--all-clusters", "all_clusters", is_flag=True,
              help="synthesize every cluster in the component")
@click.option("--out", "out_dir", default="claims", show_default=True)
def cli_synthesize(config_path, feature, cluster_id, all_clusters, out_dir):
    """Recover + adversarially verify what-layer claims from code.

    Bundles are built from clusters (token-bounded, relevance-ranked) when
    --cluster/--all-clusters is given; --feature keeps the path-prefix entry
    point (mapped to the clusters overlapping that prefix, or an ad-hoc ranked
    bundle of the prefix's symbols if clustering hasn't been run).
    """
    from archaeon.claims.recover import (
        SYNTH_SYSTEM, VERIFY_SYSTEM, synthesize_claims, verify_claims)
    from archaeon.claims.schema import save_claims
    from archaeon.llm import AgentClassifier
    from archaeon.retrieval.bundle import (
        bundle_for_cluster, pack_symbols, rank_symbols)
    from archaeon.codegraph.symsource import symbol_rows

    if sum(bool(x) for x in (feature, cluster_id is not None,
                             all_clusters)) != 1:
        click.echo("give exactly one of --feature, --cluster, --all-clusters")
        return
    cfg, conn = _load(config_path)
    retr = config_mod.retrieval(cfg)
    repo = Path(cfg["component"]["repo_path"])

    # Resolve the list of (feature_label, cluster_id | None) targets.
    targets: list[tuple[str, int | None]] = []
    if all_clusters:
        targets = [(r["label"] or f"cluster-{r['id']}", r["id"])
                   for r in conn.execute(
                       "SELECT id, label FROM clusters ORDER BY id")]
        if not targets:
            click.echo("no clusters; run `cluster` first")
            return
    elif cluster_id is not None:
        row = conn.execute("SELECT id, label FROM clusters WHERE id=?",
                           (cluster_id,)).fetchone()
        if row is None:
            click.echo(f"no cluster {cluster_id}")
            return
        targets = [(row["label"] or f"cluster-{row['id']}", row["id"])]
    else:
        overlap = [r["cluster_id"] for r in conn.execute(
            "SELECT DISTINCT cm.cluster_id FROM cluster_members cm "
            "JOIN symbols s ON s.id = cm.symbol_id "
            "WHERE s.path LIKE ? ESCAPE '\\'", (_like_prefix(feature),))]
        if overlap:
            targets = [(feature, cid) for cid in overlap]

    model = cfg["llm"].get("expensive_model", cfg["llm"]["cheap_model"])
    all_claims = []
    for label, cid in targets:
        if cid is not None:
            bundle, _ = bundle_for_cluster(conn, repo, cid, retr)
        else:
            # --feature with no clusters: rank the prefix's symbols by graph
            # degree is unavailable here, so pack in scan order under budget.
            rows = symbol_rows(conn, repo, prefix=feature)
            if not rows:
                click.echo("no parsed files under that prefix; run scan first")
                return
            bundle, _ = pack_symbols(rank_symbols(rows, {}, None),
                                     retr["token_budget"])
        claims = synthesize_claims(
            label, bundle,
            AgentClassifier(model, SYNTH_SYSTEM, max_turns=4).ask)
        verify_claims(claims, bundle,
                      AgentClassifier(model, VERIFY_SYSTEM, max_turns=4).ask)
        all_claims.extend(claims)

    # Re-id claims uniquely across clusters before saving.
    for i, c in enumerate(all_claims, 1):
        c.id = f"CLM-{i:04d}"
    save_claims(all_claims, Path(out_dir))
    verified = sum(1 for c in all_claims if c.status == "machine_verified")
    click.echo(f"claims: {len(all_claims)}  machine_verified: {verified}  "
               f"contested: {len(all_claims) - verified}  -> {out_dir}/")
```

Note: `_like_prefix` already exists in `cli.py:137`. The `synthesize` help text no longer marks `--feature` as required; the mutual-exclusion check enforces exactly one entry point.

- [ ] **Step 5: Run the CLI tests**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS — including the two new degradation tests and the pre-existing `test_ingest_git_scan_link_stats` / `test_eval_command`.

- [ ] **Step 6: Document `[retrieval]` in the example config**

Edit `archaeon.example.toml`, appending after the `[llm]` block:

```toml
# [retrieval]                              # optional — these are the defaults
# embed_model = "qwen3-embedding:4b"       # ":0.6b" for CI / low-resource
# embed_endpoint = "http://localhost:11434"
# embed_dims = 1024
# token_budget = 60000                     # per-bundle token budget (heuristic)
# w_references = 1.0                        # graph/embedding blend weights
# w_includes = 0.5
# w_coupling = 0.5
# w_embedding = 1.0
# sim_top_k = 10                           # embedding neighbours per symbol
# max_cross_file_pairs = 400               # cap on file-level edge expansion
```

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: all tests pass (existing + all new).

- [ ] **Step 8: Commit**

```bash
git add src/archaeon/cli.py archaeon.example.toml tests/test_cli.py
git commit -m "feat(cli): embed + cluster commands and cluster-driven synthesize"
```

---

## Manual integration validation (not automated — matches spec §7 integration)

After Task 7, validate against the real `motor-ctrl` component (needs a running Ollama with `qwen3-embedding:4b` pulled, and Claude CLI auth). This is the spec's acceptance evidence, run by hand:

```bash
uv run archaeon scan --config archaeon.example.toml
uv run archaeon cluster --config archaeon.example.toml
uv run archaeon synthesize --config archaeon.example.toml --all-clusters --out claims_nav
uv run archaeon claims-eval --claims claims_nav --labels claim_labels.csv
```

Confirm, per spec §7:
- Clustering separates the two feature areas of the component (inspect `cluster` output labels + `cluster_members`).
- Claim yield over the `motor_ctrl_impl` slice is **≥ 36** (the spike's count) with **no whole-file inlining** (bundles are symbol spans; check a saved claim's evidence refs land on real lines).
- Re-running with Ollama stopped still produces clusters and a bundle (graph-only), logged as a coverage note.

Record the numbers in a dated `docs/research/` note as the hardened-P1 baseline (per the spike checklist Step 8).

---

## Self-Review

**1. Spec coverage**

| Spec §4 unit | Task |
|---|---|
| 4.1 Edge extraction (`references`, `includes`, `symbol_edges`) | Task 1 (schema) + Task 3 |
| 4.2 Embedding index (`symbol_vectors`, batched `/api/embed`, idempotent, in-process cosine) | Task 1 (schema) + Task 4 |
| 4.3 Clusterer (weighted graph, greedy-modularity, cheap-model labels, `clusters`/`cluster_members`) | Task 1 (schema) + Task 5 |
| 4.4 Bundle builder (rank to centroid, token-bounded whole-symbol packing, manifest) | Task 6 |
| §3 embedding stack config (`[retrieval]`) | Task 1 + Task 7 |
| §5 data flow / `--all-clusters` | Task 7 |
| §6 degradation (Ollama down → graph-only; singletons kept; unresolved refs skipped) | Task 4 (embed degrade), Task 5 (graph-only + singletons via empty-edge path), Task 3 (unresolved skipped) |
| §6 bundle exceeds budget → split | **Partial**: `pack_symbols` truncates to budget and always keeps ≥1 symbol; the multi-sub-bundle *split-and-merge* is not implemented. See gap note below. |
| §7 testing (unit + integration + degradation) | Unit tests in Tasks 3–6; integration is the manual validation section (needs live Ollama + Claude auth, cannot be a hermetic unit test). |
| §8 schema (additive) | Task 1 (+ the resolved `file_edges` addition). |

**Gap deliberately deferred:** spec §6 "cluster bundle still exceeds budget after ranking → split into sub-bundles, synthesize each, merge". This plan packs the highest-ranked symbols up to budget and drops the tail (always keeping ≥1 symbol), which is correct and safe but not the full split/merge. Rationale: split/merge adds a claim-id-merge and per-sub-bundle verify loop that is only exercised by clusters larger than the whole token budget; §9 flags token counting as "start with a heuristic, refine if bundles come out over budget in practice." **If the reviewer wants §6 split/merge in this track, add a Task 8** that loops `pack_symbols` over the ranked remainder to produce a list of bundles and has `synthesize` iterate them per cluster. Flagged rather than silently skipped.

**2. Placeholder scan:** no `TBD`/`TODO`/"handle edge cases"/"similar to Task N" — every code step is complete and self-contained; repeated helpers (`_like_prefix`, the `_insert` test helper) are written out in full in each file that uses them, matching the codebase's existing duplication of `_like_prefix` across `scan.py`/`cli.py`.

**3. Type consistency:** verified across tasks — `symbol_rows(...)` dict keys (`id`,`name`,`kind`,`path`,`line`,`end_line`,`signature`,`text`) are consumed identically in edges/embed/cluster/bundle; `retr` dict keys used in Task 5/6 all exist in `RETRIEVAL_DEFAULTS` (Task 1); `build_embedding_index` return keys (`embedded`,`skipped`,`ollama_available`,`error`) match every reader (Tasks 4 test, Task 7 CLI); `cluster_symbols` result dict keys (`id`,`label`,`members`,`candidate_types`) match the CLI reader; `load_vectors`/`cosine` signatures are consistent between Task 4, 5, 6.

---

**Plan complete and saved to `docs/superpowers/plans/2026-07-24-p1-hardening-a-retrieval-clustering.md`.**
