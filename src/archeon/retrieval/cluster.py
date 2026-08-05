import json

import networkx as nx
import numpy as np
from networkx.algorithms.community import greedy_modularity_communities

from archeon.claims.recover import _strip_fence
from archeon.claims.schema import CLAIM_TYPES
from archeon.codegraph.symsource import symbol_rows
from archeon.retrieval.embed import load_vectors

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
    if k <= 0:
        return
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
    conn.execute("DELETE FROM cluster_members")
    conn.execute("DELETE FROM clusters")
    if g.number_of_nodes() == 0:
        conn.commit()
        return []
    if g.number_of_edges() == 0:
        communities = [{n} for n in g.nodes()]
    else:
        communities = greedy_modularity_communities(g, weight="weight")

    rows_by_id = {r["id"]: r for r in symbol_rows(conn, repo_path)}
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
