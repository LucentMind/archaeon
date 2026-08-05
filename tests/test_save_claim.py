import pytest
import yaml

from archeon.claims import schema


def _write(claims_dir, claim_id, extra=None):
    data = {
        "id": claim_id,
        "type": "conditional_rule",
        "statement": "original statement",
        "feature": "src/foo",
        "layer": "what",
        "status": "machine_verified",
        "confidence": 0.9,
        "symbols": ["A::b"],
        "evidence": [{"kind": "code", "ref": "foo.c:1-2", "role": "primary",
                      "excerpt": "return;"}],
        "counter_evidence": [],
    }
    if extra:
        data.update(extra)
    p = claims_dir / f"{claim_id}.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                 encoding="utf-8")
    return p


def test_save_claim_sets_status_and_returns_new_version(tmp_path):
    p = _write(tmp_path, "CLM-0001")
    v0 = schema.claim_version(p)
    v1 = schema.save_claim(tmp_path, "CLM-0001", status="expert_accepted",
                           expected_version=v0)
    reloaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert reloaded["status"] == "expert_accepted"
    assert v1 == schema.claim_version(p)
    assert v1 != v0


def test_save_claim_edits_statement(tmp_path):
    p = _write(tmp_path, "CLM-0002")
    schema.save_claim(tmp_path, "CLM-0002", status="expert_accepted",
                      statement="edited", expected_version=schema.claim_version(p))
    reloaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert reloaded["statement"] == "edited"
    assert reloaded["status"] == "expert_accepted"


def test_save_claim_preserves_key_order_and_unknown_keys(tmp_path):
    p = _write(tmp_path, "CLM-0003", extra={"provenance": "spike-run-7"})
    schema.save_claim(tmp_path, "CLM-0003", status="rejected",
                      expected_version=schema.claim_version(p))
    reloaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    # unknown key survives (not on the Claim dataclass)
    assert reloaded["provenance"] == "spike-run-7"
    # key order unchanged: id first, provenance still last
    keys = list(reloaded.keys())
    assert keys[0] == "id"
    assert keys[-1] == "provenance"
    # untouched fields intact
    assert reloaded["symbols"] == ["A::b"]
    assert reloaded["confidence"] == 0.9


def test_save_claim_rejects_stale_version(tmp_path):
    _write(tmp_path, "CLM-0004")
    with pytest.raises(schema.StaleClaimError):
        schema.save_claim(tmp_path, "CLM-0004", status="expert_accepted",
                          expected_version="deadbeef")


def test_save_claim_rejects_unknown_status(tmp_path):
    p = _write(tmp_path, "CLM-0005")
    with pytest.raises(ValueError):
        schema.save_claim(tmp_path, "CLM-0005", status="approved",
                          expected_version=schema.claim_version(p))


def test_save_claim_without_expected_version_skips_check(tmp_path):
    p = _write(tmp_path, "CLM-0006")
    schema.save_claim(tmp_path, "CLM-0006", status="rejected")
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["status"] == "rejected"
