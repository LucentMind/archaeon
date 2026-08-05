from archaeon.analysis.link_heuristics import discover_ticket_keys
from archaeon.connectors.jira_connector import (
    _default_fetch, _normalize_ts, ingest_jira, ingest_jira_by_keys)
from archaeon.db import connect

# Atlassian's `/rest/api/2/search/jql` endpoint pages via a cursor
# (`nextPageToken`/`isLast`), not the retired `startAt`/`total` offset scheme.
PAGE1 = {
    "nextPageToken": "tok-2", "isLast": False,
    "issues": [{
        "key": "EMB-1",
        "fields": {"summary": "Thermal shutdown",
                   "description": "50 ms budget",
                   "status": {"name": "Done"},
                   "created": "2025-01-01T00:00:00.000+0000",
                   "resolutiondate": "2025-02-01T00:00:00.000+0000"}}]}
PAGE2 = {
    "isLast": True,
    "issues": [{
        "key": "EMB-2",
        "fields": {"summary": "Debounce", "description": None,
                   "status": {"name": "Open"},
                   "created": "2025-03-01T00:00:00.000+0000",
                   "resolutiondate": None}}]}


def fake_fetch(url, params, token):
    assert url == "https://jira.example/rest/api/2/search/jql"
    assert token == "tkn"
    return PAGE2 if params.get("nextPageToken") == "tok-2" else PAGE1


def test_ingest_jira_pages_and_inserts(tmp_path):
    conn = connect(tmp_path / "e.db")
    n = ingest_jira(conn, "https://jira.example", "project = EMB",
                    "tkn", fetch=fake_fetch)
    assert n == 2
    row = conn.execute("SELECT * FROM tickets WHERE key='EMB-1'").fetchone()
    assert row["summary"] == "Thermal shutdown"
    assert row["status"] == "Done"
    row2 = conn.execute("SELECT * FROM tickets WHERE key='EMB-2'").fetchone()
    assert row2["description"] == ""
    assert row2["resolved"] is None


def test_normalize_ts_strips_ms_and_colonless_offset():
    assert _normalize_ts("2025-01-01T00:00:00.000+0000") == "2025-01-01T00:00:00"
    assert _normalize_ts(None) is None
    assert _normalize_ts("") == ""


def test_jira_timestamps_normalized_for_sqlite_datetime(tmp_path):
    real_shape_page = {
        "startAt": 0, "maxResults": 100, "total": 1,
        "issues": [{
            "key": "EMB-9",
            "fields": {"summary": "Real Jira timestamp shape",
                       "description": "",
                       "status": {"name": "Done"},
                       "created": "2025-12-15T00:00:00.000+0000",
                       "resolutiondate": "2026-01-20T12:30:00.000+0000"}}]}

    def fake_fetch(url, params, token):
        return real_shape_page

    conn = connect(tmp_path / "e.db")
    n = ingest_jira(conn, "https://jira.example", "project = EMB",
                    "tkn", fetch=fake_fetch)
    assert n == 1

    row = conn.execute(
        "SELECT datetime(created) AS c, datetime(resolved) AS r "
        "FROM tickets WHERE key=?", ("EMB-9",)).fetchone()
    assert row["c"] is not None
    assert row["r"] is not None

    stored = conn.execute(
        "SELECT created FROM tickets WHERE key=?", ("EMB-9",)).fetchone()
    assert "+0000" not in stored["created"]


def test_default_fetch_uses_basic_auth_when_email_given(monkeypatch):
    # Jira Cloud API tokens (id.atlassian.com) authenticate via HTTP Basic
    # auth as (email, token); a bare Bearer header gets a 403, not a 401.
    calls = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"issues": [], "total": 0}

    def fake_get(url, params, **kwargs):
        calls["kwargs"] = kwargs
        return FakeResp()

    monkeypatch.setattr(
        "archaeon.connectors.jira_connector.requests.get", fake_get)
    _default_fetch("https://jira.example/rest/api/2/search", {}, "tkn",
                   email="dev@example.com")
    assert calls["kwargs"]["auth"] == ("dev@example.com", "tkn")
    assert "headers" not in calls["kwargs"]


def test_default_fetch_falls_back_to_bearer_without_email(monkeypatch):
    calls = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"issues": [], "total": 0}

    def fake_get(url, params, **kwargs):
        calls["kwargs"] = kwargs
        return FakeResp()

    monkeypatch.setattr(
        "archaeon.connectors.jira_connector.requests.get", fake_get)
    _default_fetch("https://jira.example/rest/api/2/search", {}, "tkn")
    assert calls["kwargs"]["headers"] == {"Authorization": "Bearer tkn"}
    assert "auth" not in calls["kwargs"]


def test_search_stops_on_missing_next_token_even_if_not_marked_last(tmp_path):
    # Defensive: don't loop forever if a response claims more pages exist
    # but doesn't actually provide a token to fetch them with.
    from archaeon.connectors.jira_connector import _search

    calls = []

    def fake_fetch(url, params, token):
        calls.append(dict(params))
        return {"isLast": False, "issues": [
            {"key": "EMB-1", "fields": {"summary": "s", "description": "",
                                        "status": {"name": "Open"},
                                        "created": None,
                                        "resolutiondate": None}}]}

    conn = connect(tmp_path / "e.db")
    n = _search(conn, "https://jira.example/rest/api/2/search/jql",
               "project = EMB", "tkn", fake_fetch)
    assert n == 1
    assert len(calls) == 1  # no infinite loop despite isLast=False


def test_discover_ticket_keys_from_commits_and_prs(tmp_path):
    conn = connect(tmp_path / "e.db")
    conn.execute("INSERT INTO commits(sha, author, date, message) VALUES "
                 "('c1', 'a', 'd', 'EMB-1 fix thermal')")
    conn.execute("INSERT INTO prs(number, title, body, branch, merged_at) "
                 "VALUES (7, 'MOT-9 cleanup', 'also EMB-2', "
                 "'feature/PWR-3-x', 'd')")
    conn.commit()
    keys = discover_ticket_keys(conn, ["EMB", "MOT", "PWR"])
    assert keys == {"EMB-1", "MOT-9", "EMB-2", "PWR-3"}


def test_ingest_jira_by_keys_batches(tmp_path):
    calls = []

    def fake_fetch(url, params, token):
        calls.append(params["jql"])
        key = params["jql"].split("(")[1].rstrip(")")
        return {"startAt": 0, "maxResults": 100, "total": 1,
                "issues": [{"key": key, "fields": {"summary": key,
                            "description": "", "status": {"name": "Done"},
                            "created": None, "resolutiondate": None}}]}

    conn = connect(tmp_path / "e.db")
    n = ingest_jira_by_keys(conn, "https://jira.example", ["EMB-1", "MOT-9"],
                            "tkn", fetch=fake_fetch, batch=1)
    assert n == 2
    assert len(calls) == 2  # one JQL batch per key at batch=1
    assert all(c.startswith("key in (") for c in calls)
    assert {r["key"] for r in conn.execute("SELECT key FROM tickets")} == \
        {"EMB-1", "MOT-9"}
