from archeon.codegraph.scan import scan_component, _keep
from archeon.db import connect

FN = "int {name}_fn(void) {{ return 1; }}\n"


def _tree(root):
    for rel in ("modules/nav/src/impl.cpp",
                "modules/nav/include/api.hpp",
                "modules/nav/generated/public/cpp/model.hpp",
                "modules/nav/generated/public/cpp/model.cpp",
                "thirdparty/imgui/demo.cpp",
                "tests/t.cpp"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(FN.format(name=rel.split("/")[-1].split(".")[0]))


def _paths(conn):
    return {r["path"] for r in conn.execute("SELECT DISTINCT path FROM symbols")}


def test_keep_predicate_exclude_wins_over_include():
    assert _keep("a/generated/x.cpp", ["**/*.cpp"], ["**/generated/**/*.cpp"]) is False
    assert _keep("a/src/x.cpp", ["**/*.cpp"], ["**/generated/**/*.cpp"]) is True
    assert _keep("a/src/x.hpp", ["**/*.cpp"], None) is False  # include-gated out
    assert _keep("a/src/x.cpp", None, None) is True           # no filters


def test_exclude_drops_generated_cpp_thirdparty_tests(tmp_path):
    root = tmp_path / "repo"
    _tree(root)
    conn = connect(tmp_path / "e.db")
    scan_component(conn, root, ["modules/", "thirdparty/", "tests/"],
                   compile_db_dir=None,
                   exclude=["**/generated/**/*.cpp", "**/thirdparty/**",
                            "**/tests/**"])
    paths = _paths(conn)
    assert "modules/nav/src/impl.cpp" in paths
    assert "modules/nav/include/api.hpp" in paths
    assert "modules/nav/generated/public/cpp/model.hpp" in paths
    assert "modules/nav/generated/public/cpp/model.cpp" not in paths
    assert "thirdparty/imgui/demo.cpp" not in paths
    assert "tests/t.cpp" not in paths


def test_include_gate_keeps_only_headers(tmp_path):
    root = tmp_path / "repo"
    _tree(root)
    conn = connect(tmp_path / "e.db")
    scan_component(conn, root, ["modules/"], compile_db_dir=None,
                   include=["**/*.hpp"])
    paths = _paths(conn)
    assert all(p.endswith(".hpp") for p in paths)
    assert "modules/nav/include/api.hpp" in paths


def test_no_filters_matches_unfiltered_scan(tmp_path):
    root = tmp_path / "repo"
    _tree(root)
    a = connect(tmp_path / "a.db")
    b = connect(tmp_path / "b.db")
    scan_component(a, root, ["modules/"], compile_db_dir=None)
    scan_component(b, root, ["modules/"], compile_db_dir=None,
                   include=None, exclude=None)
    assert _paths(a) == _paths(b)
