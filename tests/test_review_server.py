import yaml
from fastapi.testclient import TestClient

from archeon.db import connect
from archeon.review.server import create_app


def _write(claims_dir, claim_id, **over):
    data = {"id": claim_id, "type": "threshold", "statement": "s",
            "feature": "src/foo", "status": "machine_verified",
            "confidence": 0.9, "symbols": ["A::b"],
            "evidence": [{"kind": "code", "ref": "a.c:1-2"}],
            "counter_evidence": []}
    data.update(over)
    (claims_dir / f"{claim_id}.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _client(tmp_path):
    return TestClient(create_app(tmp_path))


def test_components_endpoint(tmp_path):
    _write(tmp_path, "CLM-0001")
    r = _client(tmp_path).get("/api/components")
    assert r.status_code == 200
    assert r.json()[0]["component"] == "src/foo"


def test_clusters_endpoint_reads_db_readonly(tmp_path):
    # A populated DB (built read-write here) is opened read-only by the server;
    # GET /api/clusters must surface the Spec A cluster label through it.
    _write(tmp_path, "CLM-0001", feature="src/foo", symbols=["do_thing"])
    db = tmp_path / "e.db"
    conn = connect(db)  # clusters/cluster_members from Spec A schema
    conn.execute("INSERT INTO symbols(name, kind, path, line, end_line, "
                 "signature, source) VALUES ('do_thing','function','a.c',1,2,"
                 "'','ts')")
    sid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    cid = conn.execute("INSERT INTO clusters(component, label, "
                       "candidate_types) VALUES ('src/foo','thermal','')").lastrowid
    conn.execute("INSERT INTO cluster_members(cluster_id, symbol_id) "
                 "VALUES (?, ?)", (cid, sid))
    conn.commit()
    conn.close()
    client = TestClient(create_app(tmp_path, db=str(db)))
    cl = client.get("/api/clusters", params={"component": "src/foo"}).json()
    assert cl[0]["cluster"] == "thermal" and cl[0]["clustered"] is True


def test_claims_endpoint_attaches_render_spec(tmp_path):
    _write(tmp_path, "CLM-0001", type="threshold")
    cards = _client(tmp_path).get("/api/claims", params={"component": "src/foo"}).json()
    assert cards[0]["render"]["mode"] == "table"


def test_post_accept_writes_yaml_and_returns_new_version(tmp_path):
    _write(tmp_path, "CLM-0001")
    c = _client(tmp_path)
    card = c.get("/api/claims", params={"component": "src/foo"}).json()[0]
    r = c.post(f"/api/claims/CLM-0001",
               json={"status": "expert_accepted", "version": card["version"]})
    assert r.status_code == 200 and r.json()["ok"] is True
    on_disk = yaml.safe_load((tmp_path / "CLM-0001.yaml").read_text())
    assert on_disk["status"] == "expert_accepted"
    assert r.json()["version"] != card["version"]


def test_post_edit_sets_statement(tmp_path):
    _write(tmp_path, "CLM-0001")
    c = _client(tmp_path)
    card = c.get("/api/claims", params={"component": "src/foo"}).json()[0]
    c.post("/api/claims/CLM-0001", json={"status": "expert_accepted",
                                         "statement": "edited",
                                         "version": card["version"]})
    on_disk = yaml.safe_load((tmp_path / "CLM-0001.yaml").read_text())
    assert on_disk["statement"] == "edited"


def test_post_stale_version_is_409(tmp_path):
    _write(tmp_path, "CLM-0001")
    r = _client(tmp_path).post("/api/claims/CLM-0001",
                               json={"status": "rejected", "version": "stale"})
    assert r.status_code == 409


def test_post_unknown_status_is_400(tmp_path):
    _write(tmp_path, "CLM-0001")
    c = _client(tmp_path)
    card = c.get("/api/claims", params={"component": "src/foo"}).json()[0]
    r = c.post("/api/claims/CLM-0001",
               json={"status": "approved", "version": card["version"]})
    assert r.status_code == 400


def test_post_missing_claim_is_404(tmp_path):
    r = _client(tmp_path).post("/api/claims/NOPE",
                               json={"status": "rejected", "version": "x"})
    assert r.status_code == 404


def test_index_html_served_at_root(tmp_path):
    r = _client(tmp_path).get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
