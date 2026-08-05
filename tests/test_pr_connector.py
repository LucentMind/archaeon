import json

from archeon.connectors import pr_connector
from archeon.connectors.pr_connector import ingest_prs
from archeon.db import connect

PR482 = {"number": 482, "title": "Add debounce", "body": "Fixes EMB-2",
         "user": {"login": "dev1"}, "head": {"ref": "feature/EMB-2-debounce"},
         "merged_at": "2025-04-01T00:00:00Z", "merge_commit_sha": "beef"}
PR483 = {"number": 483, "title": "WIP", "body": "", "user": {"login": "dev2"},
         "head": {"ref": "wip"}, "merged_at": None, "merge_commit_sha": None}
COMMENTS = [{"id": 9001, "user": {"login": "dev2"},
             "body": "added debounce so transient spikes don't trip shutdown",
             "path": "fault_handler.c",
             "created_at": "2025-03-30T00:00:00Z"}]
PR_COMMITS = [{"sha": "c1"}, {"sha": "c2"}]


def fake_fetch(endpoint, params):
    # both component commits c1, c2 belong to PR 482; c3 belongs to unmerged 483
    if endpoint.endswith("/commits/c1/pulls") or \
            endpoint.endswith("/commits/c2/pulls"):
        return [PR482] if params["page"] == 1 else []
    if endpoint.endswith("/commits/c3/pulls"):
        return [PR483] if params["page"] == 1 else []
    if endpoint.endswith("/pulls/482/comments"):
        return COMMENTS if params["page"] == 1 else []
    if endpoint.endswith("/pulls/482/commits"):
        return PR_COMMITS if params["page"] == 1 else []
    raise AssertionError(f"unexpected endpoint {endpoint}")


def test_ingest_prs_from_component_commits(tmp_path):
    conn = connect(tmp_path / "e.db")
    for sha in ("c1", "c2", "c3"):
        conn.execute("INSERT INTO commits(sha, author, date, message) "
                     "VALUES (?, 'a', 'd', 'm')", (sha,))
    conn.commit()
    n = ingest_prs(conn, "org/repo", fetch=fake_fetch)
    assert n == 1  # PR 482 stored once (deduped across c1/c2); 483 unmerged
    pr = conn.execute("SELECT * FROM prs WHERE number=482").fetchone()
    assert pr["merge_sha"] == "beef"
    assert pr["branch"] == "feature/EMB-2-debounce"
    assert conn.execute("SELECT COUNT(*) AS c FROM prs WHERE number=483"
                        ).fetchone()["c"] == 0
    c = conn.execute("SELECT * FROM pr_comments").fetchone()
    assert c["pr_number"] == 482 and "debounce" in c["body"]
    members = {r["sha"] for r in
               conn.execute("SELECT sha FROM pr_commits WHERE pr_number=482")}
    assert members == {"c1", "c2"}


def test_gh_fetch_builds_get_request(monkeypatch):
    calls = {}

    def fake_run(args, **kwargs):
        calls["args"] = args

        class R:
            stdout = json.dumps([{"number": 1}])
        return R()

    monkeypatch.setattr(pr_connector.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(pr_connector.subprocess, "run", fake_run)
    out = pr_connector._gh_fetch("repos/org/repo/pulls",
                                 {"state": "closed", "page": 1},
                                 hostname="github.corp")
    assert out == [{"number": 1}]
    args = calls["args"]
    assert args[:4] == ["gh", "api", "-X", "GET"]
    assert "--hostname" in args and "github.corp" in args
    assert "repos/org/repo/pulls" in args
    assert "-f" in args and "state=closed" in args


def test_gh_fetch_missing_cli_raises(monkeypatch):
    monkeypatch.setattr(pr_connector.shutil, "which", lambda _: None)
    try:
        pr_connector._gh_fetch("repos/org/repo/pulls", {})
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "gh" in str(e)
