import subprocess
from pathlib import Path

from archaeon.connectors.git_connector import ingest_git
from archaeon.db import connect


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "src").mkdir()
    (repo / "src" / "a.c").write_text("int a;\n")
    (repo / "other.txt").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "EMB-1: initial\n\nbody line")
    (repo / "src" / "a.c").write_text("int a;\nint b;\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tweak a")
    return repo


def test_ingest_git_inserts_commits_and_files(tmp_path):
    repo = _make_repo(tmp_path)
    conn = connect(tmp_path / "e.db")
    n = ingest_git(conn, repo)
    assert n == 2
    msgs = [r["message"] for r in
            conn.execute("SELECT message FROM commits ORDER BY date")]
    assert any("EMB-1: initial" in m for m in msgs)
    files = conn.execute("SELECT DISTINCT path FROM commit_files").fetchall()
    assert {"src/a.c", "other.txt"} == {r["path"] for r in files}


def test_path_prefix_filter(tmp_path):
    repo = _make_repo(tmp_path)
    conn = connect(tmp_path / "e.db")
    n = ingest_git(conn, repo, path_prefixes=["src/"])
    assert n == 2
    files = {r["path"] for r in
             conn.execute("SELECT DISTINCT path FROM commit_files")}
    assert files == {"src/a.c"}


def _add_bot_commit(repo: Path) -> None:
    (repo / "src" / "a.c").write_text("int a;\nint b;\nint c;\n")
    _git(repo, "add", ".")
    subprocess.run(
        ["git", "-C", str(repo), "commit",
         "--author=dependabot[bot] <bot@github.com>",
         "-m", "Bump lodash from 1.0 to 2.0"],
        check=True, capture_output=True)


def test_bot_commits_excluded_by_default(tmp_path):
    repo = _make_repo(tmp_path)
    _add_bot_commit(repo)
    conn = connect(tmp_path / "e.db")
    n = ingest_git(conn, repo)
    assert n == 2  # only the two human commits
    authors = {r["author"] for r in conn.execute("SELECT author FROM commits")}
    assert "dependabot[bot]" not in authors
    msgs = [r["message"] for r in conn.execute("SELECT message FROM commits")]
    assert not any("Bump lodash" in m for m in msgs)


def test_bot_filter_can_be_disabled(tmp_path):
    repo = _make_repo(tmp_path)
    _add_bot_commit(repo)
    conn = connect(tmp_path / "e.db")
    n = ingest_git(conn, repo, exclude_authors=[],
                   exclude_message_patterns=[])
    assert n == 3  # the bot commit is now kept


from archaeon.connectors.git_connector import (  # noqa: E402
    blob_sha, head_sha, is_dirty, show_file)


def test_head_sha_returns_full_sha(tmp_path):
    repo = _make_repo(tmp_path)
    sha = head_sha(repo)
    assert sha is not None and len(sha) == 40


def test_show_file_returns_lines_at_head(tmp_path):
    repo = _make_repo(tmp_path)
    assert show_file(repo, "src/a.c", "HEAD") == ["int a;", "int b;"]


def test_blob_sha_present_and_none_for_missing(tmp_path):
    repo = _make_repo(tmp_path)
    assert blob_sha(repo, "src/a.c", "HEAD")
    assert blob_sha(repo, "nope.c", "HEAD") is None
    assert show_file(repo, "nope.c", "HEAD") is None


def test_is_dirty_reflects_working_tree(tmp_path):
    repo = _make_repo(tmp_path)
    assert is_dirty(repo) is False
    (repo / "src" / "a.c").write_text("int a;\nint b;\nint c;\n")
    assert is_dirty(repo) is True
