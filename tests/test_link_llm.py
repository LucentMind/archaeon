from archaeon.analysis.link_llm import candidate_tickets, recover_links
from archaeon.db import connect


class FakeAsker:
    """Stands in for AgentClassifier.ask: records prompts, returns canned
    answers in order."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        return self.answers.pop(0)


def _seed(conn):
    conn.execute("INSERT INTO tickets(key, summary, description, created, "
                 "resolved) VALUES ('EMB-1', 'thermal shutdown budget', "
                 "'', '2025-01-01T00:00:00', '2025-06-01T00:00:00')")
    conn.execute("INSERT INTO tickets(key, summary, description, created, "
                 "resolved) VALUES ('EMB-2', 'ui polish', '', "
                 "'2025-01-01T00:00:00', '2025-06-01T00:00:00')")
    conn.execute("INSERT INTO commits(sha, author, date, message) VALUES "
                 "('c1', 'a', '2025-03-01T00:00:00', "
                 "'fix thermal shutdown timing')")
    conn.execute("INSERT INTO commits(sha, author, date, message) VALUES "
                 "('c2', 'a', '2025-03-02T00:00:00', 'polish ui colors')")
    conn.commit()


def test_candidate_tickets_ranked_by_overlap(tmp_path):
    conn = connect(tmp_path / "e.db")
    _seed(conn)
    cands = candidate_tickets(conn, "c1")
    assert cands[0]["key"] == "EMB-1"


def test_recover_links_inserts_llm_links(tmp_path):
    conn = connect(tmp_path / "e.db")
    _seed(conn)
    ask = FakeAsker(answers=["EMB-1", "NONE"])
    n = recover_links(conn, ask)
    assert n == 1
    row = conn.execute("SELECT * FROM links WHERE method='llm'").fetchone()
    assert (row["src_ref"], row["dst_ref"]) == ("c1", "EMB-1")
    assert row["confidence"] == 0.7
    assert "fix thermal shutdown timing" in ask.calls[0]


def test_recover_links_skips_already_linked(tmp_path):
    conn = connect(tmp_path / "e.db")
    _seed(conn)
    conn.execute("INSERT INTO links(src_type, src_ref, dst_type, dst_ref, "
                 "method, confidence) VALUES ('commit', 'c1', 'ticket', "
                 "'EMB-1', 'key_regex', 1.0)")
    conn.commit()
    ask = FakeAsker(answers=["NONE"])
    recover_links(conn, ask)
    assert len(ask.calls) == 1  # only c2 was asked about


def test_candidate_tickets_includes_boundary_day(tmp_path):
    conn = connect(tmp_path / "e.db")
    conn.execute("INSERT INTO commits(sha, author, date, message) VALUES "
                 "('b1', 'a', '2025-03-01T00:00:00', "
                 "'fix thermal shutdown timing')")
    # created exactly window_days (30) after the commit date, at day
    # granularity -- would have been dropped before the datetime() fix
    # due to 'T' vs ' ' separator mismatch in TEXT comparison.
    conn.execute("INSERT INTO tickets(key, summary, description, created, "
                 "resolved) VALUES ('EMB-9', 'thermal shutdown budget', "
                 "'', '2025-03-31T00:00:00', '2099-01-01T00:00:00')")
    conn.commit()
    cands = candidate_tickets(conn, "b1")
    assert "EMB-9" in {c["key"] for c in cands}


def test_recover_links_respects_max_commits(tmp_path):
    conn = connect(tmp_path / "e.db")
    _seed(conn)
    ask = FakeAsker(answers=["EMB-1", "EMB-2"])
    recover_links(conn, ask, max_commits=1)
    assert len(ask.calls) == 1  # only one commit was asked about


def test_recover_links_survives_ask_exception(tmp_path):
    conn = connect(tmp_path / "e.db")
    _seed(conn)  # c1 -> EMB-1 candidate, c2 -> EMB-2 candidate

    def flaky_ask(prompt):
        if "thermal" in prompt:
            raise Exception("Claude Code returned an error result: "
                           "Reached maximum number of turns (1)")
        return "EMB-2"

    n = recover_links(conn, flaky_ask)
    assert n == 1  # c1's failure didn't block c2 from being linked
    row = conn.execute("SELECT * FROM links WHERE method='llm'").fetchone()
    assert (row["src_ref"], row["dst_ref"]) == ("c2", "EMB-2")


def test_recover_links_rejects_out_of_candidate_answer(tmp_path):
    conn = connect(tmp_path / "e.db")
    conn.execute("INSERT INTO tickets(key, summary, description, created, "
                 "resolved) VALUES ('EMB-1', 'thermal shutdown budget', "
                 "'', '2025-01-01T00:00:00', '2025-06-01T00:00:00')")
    conn.execute("INSERT INTO commits(sha, author, date, message) VALUES "
                 "('c1', 'a', '2025-03-01T00:00:00', "
                 "'fix thermal shutdown timing')")
    conn.commit()
    ask = FakeAsker(answers=["EMB-999"])
    n = recover_links(conn, ask)
    assert n == 0
    row = conn.execute("SELECT * FROM links WHERE method='llm'").fetchone()
    assert row is None
