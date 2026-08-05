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
