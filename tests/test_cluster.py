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
    _edge(conn, a2, b1, 0.1)   # weak cross-group bridge: modularity must still split
    conn.commit()
    return conn, {"a1": a1, "a2": a2, "b1": b1, "b2": b2}


def test_two_features_land_in_distinct_clusters_graph_only(tmp_path):
    conn, ids = _two_feature_fixture(tmp_path)
    retr = config_mod.retrieval({})
    result = cluster_mod.cluster_symbols(
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


def test_empty_graph_still_clears_stale_clusters(tmp_path):
    conn = connect(tmp_path / "e.db")
    conn.execute(
        "INSERT INTO clusters(component, label, candidate_types) "
        "VALUES ('old', 'stale', '')")
    conn.execute(
        "INSERT INTO cluster_members(cluster_id, symbol_id) VALUES (1, 1)")
    conn.commit()
    retr = config_mod.retrieval({})
    result = cluster_mod.cluster_symbols(conn, tmp_path, "demo", retr)
    assert result == []
    n_clusters = conn.execute(
        "SELECT COUNT(*) AS c FROM clusters").fetchone()["c"]
    n_members = conn.execute(
        "SELECT COUNT(*) AS c FROM cluster_members").fetchone()["c"]
    assert n_clusters == 0
    assert n_members == 0


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
