from pathlib import Path

from archeon import config as config_mod


def test_scoped_config_loads_with_exclude():
    cfg = config_mod.load(Path("archeon.example.scoped.toml"))
    excl = cfg["component"].get("exclude")
    assert excl is not None
    assert "**/generated/**/*.cpp" in excl
