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
