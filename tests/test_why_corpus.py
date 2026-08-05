from archeon.claims.schema import Claim, Evidence
from archeon.claims.why_corpus import (
    build_corpus, collect_artifacts, spans_for_claims)
from archeon.db import connect
from archeon.retrieval.archaeology import ArtifactRefs


def _pinned(ref, sha, start, end):
    return Evidence(kind="code", ref=ref, role="primary", commit_sha=sha,
                    line_start=start, line_end=end, pin_status="pinned")


def test_spans_come_from_pinned_code_evidence_only(tmp_path):
    c = Claim(id="CLM-0001", type="threshold", statement="s", evidence=[
        _pinned("src/a.c:5-9", "sha1", 5, 9),
        Evidence(kind="code", ref="src/b.c:1", pin_status="unpinnable"),
        Evidence(kind="ticket", ref="EMB-1", role="corroborating"),
    ])
    assert spans_for_claims([c]) == [("src/a.c", 5, 9, "sha1")]


def test_spans_are_deduped_across_claims():
    ev = _pinned("src/a.c:5-9", "sha1", 5, 9)
    a = Claim(id="CLM-0001", type="threshold", statement="s", evidence=[ev])
    b = Claim(id="CLM-0002", type="invariant", statement="t", evidence=[ev])
    assert spans_for_claims([a, b]) == [("src/a.c", 5, 9, "sha1")]


def _lake(tmp_path):
    conn = connect(tmp_path / "e.db")
    conn.execute("INSERT INTO tickets(key, summary, description, status, "
                 "created, resolved) VALUES ('EMB-1', 'Sum one', "
                 "'Why one happened', 'Done', '2026-01-01', '2026-02-01')")
    conn.execute("INSERT INTO tickets(key, summary, description, status, "
                 "created, resolved) VALUES ('EMB-2', 'Sum two', "
                 "'Why two happened', 'Done', '2026-01-01', '2026-03-01')")
    conn.execute("INSERT INTO prs(number, title, body, author, branch, "
                 "merged_at, merge_sha) VALUES (42, 'PR title', 'PR body', "
                 "'a', 'b', '2026-02-15', 'sha_m')")
    conn.execute("INSERT INTO pr_comments(id, pr_number, author, body, path, "
                 "created) VALUES ('c1', 42, 'a', 'Review rationale', "
                 "'src/a.c', '2026-02-16')")
    return conn


def test_corpus_ranks_by_support_then_recency(tmp_path):
    conn = _lake(tmp_path)
    refs = ArtifactRefs(tickets={"EMB-1": {"s1"}, "EMB-2": {"s1", "s2"}},
                        prs={}, unknown=set())
    _text, manifest = build_corpus(conn, refs, token_budget=10000)
    # EMB-2 has support 2 and outranks EMB-1's support 1.
    assert [m["ref"] for m in manifest] == ["EMB-2", "EMB-1"]
    assert manifest[0]["support"] == 2


def _ticket(conn, key, created=None, resolved=None):
    conn.execute(
        "INSERT INTO tickets(key, summary, description, status, created, "
        "resolved) VALUES (?, 'sum', 'why', 'Done', ?, ?)",
        (key, created, resolved))


def test_ranking_support_dominates_timestamp(tmp_path):
    conn = connect(tmp_path / "e.db")
    # ZZZ-1 is older and alphabetically last, but has higher support so it
    # must still outrank AAA-2, which is newer and alphabetically first.
    _ticket(conn, "ZZZ-1", resolved="2026-01-01")
    _ticket(conn, "AAA-2", resolved="2026-06-01")
    refs = ArtifactRefs(tickets={"ZZZ-1": {"s1", "s2"}, "AAA-2": {"s1"}},
                        prs={}, unknown=set())
    _text, manifest = build_corpus(conn, refs, token_budget=10000)
    assert [m["ref"] for m in manifest] == ["ZZZ-1", "AAA-2"]


def test_ranking_newer_timestamp_first_within_support_group(tmp_path):
    conn = connect(tmp_path / "e.db")
    # Same support (1) for both; alphabetical order would put BBB-1 first,
    # but recency must decide within the tied support group.
    _ticket(conn, "BBB-1", resolved="2026-01-01")
    _ticket(conn, "BBB-2", resolved="2026-06-01")
    refs = ArtifactRefs(tickets={"BBB-1": {"s1"}, "BBB-2": {"s2"}},
                        prs={}, unknown=set())
    _text, manifest = build_corpus(conn, refs, token_budget=10000)
    assert [m["ref"] for m in manifest] == ["BBB-2", "BBB-1"]


def test_ranking_empty_timestamp_sorts_last_within_support_group(tmp_path):
    conn = connect(tmp_path / "e.db")
    # Same support (1); CCC-1 has no created/resolved date at all, so it
    # must sort after CCC-2 even though it is alphabetically first.
    _ticket(conn, "CCC-1")
    _ticket(conn, "CCC-2", resolved="2026-01-01")
    refs = ArtifactRefs(tickets={"CCC-1": {"s1"}, "CCC-2": {"s2"}},
                        prs={}, unknown=set())
    _text, manifest = build_corpus(conn, refs, token_budget=10000)
    assert [m["ref"] for m in manifest] == ["CCC-2", "CCC-1"]


def test_ranking_ref_ascending_breaks_full_tie(tmp_path):
    conn = connect(tmp_path / "e.db")
    # Identical support (1) and identical timestamp -> full tie broken by
    # ref ascending, i.e. DDD-1 before DDD-2 even though it was inserted
    # second and registered second in the refs dict below.
    _ticket(conn, "DDD-2", resolved="2026-01-01")
    _ticket(conn, "DDD-1", resolved="2026-01-01")
    refs = ArtifactRefs(tickets={"DDD-2": {"s1"}, "DDD-1": {"s2"}},
                        prs={}, unknown=set())
    _text, manifest = build_corpus(conn, refs, token_budget=10000)
    assert [m["ref"] for m in manifest] == ["DDD-1", "DDD-2"]


def test_corpus_includes_pr_body_and_its_comments(tmp_path):
    conn = _lake(tmp_path)
    refs = ArtifactRefs(tickets={}, prs={42: {"s1"}}, unknown=set())
    text, manifest = build_corpus(conn, refs, token_budget=10000)
    kinds = {m["kind"] for m in manifest}
    assert kinds == {"pr", "pr_comment"}
    assert "pr:42" in {m["ref"] for m in manifest}
    assert "pr_comment:c1" in {m["ref"] for m in manifest}
    assert "PR body" in text and "Review rationale" in text


def test_ticket_header_carries_the_manifest_ref_verbatim(tmp_path):
    # B4: the synthesis prompt tells the model to cite the ref "exactly as
    # shown in the === header". The header must therefore render the exact
    # same string the manifest uses as `ref`, or a model that follows the
    # instruction literally produces a ref _evidence_kind cannot classify.
    conn = _lake(tmp_path)
    refs = ArtifactRefs(tickets={"EMB-1": {"s1"}}, prs={}, unknown=set())
    text, manifest = build_corpus(conn, refs, token_budget=10000)
    ref = manifest[0]["ref"]
    assert ref == "EMB-1"
    assert f"=== {ref} " in text


def test_pr_header_carries_the_manifest_ref_verbatim(tmp_path):
    conn = _lake(tmp_path)
    refs = ArtifactRefs(tickets={}, prs={42: {"s1"}}, unknown=set())
    text, manifest = build_corpus(conn, refs, token_budget=10000)
    pr_ref = next(m["ref"] for m in manifest if m["kind"] == "pr")
    assert pr_ref == "pr:42"
    assert f"=== {pr_ref} " in text


def test_pr_comment_header_carries_the_manifest_ref_verbatim(tmp_path):
    conn = _lake(tmp_path)
    refs = ArtifactRefs(tickets={}, prs={42: {"s1"}}, unknown=set())
    text, manifest = build_corpus(conn, refs, token_budget=10000)
    comment_ref = next(m["ref"] for m in manifest if m["kind"] == "pr_comment")
    assert comment_ref == "pr_comment:c1"
    assert f"=== {comment_ref} " in text


def test_pr_comment_inherits_its_prs_support(tmp_path):
    conn = _lake(tmp_path)
    refs = ArtifactRefs(tickets={}, prs={42: {"s1", "s2"}}, unknown=set())
    _text, manifest = build_corpus(conn, refs, token_budget=10000)
    by_ref = {m["ref"]: m for m in manifest}
    assert by_ref["pr_comment:c1"]["support"] == 2


def test_corpus_truncates_to_token_budget(tmp_path):
    conn = _lake(tmp_path)
    refs = ArtifactRefs(tickets={"EMB-1": {"s1"}, "EMB-2": {"s1", "s2"}},
                        prs={}, unknown=set())
    _text, manifest = build_corpus(conn, refs, token_budget=1)
    # Always emits at least the top-ranked artifact, never more here.
    assert [m["ref"] for m in manifest] == ["EMB-2"]


def test_missing_artifact_rows_are_skipped_not_fatal(tmp_path):
    conn = _lake(tmp_path)
    refs = ArtifactRefs(tickets={"NOPE-9": {"s1"}}, prs={999: {"s1"}},
                        unknown=set())
    text, manifest = build_corpus(conn, refs, token_budget=10000)
    assert manifest == []
    assert text == ""


def test_collect_artifacts_walks_spans_to_artifacts(tmp_path, monkeypatch):
    import archeon.claims.why_corpus as mod
    conn = _lake(tmp_path)
    conn.execute("INSERT INTO commits(sha, author, date, message) "
                 "VALUES ('sha_x', 'a', '2026-01-01', 'm')")
    conn.execute("INSERT INTO links(src_type, src_ref, dst_type, dst_ref, "
                 "method, confidence) VALUES "
                 "('commit','sha_x','ticket','EMB-1','key_regex',1.0)")
    calls = []

    def fake_shaping_commits(*args, **kwargs):
        calls.append((args, kwargs))
        return ["sha_x"]

    monkeypatch.setattr(mod, "shaping_commits", fake_shaping_commits)
    c = Claim(id="CLM-0001", type="threshold", statement="s",
              evidence=[_pinned("src/a.c:5-9", "sha1", 5, 9)])
    refs = collect_artifacts(conn, tmp_path, [c],
                             {"max_commits_per_span": 7})
    assert refs.tickets == {"EMB-1": {"sha_x"}}
    # Prove the exact args threaded through, not just that *something* was
    # called: the repo-relative path, the pinned start/end, the evidence's
    # own commit_sha as `rev` (identical line numbers mean different content
    # at different revisions), and max_commits from why_cfg.
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == tmp_path
    assert args[1] == "src/a.c"
    assert args[2] == 5
    assert args[3] == 9
    assert kwargs["rev"] == "sha1"
    assert kwargs["max_commits"] == 7


def test_collect_artifacts_falls_back_to_file_level_commits(tmp_path,
                                                             monkeypatch):
    import archeon.claims.why_corpus as mod
    conn = _lake(tmp_path)
    conn.execute("INSERT INTO commits(sha, author, date, message) "
                 "VALUES ('sha_y', 'a', '2026-01-01', 'm')")
    conn.execute("INSERT INTO links(src_type, src_ref, dst_type, dst_ref, "
                 "method, confidence) VALUES "
                 "('commit','sha_y','ticket','EMB-1','key_regex',1.0)")
    # shaping_commits finds nothing (e.g. the path was renamed or deleted at
    # this rev); the fallback to file_level_commits must still surface the
    # claim's artifact rather than silently losing it.
    monkeypatch.setattr(mod, "shaping_commits", lambda *a, **k: [])
    monkeypatch.setattr(mod, "file_level_commits", lambda *a, **k: ["sha_y"])
    c = Claim(id="CLM-0001", type="threshold", statement="s",
              evidence=[_pinned("src/a.c:5-9", "sha1", 5, 9)])
    refs = collect_artifacts(conn, tmp_path, [c],
                             {"max_commits_per_span": 50})
    assert refs.tickets == {"EMB-1": {"sha_y"}}
