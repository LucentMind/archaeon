from pathlib import Path

from archaeon.codegraph.ts_scan import ts_symbols

C_SRC = """
struct motor_state { int temp; };

static int clamp(int v) { return v; }

int enter_state(int s) {
    return clamp(s);
}
"""


def test_ts_symbols_functions_and_structs(tmp_path):
    f = tmp_path / "m.c"
    f.write_text(C_SRC)
    syms = ts_symbols(f)
    by_name = {s["name"]: s for s in syms}
    assert by_name["enter_state"]["kind"] == "function"
    assert by_name["clamp"]["kind"] == "function"
    assert by_name["motor_state"]["kind"] == "struct"
    assert by_name["enter_state"]["line"] > by_name["clamp"]["line"]
