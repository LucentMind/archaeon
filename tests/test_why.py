import json

from archaeon.claims.schema import (
    CODE_INFERRED_MAX_CONFIDENCE, Claim, Evidence)
from archaeon.claims.why import (
    artifact_body, ground_citations, normalize_text, synthesize_why_claims,
    verify_why_claims)
from archaeon.db import connect


def _lake(tmp_path):
    conn = connect(tmp_path / "e.db")
    conn.execute("INSERT INTO tickets(key, summary, description, status, "
                 "created, resolved) VALUES ('EMB-1', 'Smoothen puck', "
                 "'Statuses arrive irregularly, so the puck jitters.', "
                 "'Done', '2026-01-01', '2026-02-01')")
    # Real PR bodies in the lake contain literal CRLF. This MUST be bound as
    # a parameter: SQLite does not interpret backslash escapes inside a SQL
    # string literal, so an inline '...\r\n...' would store two literal
    # backslashes and would not exercise the CRLF path at all.
    conn.execute("INSERT INTO prs(number, title, body, author, branch, "
                 "merged_at, merge_sha) VALUES (42, 'Improve smoothness', "
                 "?, 'a', 'b', '2026-02-15', 'sha_m')",
                 ("Fixes jitter\r\non slow devices",))
    conn.execute("INSERT INTO pr_comments(id, pr_number, author, body, path, "
                 "created) VALUES ('c1', 42, 'a', 'We chose lerp for cost', "
                 "'src/a.c', '2026-02-16')")
    return conn


def _why(evidence, cid="WHY-0001"):
    return Claim(id=cid, type="rationale", statement="s", layer="why",
                 confidence=0.7, explains=["CLM-0001"], evidence=[
                     Evidence(kind="code", ref="src/a.c:5", role="primary"),
                     *evidence])


def test_normalize_text_folds_crlf_whitespace_and_case():
    assert normalize_text("Fixes jitter\r\non  slow\tdevices") == \
        normalize_text("fixes JITTER on slow devices")


def test_artifact_body_resolves_each_ref_kind(tmp_path):
    conn = _lake(tmp_path)
    assert "jitters" in artifact_body(conn, "EMB-1")
    assert "jitter" in artifact_body(conn, "pr:42")
    assert "lerp" in artifact_body(conn, "pr_comment:c1")
    assert artifact_body(conn, "NOPE-1") is None
    assert artifact_body(conn, "pr:999") is None
    assert artifact_body(conn, "garbage") is None


def test_verbatim_excerpt_is_grounded(tmp_path):
    conn = _lake(tmp_path)
    c = _why([Evidence(kind="ticket", ref="EMB-1", role="corroborating",
                       excerpt="Statuses arrive irregularly")])
    stats = ground_citations([c], conn)
    assert stats["grounded"] == 1 and stats["dropped"] == 0
    assert c.corroboration == "corroborated"
    assert c.confidence == 0.7          # untouched


def test_excerpt_surviving_crlf_and_reflow_is_grounded(tmp_path):
    conn = _lake(tmp_path)
    c = _why([Evidence(kind="pr", ref="pr:42", role="corroborating",
                       excerpt="Fixes jitter on slow devices")])
    stats = ground_citations([c], conn)
    # Strengthened: the brief's version only checked corroboration, which a
    # broken implementation that always sets "corroborated" would still
    # pass. Pin down the counters and that the pr evidence actually survived
    # (rather than, say, corroboration being set by some other path while
    # the citation itself was silently dropped).
    assert stats["grounded"] == 1 and stats["dropped"] == 0
    assert c.corroboration == "corroborated"
    assert [e.kind for e in c.evidence] == ["code", "pr"]


def test_fabricated_excerpt_is_dropped(tmp_path):
    conn = _lake(tmp_path)
    c = _why([Evidence(kind="ticket", ref="EMB-1", role="corroborating",
                       excerpt="The team agreed to a 50ms budget")])
    stats = ground_citations([c], conn)
    assert stats["dropped"] == 1
    assert [e.kind for e in c.evidence] == ["code"]   # only hypothesis left
    assert c.corroboration == "code_inferred"


def test_citation_to_a_missing_artifact_is_dropped(tmp_path):
    conn = _lake(tmp_path)
    c = _why([Evidence(kind="ticket", ref="NOPE-1", role="corroborating",
                       excerpt="anything")])
    stats = ground_citations([c], conn)
    # Strengthened: the brief's version only asserted corroboration, which
    # would also pass if the missing-artifact citation were left in place
    # (not counted as dropped) while corroboration happened to be computed
    # correctly some other way. Pin the counters and the pruned evidence too.
    assert stats["dropped"] == 1 and stats["grounded"] == 0
    assert [e.kind for e in c.evidence] == ["code"]
    assert c.corroboration == "code_inferred"


def test_code_inferred_claim_is_capped_and_never_verified(tmp_path):
    conn = _lake(tmp_path)
    c = _why([])                     # no artifact evidence at all
    c.confidence = 0.9
    stats = ground_citations([c], conn)
    assert stats["code_inferred"] == 1
    assert c.corroboration == "code_inferred"
    assert c.confidence == CODE_INFERRED_MAX_CONFIDENCE
    assert c.status == "recovered"


def test_grounding_keeps_the_code_hypothesis_and_never_deletes_claims(
        tmp_path):
    conn = _lake(tmp_path)
    c = _why([Evidence(kind="ticket", ref="NOPE-1", role="corroborating",
                       excerpt="x")])
    ground_citations([c], conn)
    # The claim survives with its code evidence -> "no valid evidence" holds.
    assert any(e.kind == "code" and e.role == "primary" for e in c.evidence)


def test_partial_grounding_keeps_only_the_real_citation(tmp_path):
    conn = _lake(tmp_path)
    c = _why([
        Evidence(kind="ticket", ref="EMB-1", role="corroborating",
                 excerpt="Statuses arrive irregularly"),
        Evidence(kind="ticket", ref="EMB-1", role="corroborating",
                 excerpt="invented sentence"),
    ])
    stats = ground_citations([c], conn)
    assert stats["grounded"] == 1 and stats["dropped"] == 1
    assert c.corroboration == "corroborated"
    tickets = [e for e in c.evidence if e.kind == "ticket"]
    assert len(tickets) == 1
    # Strengthened: confirm it's specifically the real citation that
    # survived, not merely that some one ticket citation remains (a bug
    # that kept the fabricated one and dropped the real one would still
    # satisfy len(...) == 1).
    assert tickets[0].excerpt == "Statuses arrive irregularly"


def test_empty_excerpt_cannot_ground(tmp_path):
    conn = _lake(tmp_path)
    c = _why([Evidence(kind="ticket", ref="EMB-1", role="corroborating",
                       excerpt="   ")])
    stats = ground_citations([c], conn)
    # Strengthened: also confirm it was actually counted as dropped and
    # pruned from evidence, not just that corroboration ended up right.
    assert stats["dropped"] == 1 and stats["grounded"] == 0
    assert [e.kind for e in c.evidence] == ["code"]
    assert c.corroboration == "code_inferred"


def test_what_layer_claims_are_left_alone(tmp_path):
    conn = _lake(tmp_path)
    what = Claim(id="CLM-0001", type="threshold", statement="s", layer="what",
                 status="machine_verified", confidence=0.9)
    stats = ground_citations([what], conn)
    assert what.corroboration is None
    assert what.confidence == 0.9
    # Strengthened: a what-layer claim must pass through completely
    # unmodified, not merely keep corroboration/confidence. Pin status too,
    # and confirm nothing was counted against the stats totals.
    assert what.status == "machine_verified"
    assert stats == {"grounded": 0, "dropped": 0, "code_inferred": 0}


def test_stats_aggregate_across_mixed_claims_in_one_call(tmp_path):
    conn = _lake(tmp_path)
    grounded = _why([Evidence(kind="ticket", ref="EMB-1",
                              role="corroborating",
                              excerpt="Statuses arrive irregularly")],
                     cid="WHY-0002")
    demoted = _why([Evidence(kind="ticket", ref="NOPE-1",
                             role="corroborating", excerpt="anything")],
                    cid="WHY-0003")
    what = Claim(id="CLM-0002", type="threshold", statement="s",
                 layer="what", status="machine_verified", confidence=0.9)
    stats = ground_citations([grounded, demoted, what], conn)
    # Added: none of the brief's tests exercised more than one claim per
    # call, so a bug that reset stats per-claim instead of accumulating, or
    # that let a what-layer claim's (absent) evidence pollute the counters,
    # would slip through unnoticed.
    assert stats == {"grounded": 1, "dropped": 1, "code_inferred": 1}
    assert grounded.corroboration == "corroborated"
    assert demoted.corroboration == "code_inferred"
    assert what.corroboration is None


def test_code_inferred_cap_never_raises_a_lower_confidence(tmp_path):
    conn = _lake(tmp_path)
    c = _why([])                     # no artifact evidence at all
    c.confidence = 0.15              # already below CODE_INFERRED_MAX_CONFIDENCE
    stats = ground_citations([c], conn)
    assert stats["code_inferred"] == 1
    assert c.corroboration == "code_inferred"
    # The cap must use min(), not unconditional assignment. This test proves
    # the cap only ever lowers confidence, never raises it.
    assert c.confidence == 0.15


def test_excerpt_none_is_dropped(tmp_path):
    conn = _lake(tmp_path)
    c = _why([Evidence(kind="ticket", ref="EMB-1", role="corroborating",
                       excerpt=None)])
    stats = ground_citations([c], conn)
    # The excerpt is None, which normalize_text guards with (s or "").
    # It should be dropped, not raise an error.
    assert stats["dropped"] == 1 and stats["grounded"] == 0
    assert [e.kind for e in c.evidence] == ["code"]   # only hypothesis left
    assert c.corroboration == "code_inferred"


def _what(cid="CLM-0001", ref="src/a.c:5-9", commit_sha="sha1"):
    return Claim(id=cid, type="threshold", statement="Puck lerps to target",
                 layer="what", status="machine_verified", symbols=["lerp"],
                 evidence=[Evidence(kind="code", ref=ref,
                                    role="primary", excerpt="lerp(a,b,t);",
                                    commit_sha=commit_sha, line_start=5,
                                    line_end=9, pin_status="pinned")])


def _ask_returning(payload):
    return lambda _prompt: json.dumps(payload)


def test_synthesis_builds_why_claims_with_artifact_evidence():
    claims = synthesize_why_claims("nav", [_what()], "corpus", _ask_returning([
        {"type": "rationale", "statement": "Lerp was chosen to stop jitter",
         "explains": ["CLM-0001"],
         "evidence": [{"ref": "EMB-1", "excerpt": "puck jitters"}]}]))
    assert len(claims) == 1
    c = claims[0]
    assert c.layer == "why" and c.type == "rationale"
    assert c.id == "WHY-0001"
    assert c.explains == ["CLM-0001"]
    art = [e for e in c.evidence if e.kind == "ticket"]
    assert art[0].role == "corroborating" and art[0].ref == "EMB-1"


def test_code_hypothesis_is_copied_from_the_explained_what_claim():
    claims = synthesize_why_claims("nav", [_what()], "corpus", _ask_returning([
        {"type": "intent", "statement": "s", "explains": ["CLM-0001"],
         "evidence": [{"ref": "EMB-1", "excerpt": "x"}]}]))
    code = [e for e in claims[0].evidence if e.kind == "code"]
    assert len(code) == 1
    # Copied verbatim off the what-claim, including Spec B's anchor.
    assert code[0].ref == "src/a.c:5-9"
    assert code[0].commit_sha == "sha1"
    assert code[0].role == "primary"
    # Strengthened: the brief's version only checked ref/commit_sha/role,
    # which a copy that reconstructed the Evidence from a handful of fields
    # (dropping the rest of the commit pin) would still pass. Pin down the
    # remaining pin fields too, so a shallow/partial copy fails here.
    assert code[0].line_start == 5
    assert code[0].line_end == 9
    assert code[0].pin_status == "pinned"


def test_evidence_ref_kinds_are_classified_from_the_ref_form():
    claims = synthesize_why_claims("nav", [_what()], "corpus", _ask_returning([
        {"type": "rationale", "statement": "s", "explains": ["CLM-0001"],
         "evidence": [{"ref": "EMB-1", "excerpt": "a"},
                      {"ref": "pr:42", "excerpt": "b"},
                      {"ref": "pr_comment:c1", "excerpt": "c"}]}]))
    kinds = {e.ref: e.kind for e in claims[0].evidence if e.kind != "code"}
    assert kinds == {"EMB-1": "ticket", "pr:42": "pr",
                     "pr_comment:c1": "pr_comment"}


def test_unknown_explains_ids_are_dropped():
    claims = synthesize_why_claims("nav", [_what()], "corpus", _ask_returning([
        {"type": "rationale", "statement": "s",
         "explains": ["CLM-0001", "CLM-9999"],
         "evidence": [{"ref": "EMB-1", "excerpt": "x"}]}]))
    assert claims[0].explains == ["CLM-0001"]


def test_unknown_explains_ids_filtered_but_known_ones_survive():
    # Added: the brief only exercised one known + one unknown id. With a
    # single known id, an implementation that (buggily) kept only the FIRST
    # explains entry regardless of validity would coincidentally pass the
    # test above. Use two known claims plus one unknown, interleaved, and
    # confirm both known ids survive in order.
    what_a = _what(cid="CLM-0001")
    what_b = _what(cid="CLM-0002", ref="src/b.c:1-2", commit_sha="sha2")
    claims = synthesize_why_claims(
        "nav", [what_a, what_b], "corpus", _ask_returning([
            {"type": "rationale", "statement": "s",
             "explains": ["CLM-0001", "CLM-9999", "CLM-0002"],
             "evidence": [{"ref": "EMB-1", "excerpt": "x"}]}]))
    assert claims[0].explains == ["CLM-0001", "CLM-0002"]


def test_claim_explaining_nothing_real_is_discarded():
    claims = synthesize_why_claims("nav", [_what()], "corpus", _ask_returning([
        {"type": "rationale", "statement": "s", "explains": ["CLM-9999"],
         "evidence": [{"ref": "EMB-1", "excerpt": "x"}]}]))
    assert claims == []


def test_unknown_why_type_is_rejected():
    claims = synthesize_why_claims("nav", [_what()], "corpus", _ask_returning([
        {"type": "threshold", "statement": "s", "explains": ["CLM-0001"],
         "evidence": []}]))
    assert claims == []


def test_unparsable_reply_yields_no_claims():
    assert synthesize_why_claims("nav", [_what()], "c",
                                 lambda _p: "not json") == []
    assert synthesize_why_claims("nav", [_what()], "c",
                                 lambda _p: '{"a": 1}') == []


def test_model_supplied_code_refs_are_ignored():
    # The model must not be able to introduce a code ref.
    claims = synthesize_why_claims("nav", [_what()], "corpus", _ask_returning([
        {"type": "rationale", "statement": "s", "explains": ["CLM-0001"],
         "evidence": [{"ref": "src/evil.c:1", "excerpt": "invented"}]}]))
    refs = {e.ref for e in claims[0].evidence}
    assert refs == {"src/a.c:5-9"}      # only the copied hypothesis
    # Strengthened: a set comparison alone would still pass if the invented
    # ref had been let through alongside a duplicate of the real one (same
    # set, different length). Pin down the count too.
    assert len(claims[0].evidence) == 1


def test_duplicate_code_refs_across_explained_claims_are_deduped():
    # Added: the brief's implementation tracks `seen_refs` to dedup code
    # evidence copied from multiple explained what-claims, but none of the
    # brief's tests exercised more than one explained claim, so a dedup
    # regression (e.g. dropping seen_refs entirely) would go unnoticed.
    what_a = _what(cid="CLM-0001")
    what_b = _what(cid="CLM-0002")   # same code ref as what_a
    claims = synthesize_why_claims(
        "nav", [what_a, what_b], "corpus", _ask_returning([
            {"type": "rationale", "statement": "s",
             "explains": ["CLM-0001", "CLM-0002"],
             "evidence": [{"ref": "EMB-1", "excerpt": "x"}]}]))
    code = [e for e in claims[0].evidence if e.kind == "code"]
    assert len(code) == 1
    assert code[0].ref == "src/a.c:5-9"


def test_ids_are_sequential_why_ids():
    payload = [
        {"type": "rationale", "statement": "a", "explains": ["CLM-0001"],
         "evidence": [{"ref": "EMB-1", "excerpt": "x"}]},
        {"type": "intent", "statement": "b", "explains": ["CLM-0001"],
         "evidence": [{"ref": "EMB-1", "excerpt": "y"}]}]
    claims = synthesize_why_claims("nav", [_what()], "c",
                                   _ask_returning(payload))
    assert [c.id for c in claims] == ["WHY-0001", "WHY-0002"]


def _corroborated(cid="WHY-0001"):
    c = Claim(id=cid, type="rationale", statement="s", layer="why",
              confidence=0.5, explains=["CLM-0001"],
              corroboration="corroborated", evidence=[
                  Evidence(kind="code", ref="src/a.c:5", role="primary"),
                  Evidence(kind="ticket", ref="EMB-1", role="corroborating",
                           excerpt="puck jitters")])
    return c


def test_supported_claim_becomes_machine_verified():
    c = _corroborated()
    verify_why_claims([c], "corpus",
                      lambda _p: '{"supported": true, "confidence": 0.85}')
    assert c.status == "machine_verified"
    assert c.confidence == 0.85
    assert c.corroboration == "corroborated"


def test_refuted_claim_becomes_contested_with_counter_evidence():
    c = _corroborated()
    verify_why_claims([c], "corpus", lambda _p: json.dumps(
        {"supported": False, "confidence": 0.2,
         "counter": "the ticket never states this"}))
    assert c.status == "contested"
    assert c.counter_evidence == ["the ticket never states this"]
    # Still corroborated: a real artifact was cited, it just does not support.
    assert c.corroboration == "corroborated"


def test_code_inferred_claims_are_never_verified():
    c = _corroborated()
    c.corroboration = "code_inferred"
    c.confidence = 0.4
    calls = []

    def ask(prompt):
        calls.append(prompt)
        return '{"supported": true, "confidence": 0.9}'

    verify_why_claims([c], "corpus", ask)
    assert calls == []                       # no model call at all
    assert c.status == "recovered"
    assert c.confidence == 0.4


def test_verification_failure_contests_that_claim_only():
    # The first claim verifies, the second's call raises. The first must keep
    # its result: an uncaught error here would lose all prior progress,
    # because saving happens only after the whole loop returns.
    good, bad = _corroborated("WHY-0001"), _corroborated("WHY-0002")
    calls = {"n": 0}

    def flaky(_prompt):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("backend down")
        return '{"supported": true, "confidence": 0.8}'

    verify_why_claims([good, bad], "corpus", flaky)
    assert good.status == "machine_verified"
    assert good.confidence == 0.8
    assert bad.status == "contested"
    assert bad.confidence == 0.0
    assert "backend down" in bad.counter_evidence[0]
    # Strengthened: the brief's stub only proves isolation if it actually
    # calls the model once per claim. Pin the call count so a bug that
    # short-circuited after the first raise (and thus never touched `good`
    # at all, making the "isolation" look accidental) would be caught.
    assert calls["n"] == 2


def test_unparsable_verifier_reply_contests():
    c = _corroborated()
    verify_why_claims([c], "corpus", lambda _p: "garbage")
    assert c.status == "contested"
    assert c.confidence == 0.3
    # Strengthened: the brief never checked counter_evidence here. An
    # unparsable reply has no "counter" key to report, so it must stay
    # empty rather than accidentally being populated with junk.
    assert c.counter_evidence == []


def test_supported_truthy_but_not_true_is_not_supported():
    # Regression guard: the design requires `is True`, not truthiness. A
    # string "yes" or an int 1 is JSON-valid but must NOT be treated as
    # supported, since json.loads would produce these from a sloppy model
    # reply that meant to say true but didn't.
    c = _corroborated()
    verify_why_claims([c], "corpus",
                      lambda _p: '{"supported": "yes", "confidence": 0.9}')
    assert c.status == "contested"
    assert c.confidence == 0.9   # confidence is still honored from the reply

    c2 = _corroborated("WHY-0002")
    verify_why_claims([c2], "corpus",
                      lambda _p: '{"supported": 1, "confidence": 0.9}')
    assert c2.status == "contested"


def test_what_layer_claims_are_not_touched_by_why_verification():
    what = Claim(id="CLM-0001", type="threshold", statement="s", layer="what",
                 status="machine_verified", confidence=0.9)
    verify_why_claims([what], "corpus", lambda _p: '{"supported": false}')
    assert what.status == "machine_verified"
    assert what.confidence == 0.9


def test_supported_claim_with_non_numeric_confidence_uses_default():
    # A reply can be valid JSON yet carry a non-numeric confidence. The
    # unguarded float("high") must not raise past the per-claim isolation;
    # it should fall back to the 0.8 default instead.
    c = _corroborated()
    verify_why_claims([c], "corpus",
                      lambda _p: '{"supported": true, "confidence": "high"}')
    assert c.status == "machine_verified"
    assert c.confidence == 0.8


def test_contested_claim_with_null_confidence_uses_default():
    # json null decodes to Python None; float(None) raises TypeError. Must
    # fall back to the 0.3 default rather than raising.
    c = _corroborated()
    verify_why_claims([c], "corpus",
                      lambda _p: '{"supported": false, "confidence": null}')
    assert c.status == "contested"
    assert c.confidence == 0.3


def test_malformed_confidence_does_not_abort_the_whole_loop():
    # This is the assertion that proves the crash no longer escapes
    # verify_why_claims entirely: the first claim's reply carries a
    # malformed confidence (which, unguarded, would raise past the
    # per-claim try/except and discard every remaining claim because
    # claims are only saved after the loop returns). The second claim must
    # still be processed and end up machine_verified.
    first, second = _corroborated("WHY-0001"), _corroborated("WHY-0002")
    calls = {"n": 0}

    def ask(_prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            return '{"supported": true, "confidence": "high"}'
        return '{"supported": true, "confidence": 0.8}'

    verify_why_claims([first, second], "corpus", ask)
    assert calls["n"] == 2
    assert first.status == "machine_verified"
    assert first.confidence == 0.8          # fell back to the default
    assert second.status == "machine_verified"
    assert second.confidence == 0.8
