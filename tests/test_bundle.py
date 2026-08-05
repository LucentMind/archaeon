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


def test_bundle_for_cluster_filters_members_and_packs(tmp_path):
    from archaeon.db import connect
    from archaeon import config as config_mod
    from archaeon.retrieval.bundle import bundle_for_cluster

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.c").write_text(
        "int m1(void){return 1;}\n"
        "int m2(void){return 2;}\n"
        "int other(void){return 3;}\n")
    conn = connect(tmp_path / "e.db")

    def ins(name, line):
        conn.execute(
            "INSERT INTO symbols(name,kind,path,line,end_line,signature,source)"
            " VALUES (?, 'function','src/a.c',?,?, '', 'tree-sitter')",
            (name, line, line))
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    m1, m2, other = ins("m1", 1), ins("m2", 2), ins("other", 3)
    cur = conn.execute(
        "INSERT INTO clusters(component,label,candidate_types) "
        "VALUES('demo','feat','')")
    cid = cur.lastrowid
    conn.executemany(
        "INSERT INTO cluster_members(cluster_id,symbol_id) VALUES (?,?)",
        [(cid, m1), (cid, m2)])
    for sid, vec in ((m1, [1.0, 0.0]), (m2, [0.9, 0.1])):
        conn.execute(
            "INSERT INTO symbol_vectors(symbol_id,model,dims,vec) "
            "VALUES (?,?,?,?)",
            (sid, "m", 2, np.asarray(vec, dtype=np.float32).tobytes()))
    conn.commit()

    retr = config_mod.retrieval({"retrieval": {"embed_model": "m",
                                               "embed_dims": 2}})
    bundle, manifest = bundle_for_cluster(conn, tmp_path, cid, retr)

    # SQL member filter: only cluster members, not `other`
    assert {e["id"] for e in manifest} == {m1, m2}
    assert "m1" in bundle and "m2" in bundle
    assert "other" not in bundle


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
