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
