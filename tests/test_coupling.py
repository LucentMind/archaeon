from archaeon.analysis.coupling import compute_coupling, strongest_pairs
from archaeon.db import connect


def _seed(conn):
    rows = [("c1", "a.c"), ("c1", "b.c"),
            ("c2", "a.c"), ("c2", "b.c"),
            ("c3", "a.c"), ("c3", "z.c")]
    for sha, path in rows:
        conn.execute("INSERT OR IGNORE INTO commits(sha, author, date, "
                     "message) VALUES (?, '', '', '')", (sha,))
        conn.execute("INSERT INTO commit_files(sha, path, additions, "
                     "deletions) VALUES (?, ?, 1, 0)", (sha, path))
    conn.commit()


def test_compute_coupling_counts_pairs(tmp_path):
    conn = connect(tmp_path / "e.db")
    _seed(conn)
    n = compute_coupling(conn)
    assert n == 2  # distinct pairs: (a.c, b.c) and (a.c, z.c)
    ab = conn.execute("SELECT * FROM coupling WHERE path_a='a.c' AND "
                      "path_b='b.c'").fetchone()
    assert ab["co_changes"] == 2
    assert ab["support_a"] == 3   # a.c changed in 3 commits
    assert ab["support_b"] == 2   # b.c changed in 2 commits
    top = strongest_pairs(conn, limit=1)[0]
    assert (top["path_a"], top["path_b"]) == ("a.c", "b.c")


def test_bulk_commits_skipped(tmp_path):
    conn = connect(tmp_path / "e.db")
    conn.execute("INSERT INTO commits(sha, author, date, message) "
                 "VALUES ('big', '', '', '')")
    for i in range(40):
        conn.execute("INSERT INTO commit_files(sha, path, additions, "
                     "deletions) VALUES ('big', ?, 1, 0)", (f"f{i}.c",))
    conn.commit()
    assert compute_coupling(conn, max_files_per_commit=30) == 0
