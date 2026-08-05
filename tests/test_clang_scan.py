import json

import pytest

from archeon.codegraph.clang_scan import clang_symbols

C_SRC = "int enter_state(int s) { return s; }\n"


def test_clang_symbols(tmp_path):
    pytest.importorskip("clang.cindex")
    src = tmp_path / "m.c"
    src.write_text(C_SRC)
    (tmp_path / "compile_commands.json").write_text(json.dumps([
        {"directory": str(tmp_path), "file": str(src),
         "arguments": ["cc", "-c", str(src)]}]))
    try:
        syms = clang_symbols(src, tmp_path)
    except RuntimeError as e:
        pytest.skip(f"libclang unavailable: {e}")
    names = {s["name"] for s in syms}
    assert "enter_state" in names
    fn = next(s for s in syms if s["name"] == "enter_state")
    assert fn["kind"] == "function"
    assert fn["line"] == 1


def test_clang_symbols_missing_compile_command(tmp_path):
    pytest.importorskip("clang.cindex")
    src = tmp_path / "orphan.c"
    src.write_text(C_SRC)
    (tmp_path / "compile_commands.json").write_text("[]")
    with pytest.raises(RuntimeError):
        clang_symbols(src, tmp_path)
