import subprocess

from archeon.claims.pin import content_hash, normalize, parse_ref, pin_claims, pin_evidence, is_stale, stale_claims
from archeon.claims.schema import Claim, Evidence


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True,
                   capture_output=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "src" / "f.c").write_text("int f(void) {\n  return 42;\n}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def test_parse_ref_single_line():
    assert parse_ref("src/a.c:12") == ("src/a.c", 12, 12)


def test_parse_ref_range():
    assert parse_ref("src/a.c:12-20") == ("src/a.c", 12, 20)


def test_parse_ref_rejects_garbage():
    assert parse_ref("no-line-here") is None
    assert parse_ref("") is None
    assert parse_ref("src/a.c:5-3") is None  # end before start


def test_normalize_strips_trailing_ws_and_drops_blanks():
    assert normalize(["a  ", "", "  ", "b\t"]) == ["a", "b"]


def test_normalize_keeps_leading_indentation():
    assert normalize(["  return 42;  "]) == ["  return 42;"]


def test_content_hash_stable_under_cosmetic_change():
    assert content_hash(normalize(["x = 1", "y = 2"])) == \
        content_hash(normalize(["x = 1   ", "", "y = 2"]))


def test_content_hash_changes_on_semantic_edit():
    assert content_hash(normalize(["return 42;"])) != \
        content_hash(normalize(["return 7;"]))


def test_pin_evidence_fills_anchor_on_clean_repo(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/f.c:1-3")
    pin_evidence(e, repo)
    assert e.pin_status == "pinned"
    assert e.commit_sha and len(e.commit_sha) == 40
    assert e.blob_sha
    assert e.line_start == 1 and e.line_end == 3
    assert e.content_hash


def test_pin_evidence_marks_dirty_repo(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src" / "untracked.c").write_text("x\n")  # working tree dirty
    e = Evidence(kind="code", ref="src/f.c:2")
    pin_evidence(e, repo)
    assert e.pin_status == "dirty"
    assert e.commit_sha  # still anchored, just provisional


def test_pin_evidence_unpinnable_on_bad_ref(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="this is not a ref")
    pin_evidence(e, repo)
    assert e.pin_status == "unpinnable"
    assert e.commit_sha is None and e.content_hash is None


def test_pin_evidence_unpinnable_out_of_bounds(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/f.c:99-120")
    pin_evidence(e, repo)
    assert e.pin_status == "unpinnable"


def test_pin_evidence_unpinnable_missing_file(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/ghost.c:1")
    pin_evidence(e, repo)
    assert e.pin_status == "unpinnable"


def test_pin_claims_only_touches_code_evidence(tmp_path):
    repo = _repo(tmp_path)
    c = Claim(id="CLM-0001", type="threshold", statement="s",
              evidence=[Evidence(kind="code", ref="src/f.c:1"),
                        Evidence(kind="ticket", ref="EMB-1")])
    pin_claims([c], repo)
    assert c.evidence[0].pin_status == "pinned"
    assert c.evidence[1].pin_status is None  # ticket evidence untouched


def test_not_stale_when_lines_inserted_above(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/f.c:2")  # "  return 42;"
    pin_evidence(e, repo)
    assert e.pin_status == "pinned"
    # Insert a line ABOVE the cited span — line numbers drift, content does not.
    (repo / "src" / "f.c").write_text(
        "int g(void) { return 0; }\nint f(void) {\n  return 42;\n}\n")
    assert is_stale(e, repo) is False


def test_stale_when_cited_line_edited(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/f.c:2")
    pin_evidence(e, repo)
    (repo / "src" / "f.c").write_text("int f(void) {\n  return 7;\n}\n")
    assert is_stale(e, repo) is True


def test_not_stale_on_cosmetic_reflow(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/f.c:1-3")
    pin_evidence(e, repo)
    # Trailing whitespace + an inserted blank line inside the span only.
    (repo / "src" / "f.c").write_text(
        "int f(void) {  \n\n  return 42;   \n}\n")
    assert is_stale(e, repo) is False


def test_stale_when_cited_file_deleted(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/f.c:2")
    pin_evidence(e, repo)
    (repo / "src" / "f.c").unlink()
    assert is_stale(e, repo) is True


def test_legacy_evidence_is_never_stale(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/f.c:2")  # never pinned
    assert e.pin_status is None
    assert is_stale(e, repo) is False


def test_unpinnable_evidence_is_never_stale(tmp_path):
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/ghost.c:1")
    pin_evidence(e, repo)
    assert e.pin_status == "unpinnable"
    assert is_stale(e, repo) is False


def test_stale_claims_selects_only_drifted_claim(tmp_path):
    repo = _repo(tmp_path)
    good = Claim(id="CLM-0001", type="threshold", statement="s1",
                 evidence=[Evidence(kind="code", ref="src/f.c:1")])
    bad = Claim(id="CLM-0002", type="threshold", statement="s2",
                evidence=[Evidence(kind="code", ref="src/f.c:2")])
    pin_claims([good, bad], repo)
    (repo / "src" / "f.c").write_text("int f(void) {\n  return 7;\n}\n")
    result = stale_claims([good, bad], repo)
    assert [c.id for c in result] == ["CLM-0002"]


def test_pin_evidence_resolves_unique_basename_ref(tmp_path):
    # The synthesizer sometimes emits a basename-only ref (e.g. "f.c:1-3")
    # instead of the full repo-relative path. Git can't resolve it from the
    # repo root, so it degrades to unpinnable. Given the known full path set,
    # a bare basename that maps to exactly one known path must pin, and the
    # ref must be rewritten to the resolved full path so is_stale works later.
    repo = _repo(tmp_path)  # has src/f.c
    e = Evidence(kind="code", ref="f.c:1-3")
    pin_evidence(e, repo, known_paths=["src/f.c"])
    assert e.pin_status == "pinned"
    assert e.ref == "src/f.c:1-3"
    assert e.line_start == 1 and e.line_end == 3
    assert e.content_hash


def test_pin_evidence_unpinnable_on_ambiguous_basename(tmp_path):
    # The same basename living in multiple known paths cannot be disambiguated,
    # so it must stay unpinnable rather than guess.
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="f.c:2")
    pin_evidence(e, repo, known_paths=["src/f.c", "lib/f.c"])
    assert e.pin_status == "unpinnable"
    assert e.commit_sha is None and e.content_hash is None


def test_pin_evidence_full_path_wins_over_known_paths(tmp_path):
    # A directly-resolvable full-path ref is anchored as-is; the known_paths
    # resolver is only a fallback and must not rewrite a working ref.
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/f.c:2")
    pin_evidence(e, repo, known_paths=["src/f.c", "other/f.c"])
    assert e.pin_status == "pinned"
    assert e.ref == "src/f.c:2"


def test_pin_claims_resolves_basename_and_staleness_works(tmp_path):
    # End-to-end: a basename ref resolves via known_paths, pins, and — because
    # the ref was rewritten to the full path — is_stale can locate the file and
    # detect a real edit. Without the rewrite is_stale would report stale
    # immediately (no file "f.c" at the repo root).
    repo = _repo(tmp_path)
    c = Claim(id="CLM-0001", type="threshold", statement="s",
              evidence=[Evidence(kind="code", ref="f.c:2")])
    pin_claims([c], repo, known_paths=["src/f.c"])
    e = c.evidence[0]
    assert e.pin_status == "pinned"
    assert e.ref == "src/f.c:2"
    assert is_stale(e, repo) is False
    (repo / "src" / "f.c").write_text("int f(void) {\n  return 7;\n}\n")
    assert is_stale(e, repo) is True


def test_pin_evidence_does_not_repin_already_pinned_evidence(tmp_path):
    # B2: a why-claim's copied code hypothesis is already pinned (it was
    # captured during `synthesize`). If a later commit shifts lines above
    # the cited span, re-pinning against HEAD would silently re-anchor the
    # evidence to different content while still reporting pin_status
    # "pinned" -- exactly the kind of drift `check-staleness` exists to
    # catch. An already-pinned/dirty anchor must never be re-derived.
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/f.c:2")  # "  return 42;"
    pin_evidence(e, repo)
    assert e.pin_status == "pinned"
    original_sha, original_hash = e.commit_sha, e.content_hash
    # Insert a line ABOVE the cited span in a NEW commit: line 2 now holds
    # completely different content ("int f(void) {" instead of the return).
    (repo / "src" / "f.c").write_text(
        "int g(void) { return 0; }\nint f(void) {\n  return 42;\n}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "insert line above")
    pin_evidence(e, repo)          # must be a no-op: already pinned
    assert e.commit_sha == original_sha
    assert e.content_hash == original_hash
    assert e.line_start == 2 and e.line_end == 2


def test_pin_claims_does_not_repin_copied_pinned_evidence(tmp_path):
    # Same scenario through the pin_claims entry point cli.py actually calls.
    repo = _repo(tmp_path)
    e = Evidence(kind="code", ref="src/f.c:2")
    pin_evidence(e, repo)
    original_sha, original_hash = e.commit_sha, e.content_hash
    (repo / "src" / "f.c").write_text(
        "int g(void) { return 0; }\nint f(void) {\n  return 42;\n}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "insert line above")
    c = Claim(id="WHY-0001", type="rationale", statement="s", layer="why",
              evidence=[e])
    pin_claims([c], repo)
    assert c.evidence[0].commit_sha == original_sha
    assert c.evidence[0].content_hash == original_hash


def test_pin_evidence_unpinnable_on_whitespace_only_span(tmp_path):
    # A citation whose span normalizes to empty (blank/whitespace-only lines)
    # has no verifiable content: content_hash would collapse to the empty-string
    # hash and is_stale could never detect an edit. Capture must refuse to pin
    # it, surfacing it as UNPINNABLE rather than silently trusting it.
    repo = _repo(tmp_path)
    (repo / "src" / "blank.c").write_text("   \n\t\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add blank")
    e = Evidence(kind="code", ref="src/blank.c:1-2")
    pin_evidence(e, repo)
    assert e.pin_status == "unpinnable"
    assert e.commit_sha is None and e.content_hash is None
    # And an unpinnable anchor is never reported stale.
    assert is_stale(e, repo) is False
