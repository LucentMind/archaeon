from pathlib import Path

from archaeon import config as config_mod


def test_scoped_config_loads_with_exclude():
    cfg = config_mod.load(Path("archaeon.example.scoped.toml"))
    excl = cfg["component"].get("exclude")
    assert excl is not None
    assert "**/generated/**/*.cpp" in excl
