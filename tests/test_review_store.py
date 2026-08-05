import yaml

from archaeon.db import connect
from archaeon.review import store


def _write(claims_dir, claim_id, **over):
    data = {
        "id": claim_id, "type": "threshold", "statement": "s",
        "feature": "src/foo", "layer": "what", "status": "machine_verified",
        "confidence": 0.9, "symbols": ["A::b"],
        "evidence": [{"kind": "code", "ref": "foo.c:1-2", "role": "primary",
                      "excerpt": "x"}],
        "counter_evidence": [],
    }
    data.update(over)
    (claims_dir / f"{claim_id}.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_verification_bucket_mapping():
    assert store.verification_bucket("machine_verified") == "verified"
    assert store.verification_bucket("expert_accepted") == "verified"
    assert store.verification_bucket("contested") == "contested"
    assert store.verification_bucket("rejected") == "rejected"
    assert store.verification_bucket("recovered") == "unrecovered"
    assert store.verification_bucket("something_else") == "unrecovered"


def test_load_good_claims_carry_version(tmp_path):
    _write(tmp_path, "CLM-0001")
    good, broken = store.load_claim_files(tmp_path)
    assert len(good) == 1 and not broken
    assert good[0]["_version"]  # non-empty hash


def test_malformed_file_becomes_broken_card_not_a_crash(tmp_path):
    _write(tmp_path, "CLM-0001")
    (tmp_path / "CLM-0002.yaml").write_text(": not valid: yaml: [", encoding="utf-8")
    (tmp_path / "CLM-0003.yaml").write_text("just a string", encoding="utf-8")
    good, broken = store.load_claim_files(tmp_path)
    assert {g["id"] for g in good} == {"CLM-0001"}
    assert {b["id"] for b in broken} == {"CLM-0002", "CLM-0003"}
    assert all(b["broken"] and b["error"] for b in broken)


def test_claim_card_shape(tmp_path):
    _write(tmp_path, "CLM-0001", status="contested",
           counter_evidence=["verifier: symbol X not cited"])
    good, _ = store.load_claim_files(tmp_path)
    card = store.claim_card(good[0])
    assert card["id"] == "CLM-0001"
    assert card["bucket"] == "contested"
    assert card["evidence"][0]["ref"] == "foo.c:1-2"
    assert card["counter_evidence"] == ["verifier: symbol X not cited"]
    assert card["version"]


def test_claim_card_passes_broken_through(tmp_path):
    card = store.claim_card({"id": "CLM-9", "broken": True, "error": "boom",
                             "_version": ""})
    assert card["broken"] and card["error"] == "boom" and card["id"] == "CLM-9"


def test_claim_card_handles_none_values(tmp_path):
    _write(tmp_path, "CLM-0001", evidence=None, symbols=None, counter_evidence=None)
    good, _ = store.load_claim_files(tmp_path)
    card = store.claim_card(good[0])
    assert card["evidence"] == []
    assert card["symbols"] == []
    assert card["counter_evidence"] == []


def test_components_count_by_bucket(tmp_path):
    _write(tmp_path, "CLM-0001", feature="src/foo", status="machine_verified")
    _write(tmp_path, "CLM-0002", feature="src/foo", status="contested")
    _write(tmp_path, "CLM-0003", feature="src/bar", status="recovered")
    comps = {c["component"]: c for c in store.components(tmp_path)}
    assert comps["src/foo"]["verified"] == 1
    assert comps["src/foo"]["contested"] == 1
    assert comps["src/foo"]["total"] == 2
    assert comps["src/bar"]["unrecovered"] == 1


def test_broken_claims_group_under_unparsed(tmp_path):
    (tmp_path / "CLM-0002.yaml").write_text("nope: [", encoding="utf-8")
    comps = {c["component"]: c for c in store.components(tmp_path)}
    assert comps["(unparsed)"]["broken"] == 1


def test_clusters_flat_fallback_without_db(tmp_path):
    _write(tmp_path, "CLM-0001", feature="src/foo",
           evidence=[{"kind": "code", "ref": "a.c:1-2"}])
    _write(tmp_path, "CLM-0002", feature="src/foo",
           evidence=[{"kind": "code", "ref": "b.c:3-4"}])
    cl = store.clusters(tmp_path, "src/foo", conn=None)
    labels = {c["cluster"] for c in cl}
    assert labels == {"a.c", "b.c"}
    assert all(c["clustered"] is False for c in cl)


def test_clusters_use_db_metadata_when_present(tmp_path):
    _write(tmp_path, "CLM-0001", feature="src/foo", symbols=["do_thing"])
    conn = connect(tmp_path / "e.db")  # clusters/cluster_members from Spec A schema
    conn.execute(
        "INSERT INTO symbols(name, kind, path, line, end_line, signature, "
        "source) VALUES ('do_thing','function','src/a.c',1,2,'','tree-sitter')")
    sid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    cid = conn.execute(
        "INSERT INTO clusters(component, label, candidate_types) "
        "VALUES ('src/foo','thermal path','threshold')").lastrowid
    conn.execute("INSERT INTO cluster_members(cluster_id, symbol_id) "
                 "VALUES (?, ?)", (cid, sid))
    conn.commit()
    cl = store.clusters(tmp_path, "src/foo", conn=conn)
    assert cl[0]["cluster"] == "thermal path" and cl[0]["clustered"] is True


def test_clusters_degrade_to_flat_when_db_lacks_cluster_tables(tmp_path):
    _write(tmp_path, "CLM-0001", feature="src/foo",
           evidence=[{"kind": "code", "ref": "a.c:1-2"}])
    conn = connect(tmp_path / "e.db")  # real schema, no clusters/cluster_members
    cl = store.clusters(tmp_path, "src/foo", conn=conn)
    assert cl[0]["cluster"] == "a.c" and cl[0]["clustered"] is False


def test_claims_in_filters_by_component_and_cluster(tmp_path):
    _write(tmp_path, "CLM-0001", feature="src/foo",
           evidence=[{"kind": "code", "ref": "a.c:1-2"}])
    _write(tmp_path, "CLM-0002", feature="src/foo",
           evidence=[{"kind": "code", "ref": "b.c:3-4"}])
    cards = store.claims_in(tmp_path, component="src/foo", cluster="a.c")
    assert [c["id"] for c in cards] == ["CLM-0001"]
