import sqlite3
from importlib import resources
from pathlib import Path
from urllib.request import pathname2url


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    schema = resources.files("archeon").joinpath("schema.sql").read_text()
    conn.executescript(schema)
    conn.commit()
    return conn


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    """Open an existing evidence DB read-only.

    Used by the review UI, which only reads Spec A's cluster metadata and
    symbol fan-in. Opening `mode=ro` guarantees the review process can never
    write to the DB and, unlike `connect`, does not create an empty DB when
    the file is missing (a missing DB raises instead of silently degrading).
    """
    uri = "file:" + pathname2url(str(Path(path).resolve())) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn
