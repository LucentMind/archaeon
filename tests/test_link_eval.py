from archaeon.analysis.link_eval import evaluate, load_gold
from archaeon.db import connect

GOLD_CSV = """sha,ticket_key
c1,EMB-1
c2,
c3,EMB-3
"""


def _predict(conn, sha, key, method="key_regex"):
    conn.execute(
        "INSERT INTO links(src_type, src_ref, dst_type, dst_ref, method, "
        "confidence) VALUES ('commit', ?, 'ticket', ?, ?, 0.9)",
        (sha, key, method))


def test_load_gold(tmp_path):
    p = tmp_path / "gold.csv"
    p.write_text(GOLD_CSV)
    gold, sampled = load_gold(p)
    assert gold == {("c1", "EMB-1"), ("c3", "EMB-3")}
    assert sampled == {"c1", "c2", "c3"}


def test_evaluate_precision_recall(tmp_path):
    p = tmp_path / "gold.csv"
    p.write_text(GOLD_CSV)
    gold, sampled = load_gold(p)
    conn = connect(tmp_path / "e.db")
    _predict(conn, "c1", "EMB-1")          # true positive
    _predict(conn, "c2", "EMB-9")          # false positive
    _predict(conn, "c99", "EMB-1")         # outside sample: ignored
    conn.commit()
    m = evaluate(conn, gold, sampled)
    assert m["true_positives"] == 1
    assert m["predicted"] == 2
    assert m["precision"] == 0.5
    assert m["recall"] == 0.5              # 1 of 2 gold pairs found


def test_evaluate_empty_predictions(tmp_path):
    conn = connect(tmp_path / "e.db")
    m = evaluate(conn, {("c1", "EMB-1")}, {"c1"})
    assert m["precision"] == 0.0 and m["recall"] == 0.0


def test_evaluate_method_filter_restricts_predictions(tmp_path):
    p = tmp_path / "gold.csv"
    p.write_text(GOLD_CSV)
    gold, sampled = load_gold(p)
    conn = connect(tmp_path / "e.db")
    # Same sampled commit c1, two competing links from different methods:
    # key_regex predicts the correct gold ticket, llm predicts a wrong one.
    _predict(conn, "c1", "EMB-1", method="key_regex")   # correct, in gold
    _predict(conn, "c1", "EMB-9", method="llm")         # wrong, not in gold
    conn.commit()

    m_regex = evaluate(conn, gold, sampled, methods=["key_regex"])
    assert m_regex["predicted"] == 1
    assert m_regex["true_positives"] == 1
    assert m_regex["precision"] == 1.0

    m_llm = evaluate(conn, gold, sampled, methods=["llm"])
    assert m_llm["predicted"] == 1
    assert m_llm["true_positives"] == 0
    assert m_llm["precision"] == 0.0


def test_evaluate_no_method_filter_counts_all(tmp_path):
    p = tmp_path / "gold.csv"
    p.write_text(GOLD_CSV)
    gold, sampled = load_gold(p)
    conn = connect(tmp_path / "e.db")
    _predict(conn, "c1", "EMB-1", method="key_regex")
    _predict(conn, "c1", "EMB-9", method="llm")
    conn.commit()

    m = evaluate(conn, gold, sampled)
    assert m["predicted"] == 2
