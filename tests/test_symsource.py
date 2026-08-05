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
