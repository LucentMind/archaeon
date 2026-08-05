from archeon.analysis.link_heuristics import extract_heuristic_links
from archeon.db import connect


def _seed(conn):
    conn.execute("INSERT INTO tickets(key, summary) VALUES ('EMB-1', 's')")
    conn.execute("INSERT INTO tickets(key, summary) VALUES ('EMB-2', 's')")
    conn.execute("INSERT INTO commits(sha, author, date, message) VALUES "
                 "('c1', 'a', 'd', 'EMB-1: fix thermal shutdown')")
    conn.execute("INSERT INTO commits(sha, author, date, message) VALUES "
                 "('c2', 'a', 'd', 'refactor, see EMB-99 maybe')")
    conn.execute("INSERT INTO prs(number, title, body, author, branch, "
                 "merged_at, merge_sha) VALUES (482, 'Add debounce', "
                 "'Fixes EMB-2', 'dev', 'wip', 'd', 'c2')")
    conn.commit()


def test_extract_heuristic_links(tmp_path):
    conn = connect(tmp_path / "e.db")
    _seed(conn)
    n = extract_heuristic_links(conn, ["EMB"])
    rows = {(r["src_type"], r["src_ref"], r["dst_type"], r["dst_ref"],
             r["method"]) for r in conn.execute("SELECT * FROM links")}
    assert ("commit", "c1", "ticket", "EMB-1", "key_regex") in rows
    assert ("pr", "482", "ticket", "EMB-2", "key_regex") in rows
    assert ("pr", "482", "commit", "c2", "merge_sha") in rows
    # PR 482's ticket is inherited to its merge/squash commit c2
    assert ("commit", "c2", "ticket", "EMB-2", "pr_inherited") in rows
    assert not any(r[3] == "EMB-99" for r in rows)  # unknown ticket ignored
    assert n == len(rows) == 4
    # idempotent
    assert extract_heuristic_links(conn, ["EMB"]) == 0


def test_ticket_from_branch_name(tmp_path):
    conn = connect(tmp_path / "e.db")
    conn.execute("INSERT INTO tickets(key, summary) VALUES ('EMB-3', 's')")
    # PR names its ticket only in the branch, not the title/body
    conn.execute("INSERT INTO prs(number, title, body, author, branch, "
                 "merged_at, merge_sha) VALUES (490, 'cleanup', '', 'dev', "
                 "'feature/EMB-3-thermal', 'd', NULL)")
    conn.commit()
    extract_heuristic_links(conn, ["EMB"])
    rows = {(r["src_ref"], r["dst_ref"], r["method"])
            for r in conn.execute("SELECT * FROM links WHERE src_type='pr'")}
    assert ("490", "EMB-3", "branch_regex") in rows


def test_pr_ticket_inherited_to_member_commits(tmp_path):
    conn = connect(tmp_path / "e.db")
    conn.execute("INSERT INTO tickets(key, summary) VALUES ('EMB-5', 's')")
    # two commits with no ticket key in their own messages
    conn.execute("INSERT INTO commits(sha, author, date, message) VALUES "
                 "('m1', 'a', 'd', 'add debounce')")
    conn.execute("INSERT INTO commits(sha, author, date, message) VALUES "
                 "('m2', 'a', 'd', 'tune threshold')")
    conn.execute("INSERT INTO prs(number, title, body, author, branch, "
                 "merged_at, merge_sha) VALUES (500, 'EMB-5 thermal', '', "
                 "'dev', 'wip', 'd', NULL)")
    conn.execute("INSERT INTO pr_commits(pr_number, sha) VALUES (500, 'm1')")
    conn.execute("INSERT INTO pr_commits(pr_number, sha) VALUES (500, 'm2')")
    conn.commit()
    extract_heuristic_links(conn, ["EMB"])
    inherited = {r["src_ref"] for r in conn.execute(
        "SELECT src_ref FROM links WHERE dst_ref='EMB-5' AND "
        "method='pr_inherited'")}
    assert inherited == {"m1", "m2"}  # both member commits attributed


def test_inheritance_skips_commits_not_in_repo(tmp_path):
    # rebase/squash can leave member SHAs that aren't on main; those don't link
    conn = connect(tmp_path / "e.db")
    conn.execute("INSERT INTO tickets(key, summary) VALUES ('EMB-6', 's')")
    conn.execute("INSERT INTO prs(number, title, body, author, branch, "
                 "merged_at, merge_sha) VALUES (510, 'EMB-6 x', '', 'dev', "
                 "'wip', 'd', NULL)")
    conn.execute("INSERT INTO pr_commits(pr_number, sha) VALUES (510, 'gone')")
    conn.commit()
    extract_heuristic_links(conn, ["EMB"])
    n = conn.execute("SELECT COUNT(*) AS c FROM links WHERE "
                     "method='pr_inherited'").fetchone()["c"]
    assert n == 0  # 'gone' isn't in commits, so nothing inherited
