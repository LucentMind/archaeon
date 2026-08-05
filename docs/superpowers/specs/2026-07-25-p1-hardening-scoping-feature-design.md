# P1 Hardening — Scan glob filters + `--feature` prefix-bounding + validation

**Date:** 2026-07-25
**Status:** design (approved for planning)
**Related:**
acceptance run *(internal validation run — not in this repo)* ·
[spec A](2026-07-24-p1-hardening-a-retrieval-clustering-design.md) ·
[spec B](2026-07-24-p1-hardening-b-commit-pinning-design.md) ·
[P1 hardening overview](2026-07-24-p1-hardening-overview.md)

## Goal

Close the two code-level gaps the A+B acceptance run surfaced on
`motor-ctrl`, then re-run the acceptance protocol under the scoped
config to validate clustering *quality* (not just "it runs"):

- **Finding 4** — scoping is directory-prefix-only (`path_prefixes`); there is
  no way to exclude `generated/**/*.cpp` mechanical converters, `thirdparty/`,
  or `tests/`. Highest-leverage Spec A follow-up: makes clustering meaningful
  and cuts embed cost ~5×.
- **Finding 3** — `synthesize --feature <prefix>` resolves to every *cluster*
  overlapping the prefix and bundles those clusters **whole**, so under the
  mega-clusters it balloons `navigator/` to ~31,900 vendored/generated symbols
  at 3× LLM cost instead of the ~219 that actually live under the prefix.

Recommendation #1 (embed oversized-symbol fix) is already merged
(`09762a2`) and out of scope here.

## Non-goals

- No new clustering algorithm — the lever is scoping, not the clusterer.
- No `claims-eval` gated precision number this pass — validation is by
  inspection of a handful of claims plus the machine_verified/contested split,
  same protocol as the original acceptance run.
- No downstream changes to embed/cluster/synthesize internals: filtering once
  at scan flows through every stage that reads the `symbols` table.

## Part A — scan `include` / `exclude` glob filters (finding 4)

### Config surface

Two new **optional** keys under `[component]`, both glob lists:

```toml
[component]
# ... existing name/db/repo_path/path_prefixes ...
include = ["**/*.hpp", "**/*.cpp"]                 # optional
exclude = ["**/thirdparty/**", "**/generated/**/*.cpp", "**/tests/**"]
```

Both stay **out of** `config.REQUIRED`, so every existing config (and the
P0/spike tests) keeps validating with no `include`/`exclude` block.

### Semantics

- Patterns are matched against each file's **repo-root-relative POSIX path**
  (the same `rel` string already inserted into `symbols.path`).
- A file is kept **iff**:
  `(include is empty OR path matches any include glob)` **AND**
  `(path matches no exclude glob)`.
- **Exclude wins** over include.
- Both absent ⇒ **today's behavior, byte-for-byte** (no filtering beyond the
  existing `SOURCE_SUFFIXES` check).

### Matching implementation

`PurePosixPath(rel).full_match(glob)` — Python stdlib, gitignore-style `**`
semantics, no new dependency. `full_match` lands in Python 3.13, so bump
`requires-python` from `>=3.12` to `>=3.13` in `pyproject.toml`. (Dev machine
is 3.14; this is a personal research tool with no 3.12 consumers.)

Verified semantics on representative paths:

| path | glob | matches |
|---|---|---|
| `…/thirdparty/imgui/a.cpp` | `**/thirdparty/**` | ✅ |
| `…/generated/public/cpp/a.cpp` | `**/generated/**/*.cpp` | ✅ |
| `…/generated/public/cpp/a.hpp` | `**/generated/**/*.cpp` | ❌ (kept) |
| `…/modules/nav/src/a.cpp` | `**/thirdparty/**` | ❌ (kept) |

### Where it applies

Inside `scan_component`'s walk loop in
[`src/archaeon/codegraph/scan.py`](../../../src/archaeon/codegraph/scan.py),
**after** the `SOURCE_SUFFIXES` filter and **before** `_insert_symbols`. The
per-`path_prefix` `DELETE` scope is unchanged, so re-scanning a prefix stays
idempotent: a previously-unfiltered scan's now-excluded symbols get deleted and
only the filtered subset is re-inserted.

Because `embed`, `cluster`, and `synthesize` all read from the `symbols` table,
**filtering once at scan flows through every downstream stage** — no changes to
those modules.

### Signature / wiring changes

- `scan_component(conn, root, path_prefixes, compile_db_dir, include=None,
  exclude=None)` — new trailing optional params; default `None` ⇒ no filtering.
- A small private helper `_keep(rel, include_globs, exclude_globs) -> bool`
  encapsulates the predicate.
- `cli_scan` passes `cfg["component"].get("include")` /
  `cfg["component"].get("exclude")`.
- Any other call site of `scan_component` (e.g. the P0 spike path at
  `cli.py:41`) keeps working via the defaults.

### Config payoff

Update
[`archaeon.example.scoped.toml`](../../../archaeon.example.scoped.toml)
to add `exclude = ["**/generated/**/*.cpp"]` (and optionally
`**/thirdparty/**`, `**/tests/**` as defense-in-depth). This eliminates the
~267 mechanical `toCpp`/`toJava` converter `.cpp` files that the directory-only
prefixes currently drag in — **removing the documented caveat** in that file's
header, which should be rewritten to reflect that the glob exclude now handles
it cleanly.

## Part B — `--feature` prefix-bounding (finding 3)

### Current behavior (to remove)

In `cli_synthesize`
([`src/archaeon/cli.py:242-250`](../../../src/archaeon/cli.py)), `--feature`
resolves to the set of clusters overlapping the prefix and bundles each **whole**
via `bundle_for_cluster`; only when *no* cluster overlaps does it fall back to
packing the prefix's own symbols.

### New behavior

`--feature <prefix>` becomes a **single** prefix-labeled target that bundles
**exactly the symbols under the prefix** and can never expand past it. New
helper in
[`src/archaeon/retrieval/bundle.py`](../../../src/archaeon/retrieval/bundle.py),
mirroring `bundle_for_cluster` but prefix-scoped:

```python
def bundle_for_prefix(conn, repo_path, prefix, retr):
    rows = symbol_rows(conn, repo_path, prefix=prefix)
    vectors = load_vectors(conn, retr["embed_model"], retr["embed_dims"])
    vecs = [vectors[r["id"]] for r in rows if r["id"] in vectors]
    centroid = np.mean(np.vstack(vecs), axis=0) if vecs else None
    ranked = rank_symbols(rows, vectors, centroid)
    return pack_symbols(ranked, retr["token_budget"])
```

`cli_synthesize`'s `--feature` branch collapses to a single
`targets = [(feature, None)]` and calls `bundle_for_prefix`. The empty-rows
guard ("no parsed files under that prefix; run scan first") is preserved.
`--cluster` and `--all-clusters` paths are untouched; `bundle_for_cluster` and
its tests stay as-is.

This unifies what were two `--feature` paths (overlap-clusters vs no-cluster
fallback) into one prefix-faithful path, ranked by an embedding centroid
computed from the prefix's own symbols and degrading to scan order when no
vectors are present.

## Part C — validation re-run (recommendations #4, #5)

Manual, documented — run after A+B land and all tests pass.

1. Under `archaeon.example.scoped.toml` (now with the `exclude`
   glob) using the **4b** default embedding model
   (`qwen3-embedding:4b`), run:
   `scan → embed → cluster → synthesize --feature <navigator prefix> →
   check-staleness`.
2. Inspect: cluster count/sizes (are they real feature areas now, not 3
   mega-clusters?), a handful of claims for navigator-relevance, and the
   machine_verified/contested split. **Inspection only** — no `claims-eval`.
3. **Live staleness demo:** edit one pinned source line in the real checkout,
   re-run `check-staleness`, confirm it flags exactly the affected claim(s) as
   stale (the `is_stale` path is unit-tested but only the 0-stale case has been
   observed live). Revert the edit afterward.
4. Write results to
   `docs/research/2026-07-25-p1-hardening-scoped-rerun.md`, a companion to the
   A+B acceptance run, and note whether findings 2–4 are now closed.

## Testing (TDD — write first)

**Part A — `scan.py`:**
- Fixture directory tree with `src/*.cpp`, `include/*.hpp`,
  `generated/public/cpp/{a.hpp, a.cpp}`, `thirdparty/x.cpp`, `tests/t.cpp`.
- `exclude=["**/generated/**/*.cpp", "**/thirdparty/**", "**/tests/**"]` keeps
  the `.hpp` and `src` `.cpp`, drops the generated `.cpp`, thirdparty, tests.
- `include=["**/*.hpp"]` keeps only headers.
- `include`+`exclude` together: exclude wins on overlap.
- **Both `None` ⇒ identical symbol set to a pre-change scan** (backward-compat).
- `config.load` still succeeds on a config with no `include`/`exclude`.

**Part B — `bundle.py` / CLI:**
- `bundle_for_prefix` packs only symbols whose `path` is under the prefix; the
  manifest never contains an out-of-prefix symbol even when a cluster spanning
  the prefix + other files exists in `cluster_members`.
- Centroid ranking applied when vectors exist; scan-order fallback when they
  don't; `token_budget` respected (reuse `pack_symbols` guarantees).
- `synthesize --feature` CLI test: with a mega-cluster overlapping the prefix,
  the resulting bundle symbol set == the prefix's symbols (not the cluster's).
- Existing `bundle_for_cluster` and `synthesize --cluster/--all-clusters` tests
  stay green.

## Error handling / edge cases

- **Filters default off** — no `include`/`exclude` ⇒ no behavior change; the
  only observable diff for existing configs is the `requires-python` bump.
- **Malformed glob** — surfaces as an exception during scan; acceptable (config
  author error, fails loudly at the right stage).
- **No vectors under prefix** — `bundle_for_prefix` centroid is `None`,
  `rank_symbols` returns scan order (existing degradation path).
- **Empty prefix result** — preserved `ClickException` ("run scan first").

## Rollout order

1. TDD Part A (scan filters) + `requires-python` bump; commit.
2. Update scoped toml `exclude` + rewrite its caveat header; commit.
3. TDD Part B (`bundle_for_prefix` + `--feature` rewrite); commit.
4. Part C validation re-run; write the research doc; commit.
