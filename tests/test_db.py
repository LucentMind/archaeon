import sqlite3

import pytest

from archeon.db import connect, connect_readonly


def test_connect_creates_schema(tmp_path):
    conn = connect(tmp_path / "evidence.db")
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r["name"] for r in rows}
    assert {"commits", "commit_files", "tickets", "prs", "pr_comments",
            "wiki_pages", "symbols", "scan_gaps", "coupling",
            "links"} <= names


def test_connect_is_idempotent_and_persists(tmp_path):
    db = tmp_path / "evidence.db"
    conn = connect(db)
    conn.execute("INSERT INTO commits(sha, author, date, message) "
                 "VALUES ('abc', 'a', '2026-01-01', 'msg')")
    conn.commit()
    conn.close()
    conn2 = connect(db)
    row = conn2.execute("SELECT message FROM commits WHERE sha='abc'").fetchone()
    assert row["message"] == "msg"


def test_connect_readonly_reads_existing_db(tmp_path):
    db = tmp_path / "evidence.db"
    conn = connect(db)
    conn.execute("INSERT INTO commits(sha, author, date, message) "
                 "VALUES ('abc', 'a', '2026-01-01', 'msg')")
    conn.commit()
    conn.close()
    ro = connect_readonly(db)
    row = ro.execute("SELECT message FROM commits WHERE sha='abc'").fetchone()
    assert row["message"] == "msg"  # Row factory works on the read-only conn


def test_connect_readonly_rejects_writes(tmp_path):
    db = tmp_path / "evidence.db"
    connect(db).close()
    ro = connect_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO commits(sha, author, date, message) "
                   "VALUES ('x', 'a', '2026-01-01', 'm')")
        ro.commit()


def test_connect_readonly_missing_db_raises(tmp_path):
    # A read-only open must not silently create an empty DB for the review tool.
    with pytest.raises(sqlite3.OperationalError):
        connect_readonly(tmp_path / "does-not-exist.db")


def test_retrieval_tables_created(tmp_path):
    conn = connect(tmp_path / "e.db")
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("symbol_edges", "file_edges", "symbol_vectors",
              "clusters", "cluster_members"):
        assert t in names
