import yaml

from archeon.review import store
from archeon.db import connect


def _write(claims_dir, claim_id, **over):
    data = {"id": claim_id, "type": "threshold", "statement": "s",
            "feature": "src/foo", "status": "machine_verified",
            "confidence": 0.9, "symbols": ["A::b"], "evidence": [],
            "counter_evidence": []}
    data.update(over)
    (claims_dir / f"{claim_id}.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_queue_contested_first_then_score(tmp_path):
    _write(tmp_path, "HI", status="machine_verified", confidence=0.1,
           symbols=["a", "b", "c"])           # high score, not contested
    _write(tmp_path, "LO", status="machine_verified", confidence=0.95,
           symbols=["a"])                       # low score
    _write(tmp_path, "CON", status="contested", confidence=0.8, symbols=["a"])
    ids = [c["id"] for c in store.queue(tmp_path)]
    assert ids[0] == "CON"          # contested surfaced first
    assert ids[1:] == ["HI", "LO"]  # then by impact*uncertainty desc


def test_queue_excludes_terminal_states(tmp_path):
    _write(tmp_path, "DONE", status="expert_accepted")
    _write(tmp_path, "NO", status="rejected")
    _write(tmp_path, "OPEN", status="machine_verified")
    assert [c["id"] for c in store.queue(tmp_path)] == ["OPEN"]


def test_queue_uses_db_fan_in_for_impact(tmp_path):
    _write(tmp_path, "CLM-0001", confidence=0.5, symbols=["callee"])
    conn = connect(tmp_path / "e.db")  # symbol_edges from Spec A schema
    conn.executescript(
        "INSERT INTO symbols(name,kind,path,line,end_line,signature,source) "
        "VALUES ('callee','function','a.c',1,2,'','ts');"
        "INSERT INTO symbols(name,kind,path,line,end_line,signature,source) "
        "VALUES ('c1','function','a.c',3,4,'','ts');"
        "INSERT INTO symbols(name,kind,path,line,end_line,signature,source) "
        "VALUES ('c2','function','a.c',5,6,'','ts');")
    callee = conn.execute("SELECT id FROM symbols WHERE name='callee'").fetchone()["id"]
    c1 = conn.execute("SELECT id FROM symbols WHERE name='c1'").fetchone()["id"]
    c2 = conn.execute("SELECT id FROM symbols WHERE name='c2'").fetchone()["id"]
    conn.executemany(
        "INSERT INTO symbol_edges(src_id,dst_id,kind,weight) "
        "VALUES (?,?,'references',1.0)", [(c1, callee), (c2, callee)])
    conn.commit()
    q = store.queue(tmp_path, conn=conn)
    assert q[0]["impact"] == 2  # two callers reference `callee`
