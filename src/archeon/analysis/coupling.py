import sqlite3
from collections import Counter
from itertools import combinations


def compute_coupling(conn: sqlite3.Connection,
                     max_files_per_commit: int = 30) -> int:
    conn.execute("DELETE FROM coupling")
    support: Counter = Counter()
    pairs: Counter = Counter()
    by_commit: dict[str, list[str]] = {}
    for row in conn.execute("SELECT sha, path FROM commit_files"):
        by_commit.setdefault(row["sha"], []).append(row["path"])
    for files in by_commit.values():
        if len(files) > max_files_per_commit:
            continue
        for path in files:
            support[path] += 1
        for a, b in combinations(sorted(files), 2):
            pairs[(a, b)] += 1
    conn.executemany(
        "INSERT INTO coupling(path_a, path_b, co_changes, support_a, "
        "support_b) VALUES (?, ?, ?, ?, ?)",
        [(a, b, co, support[a], support[b])
         for (a, b), co in pairs.items()])
    conn.commit()
    return len(pairs)


def strongest_pairs(conn: sqlite3.Connection, limit: int = 20):
    return conn.execute(
        "SELECT *, co_changes * 1.0 / MIN(support_a, support_b) AS strength "
        "FROM coupling ORDER BY strength DESC, co_changes DESC LIMIT ?",
        (limit,)).fetchall()
