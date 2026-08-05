import subprocess

from archeon.db import connect
from archeon.retrieval.archaeology import (
    ArtifactRefs,
    artifacts_for_commits,
    file_level_commits,
    shaping_commits,
)


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True,
                   capture_output=True)


def _sha(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True,
                          check=True).stdout.strip()


def _history_repo(tmp_path):
    """Repo with three commits: A creates 10 lines, B edits line 5,
    C inserts two lines at the top (shifting old line 5 to line 7)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    f = repo / "f.c"
    f.write_text("".join(f"l{i}\n" for i in range(1, 11)))
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "A")
    a = _sha(repo)
    f.write_text(f.read_text().replace("l5\n", "l5 modified\n"))
    _git(repo, "commit", "-am", "B")
    b = _sha(repo)
    f.write_text("new1\nnew2\n" + f.read_text())
    _git(repo, "commit", "-am", "C")
    c = _sha(repo)
    return repo, a, b, c


def test_shaping_commits_returns_touching_commits_newest_first(tmp_path):
    repo, a, b, _c = _history_repo(tmp_path)
    # Line 5 as of commit B was touched by B, and created by A.
    assert shaping_commits(repo, "f.c", 5, 5, rev=b) == [b, a]


def test_shaping_commits_anchors_on_the_given_rev(tmp_path):
    # The whole point of Spec B's commit_sha: the same line numbers mean
    # different content at different revs. At HEAD, line 5 is old line 3,
    # which only A ever touched.
    repo, a, b, _c = _history_repo(tmp_path)
    assert shaping_commits(repo, "f.c", 5, 5, rev="HEAD") == [a]
    assert shaping_commits(repo, "f.c", 5, 5, rev=b) == [b, a]


def test_shaping_commits_tracks_content_through_a_shift(tmp_path):
    # C inserted two lines above, so the pinned content sits at line 7 now.
    repo, a, b, _c = _history_repo(tmp_path)
    assert shaping_commits(repo, "f.c", 7, 7, rev="HEAD") == [b, a]


def test_shaping_commits_respects_max_commits(tmp_path):
    repo, _a, b, _c = _history_repo(tmp_path)
    assert shaping_commits(repo, "f.c", 5, 5, rev=b, max_commits=1) == [b]


def test_shaping_commits_returns_empty_for_unknown_path(tmp_path):
    repo, _a, _b, _c = _history_repo(tmp_path)
    assert shaping_commits(repo, "does/not/exist.c", 1, 2) == []


def test_shaping_commits_returns_empty_for_out_of_range_span(tmp_path):
    repo, _a, _b, _c = _history_repo(tmp_path)
    assert shaping_commits(repo, "f.c", 900, 950) == []


def test_file_level_commits_returns_whole_file_history(tmp_path):
    repo, a, b, c = _history_repo(tmp_path)
    assert file_level_commits(repo, "f.c") == [c, b, a]


def test_file_level_commits_respects_max_and_unknown_path(tmp_path):
    repo, _a, _b, c = _history_repo(tmp_path)
    assert file_level_commits(repo, "f.c", max_commits=1) == [c]
    assert file_level_commits(repo, "nope.c") == []


def _lake(tmp_path):
    conn = connect(tmp_path / "e.db")
    for sha in ("sha_a", "sha_b"):
        conn.execute("INSERT INTO commits(sha, author, date, message) "
                     "VALUES (?, 'a', '2026-01-01', 'm')", (sha,))
    return conn


def _link(conn, st, sr, dt, dr, method):
    conn.execute("INSERT INTO links(src_type, src_ref, dst_type, dst_ref, "
                 "method, confidence) VALUES (?,?,?,?,?,1.0)",
                 (st, sr, dt, dr, method))


def test_direct_commit_to_ticket_links_resolve(tmp_path):
    conn = _lake(tmp_path)
    _link(conn, "commit", "sha_a", "ticket", "EMB-1", "key_regex")
    refs = artifacts_for_commits(conn, ["sha_a"])
    assert refs.tickets == {"EMB-1": {"sha_a"}}
    assert refs.prs == {}
    assert refs.unknown == set()


def test_pr_reached_via_merge_sha_and_its_ticket(tmp_path):
    conn = _lake(tmp_path)
    _link(conn, "pr", "42", "commit", "sha_a", "merge_sha")
    _link(conn, "pr", "42", "ticket", "EMB-9", "branch_regex")
    refs = artifacts_for_commits(conn, ["sha_a"])
    assert refs.prs == {42: {"sha_a"}}
    # The PR's ticket is inherited by the commit that reached the PR.
    assert refs.tickets == {"EMB-9": {"sha_a"}}


def test_pr_commits_secondary_path_also_reaches_the_pr(tmp_path):
    # Repos that do not squash-merge expose members via pr_commits instead.
    conn = _lake(tmp_path)
    conn.execute("INSERT INTO pr_commits(pr_number, sha) VALUES (7, 'sha_b')")
    refs = artifacts_for_commits(conn, ["sha_b"])
    assert refs.prs == {7: {"sha_b"}}


def test_support_accumulates_across_shaping_commits(tmp_path):
    conn = _lake(tmp_path)
    _link(conn, "commit", "sha_a", "ticket", "EMB-1", "key_regex")
    _link(conn, "commit", "sha_b", "ticket", "EMB-1", "pr_inherited")
    refs = artifacts_for_commits(conn, ["sha_a", "sha_b"])
    # Two commits reached EMB-1 -> support of 2, which drives corpus ranking.
    assert refs.tickets["EMB-1"] == {"sha_a", "sha_b"}


def test_commits_absent_from_the_lake_are_counted_not_dropped(tmp_path):
    conn = _lake(tmp_path)
    refs = artifacts_for_commits(conn, ["sha_a", "sha_missing"])
    assert refs.unknown == {"sha_missing"}


def test_empty_input_is_an_empty_result(tmp_path):
    refs = artifacts_for_commits(_lake(tmp_path), [])
    assert refs == ArtifactRefs(tickets={}, prs={}, unknown=set())
