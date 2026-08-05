import json

from archeon.claims.recover import (
    build_feature_bundle, synthesize_claims, verify_claims)


class Ask:
    """Injectable stand-in for AgentClassifier.ask: canned replies in order,
    records the prompts it saw."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


SYNTH_JSON = json.dumps([
    {"type": "threshold",
     "statement": "The controller enters SAFE_STOP when temp exceeds 125 C.",
     "symbols": ["check_temp"],
     "evidence": [{"ref": "fault.c:12", "excerpt": "if (t > 125)"}]},
    {"type": "not_a_real_type", "statement": "junk"},          # dropped
    {"type": "invariant", "statement": ""},                    # dropped (empty)
])


def test_synthesize_parses_and_filters():
    claims = synthesize_claims("thermal", "bundle", Ask([SYNTH_JSON]))
    assert len(claims) == 1  # invalid type and empty statement dropped
    c = claims[0]
    assert c.type == "threshold"
    assert c.layer == "what" and c.status == "recovered"
    assert c.symbols == ["check_temp"]
    assert c.evidence[0].ref == "fault.c:12"
    assert c.id == "CLM-0001"


def test_synthesize_strips_code_fence():
    fenced = "```json\n" + SYNTH_JSON + "\n```"
    claims = synthesize_claims("thermal", "bundle", Ask([fenced]))
    assert len(claims) == 1


def test_synthesize_bad_json_yields_no_claims():
    claims = synthesize_claims("thermal", "bundle", Ask(["not json at all"]))
    assert claims == []


def test_verify_sets_status_and_confidence():
    claims = synthesize_claims("thermal", "bundle", Ask([SYNTH_JSON]))
    verify = Ask([json.dumps(
        {"supported": True, "confidence": 0.92, "counter": ""})])
    verify_claims(claims, "bundle", verify)
    assert claims[0].status == "machine_verified"
    assert claims[0].confidence == 0.92


def test_verify_contests_on_refutation():
    claims = synthesize_claims("thermal", "bundle", Ask([SYNTH_JSON]))
    verify = Ask([json.dumps(
        {"supported": False, "confidence": 0.2,
         "counter": "a 100ms debounce precedes the transition"})])
    verify_claims(claims, "bundle", verify)
    assert claims[0].status == "contested"
    assert claims[0].confidence == 0.2
    assert claims[0].counter_evidence == [
        "a 100ms debounce precedes the transition"]


def test_verify_prompt_includes_named_symbols():
    # A claim covering multiple symbols (the horizontal/vertical pattern that
    # caused real over-generalization misses in the P1 spike) must have all
    # of them visible to the verifier, not just the statement text.
    claims = synthesize_claims("thermal", "bundle", Ask([SYNTH_JSON]))
    verify = Ask([json.dumps({"supported": True, "confidence": 0.9, "counter": ""})])
    verify_claims(claims, "bundle", verify)
    assert "check_temp" in verify.prompts[0]
    assert "Named symbols:" in verify.prompts[0]


def test_verify_prompt_handles_no_symbols():
    claims = synthesize_claims("thermal", "bundle", Ask([SYNTH_JSON]))
    claims[0].symbols = []
    verify = Ask([json.dumps({"supported": True, "confidence": 0.9, "counter": ""})])
    verify_claims(claims, "bundle", verify)
    assert "(none listed)" in verify.prompts[0]


def test_verify_survives_ask_exception_without_losing_other_claims():
    two_claims_json = json.dumps([
        {"type": "threshold", "statement": "first claim",
         "symbols": [], "evidence": [{"ref": "a.c:1", "excerpt": "x"}]},
        {"type": "threshold", "statement": "second claim",
         "symbols": [], "evidence": [{"ref": "a.c:2", "excerpt": "y"}]},
    ])
    claims = synthesize_claims("thermal", "bundle", Ask([two_claims_json]))
    assert len(claims) == 2

    calls = {"n": 0}

    def flaky_ask(prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("Claude Code returned an error result: "
                           "Reached maximum number of turns (1)")
        return json.dumps({"supported": True, "confidence": 0.9, "counter": ""})

    verify_claims(claims, "bundle", flaky_ask)
    assert claims[0].status == "contested"
    assert "verification call failed" in claims[0].counter_evidence[0]
    assert claims[1].status == "machine_verified"  # second claim unaffected
    assert claims[1].confidence == 0.9


def test_supported_claim_with_non_numeric_confidence_uses_default():
    # A reply can be valid JSON yet carry a non-numeric confidence. The
    # unguarded float("high") must not raise past the per-claim isolation;
    # it should fall back to the 0.8 default instead.
    claims = synthesize_claims("thermal", "bundle", Ask([SYNTH_JSON]))
    verify_claims(claims, "bundle",
                  Ask(['{"supported": true, "confidence": "high"}']))
    assert claims[0].status == "machine_verified"
    assert claims[0].confidence == 0.8


def test_contested_claim_with_null_confidence_uses_default():
    # json null decodes to Python None; float(None) raises TypeError. Must
    # fall back to the 0.3 default rather than raising.
    claims = synthesize_claims("thermal", "bundle", Ask([SYNTH_JSON]))
    verify_claims(claims, "bundle",
                  Ask(['{"supported": false, "confidence": null}']))
    assert claims[0].status == "contested"
    assert claims[0].confidence == 0.3


def test_malformed_confidence_does_not_abort_the_whole_loop():
    # This is the assertion that proves the crash no longer escapes
    # verify_claims entirely: the first claim's reply carries a malformed
    # confidence (which, unguarded, would raise past the per-claim
    # try/except and discard every remaining claim because claims are only
    # saved after the loop returns). The second claim must still be
    # processed and end up machine_verified.
    two_claims_json = json.dumps([
        {"type": "threshold", "statement": "first claim",
         "symbols": [], "evidence": [{"ref": "a.c:1", "excerpt": "x"}]},
        {"type": "threshold", "statement": "second claim",
         "symbols": [], "evidence": [{"ref": "a.c:2", "excerpt": "y"}]},
    ])
    claims = synthesize_claims("thermal", "bundle", Ask([two_claims_json]))
    verify = Ask(['{"supported": true, "confidence": "high"}',
                  '{"supported": true, "confidence": 0.9}'])
    verify_claims(claims, "bundle", verify)
    assert len(verify.prompts) == 2
    assert claims[0].status == "machine_verified"
    assert claims[0].confidence == 0.8      # fell back to the default
    assert claims[1].status == "machine_verified"
    assert claims[1].confidence == 0.9


def test_build_feature_bundle_numbers_lines(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.c").write_text("int a;\nint b;\n")
    bundle = build_feature_bundle(tmp_path, ["src/a.c", "missing.c"])
    assert "=== src/a.c ===" in bundle
    assert "1: int a;" in bundle and "2: int b;" in bundle
    assert "missing.c" not in bundle  # non-existent file skipped
