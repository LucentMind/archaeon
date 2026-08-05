from archaeon.claims.schema import (
    CODE_INFERRED_MAX_CONFIDENCE, CLAIM_TYPES, WHY_CLAIM_TYPES, Claim, Evidence, load_claims, save_claims)


def test_why_claim_types_are_disjoint_from_what_types():
    assert WHY_CLAIM_TYPES == {
        "intent", "rationale", "constraint_origin", "tradeoff"}
    assert not (WHY_CLAIM_TYPES & CLAIM_TYPES)


def test_code_inferred_cap_is_below_default_confidence():
    assert CODE_INFERRED_MAX_CONFIDENCE == 0.4


def test_claim_defaults_corroboration_and_explains():
    c = Claim(id="WHY-0001", type="rationale", statement="s")
    assert c.corroboration is None
    assert c.explains == []


def test_legacy_claim_dict_without_new_fields_still_loads():
    # A claim file written before Spec D must round-trip unchanged.
    legacy = {"id": "CLM-0001", "type": "threshold", "statement": "s",
              "layer": "what", "status": "machine_verified"}
    c = Claim.from_dict(legacy)
    assert c.corroboration is None
    assert c.explains == []
    assert c.to_dict()["corroboration"] is None


def test_why_claim_round_trips_new_fields():
    c = Claim(id="WHY-0001", type="rationale", statement="s", layer="why",
              corroboration="corroborated", explains=["CLM-0007"])
    assert Claim.from_dict(c.to_dict()).explains == ["CLM-0007"]
    assert Claim.from_dict(c.to_dict()).corroboration == "corroborated"


def test_claim_roundtrip_yaml(tmp_path):
    claims = [
        Claim(id="CLM-0001", type="threshold",
              statement="Overtemp trips at 125 C.", feature="thermal",
              status="machine_verified", confidence=0.9,
              symbols=["check_temp"],
              evidence=[Evidence(kind="code", role="primary",
                                 ref="fault.c:12", excerpt="if (t > 125)")]),
        Claim(id="CLM-0002", type="state_transition",
              statement="RUNNING goes to SAFE_STOP on fault.",
              feature="thermal", status="contested", confidence=0.3,
              counter_evidence=["debounce delays it"]),
    ]
    save_claims(claims, tmp_path / "claims")
    loaded = {c.id: c for c in load_claims(tmp_path / "claims")}
    assert loaded["CLM-0001"].type == "threshold"
    assert loaded["CLM-0001"].confidence == 0.9
    assert loaded["CLM-0001"].evidence[0].ref == "fault.c:12"
    assert loaded["CLM-0001"].evidence[0].role == "primary"
    assert loaded["CLM-0002"].status == "contested"
    assert loaded["CLM-0002"].counter_evidence == ["debounce delays it"]
    assert loaded["CLM-0002"].layer == "what"


def test_evidence_backward_compat_loads_without_anchor_fields(tmp_path):
    # A pre-Spec-B claim YAML with file:line-only evidence must still load,
    # with all anchor fields null.
    d = tmp_path / "claims"
    d.mkdir()
    (d / "CLM-0001.yaml").write_text(
        "id: CLM-0001\ntype: threshold\nstatement: s\n"
        "evidence:\n- kind: code\n  ref: a.c:1\n  role: primary\n"
        "  excerpt: x\n",
        encoding="utf-8")
    [c] = load_claims(d)
    e = c.evidence[0]
    assert e.ref == "a.c:1"
    assert e.commit_sha is None and e.blob_sha is None
    assert e.line_start is None and e.line_end is None
    assert e.content_hash is None and e.pin_status is None


def test_evidence_anchor_fields_roundtrip(tmp_path):
    claims = [Claim(id="CLM-0001", type="threshold", statement="s",
                    evidence=[Evidence(
                        kind="code", ref="a.c:1-3", excerpt="body",
                        commit_sha="abc123", blob_sha="def456",
                        line_start=1, line_end=3,
                        content_hash="deadbeef", pin_status="pinned")])]
    save_claims(claims, tmp_path / "c")
    [c] = load_claims(tmp_path / "c")
    e = c.evidence[0]
    assert e.commit_sha == "abc123" and e.blob_sha == "def456"
    assert e.line_start == 1 and e.line_end == 3
    assert e.content_hash == "deadbeef" and e.pin_status == "pinned"
