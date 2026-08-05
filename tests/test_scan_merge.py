import json

import pytest

from archeon.codegraph.scan import scan_component
from archeon.db import connect

GOOD = "int f(void) { return 1; }\n"


def test_scan_falls_back_and_records_gaps(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "ok.c").write_text(GOOD)
    (root / "src" / "weird.xc").write_text("not c")
    conn = connect(tmp_path / "e.db")

    stats = scan_component(conn, root, ["src/"], compile_db_dir=None)
    assert stats["tree_sitter"] == 1
    assert stats["clang"] == 0
    assert stats["gaps"] == 1
    sym = conn.execute("SELECT * FROM symbols WHERE name='f'").fetchone()
    assert sym["source"] == "tree-sitter"
    assert sym["path"] == "src/ok.c"
    gap = conn.execute("SELECT * FROM scan_gaps").fetchone()
    assert gap["path"] == "src/weird.xc"


def test_rescan_replaces_previous_results(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "ok.c").write_text(GOOD)
    conn = connect(tmp_path / "e.db")
    scan_component(conn, root, ["src/"], compile_db_dir=None)
    scan_component(conn, root, ["src/"], compile_db_dir=None)
    count = conn.execute("SELECT COUNT(*) AS c FROM symbols").fetchone()["c"]
    assert count == 1


def test_rescan_does_not_over_delete_sibling_component(tmp_path):
    root = tmp_path / "repo"
    (root / "lib_a").mkdir(parents=True)
    (root / "libxa").mkdir(parents=True)
    (root / "lib_a" / "a.c").write_text("int lib_a_fn(void) { return 1; }\n")
    (root / "libxa" / "a.c").write_text("int libxa_fn(void) { return 2; }\n")
    conn = connect(tmp_path / "e.db")

    scan_component(conn, root, ["lib_a/"], compile_db_dir=None)
    scan_component(conn, root, ["libxa/"], compile_db_dir=None)

    # Rescan only lib_a/ - this must not delete symbols belonging to libxa/,
    # even though "libxa/..." matches the unescaped LIKE pattern "lib_a/%".
    scan_component(conn, root, ["lib_a/"], compile_db_dir=None)

    rows = conn.execute("SELECT name, path FROM symbols").fetchall()
    names = [r["name"] for r in rows]
    assert names.count("libxa_fn") == 1
    assert names.count("lib_a_fn") == 1


def test_scan_uses_clang_when_compile_db_present(tmp_path):
    pytest.importorskip("clang.cindex")
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    src = root / "src" / "ok.c"
    src.write_text("int ok_fn(void) { return 1; }\n")
    compdb_dir = tmp_path / "build"
    compdb_dir.mkdir()
    (compdb_dir / "compile_commands.json").write_text(json.dumps([
        {"directory": str(root), "file": str(src),
         "arguments": ["cc", "-c", str(src)]}]))
    conn = connect(tmp_path / "e.db")

    try:
        stats = scan_component(conn, root, ["src/"],
                               compile_db_dir=compdb_dir)
    except RuntimeError as e:
        pytest.skip(f"libclang unavailable: {e}")

    assert stats["clang"] == 1
    assert stats["tree_sitter"] == 0
    rows = conn.execute(
        "SELECT * FROM symbols WHERE name='ok_fn'").fetchall()
    assert len(rows) == 1
    assert rows[0]["source"] == "clang"
