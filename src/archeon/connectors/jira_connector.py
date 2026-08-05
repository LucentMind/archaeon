import sqlite3
from datetime import datetime, timezone

import requests


def _normalize_ts(value: str | None) -> str | None:
    """Normalize a Jira timestamp (e.g. '2025-12-15T00:00:00.000+0000',
    milliseconds + a no-colon UTC offset) into a SQLite-parseable UTC
    form. SQLite's datetime() returns NULL for the no-colon offset shape,
    which silently excludes every real Jira ticket from candidate
    queries. Returns the input unchanged if it is falsy or unparseable.
    """
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")


def _default_fetch(url: str, params: dict, token: str,
                   email: str | None = None) -> dict:
    """Jira Cloud API tokens (the `id.atlassian.com`-issued kind, e.g.
    `ATATT3x...`) authenticate via HTTP Basic auth as (email, token) — a
    bare `Authorization: Bearer <token>` gets a 403 from Jira Cloud, not a
    401, so the failure doesn't look like an auth problem at first glance.
    Pass `email` to use Basic auth; omit it only for setups that really do
    use a bearer/OAuth token (e.g. some Data Center configurations).
    """
    if email:
        resp = requests.get(url, params=params, auth=(email, token),
                            timeout=30)
    else:
        resp = requests.get(url, params=params,
                            headers={"Authorization": f"Bearer {token}"},
                            timeout=30)
    resp.raise_for_status()
    return resp.json()


def _insert_issue(conn: sqlite3.Connection, issue: dict) -> None:
    f = issue["fields"]
    conn.execute(
        "INSERT OR REPLACE INTO tickets(key, summary, description, "
        "status, created, resolved) VALUES (?, ?, ?, ?, ?, ?)",
        (issue["key"], f.get("summary") or "", f.get("description") or "",
         (f.get("status") or {}).get("name"),
         _normalize_ts(f.get("created")),
         _normalize_ts(f.get("resolutiondate"))))


_FIELDS = "summary,description,status,created,resolutiondate"


def _search(conn: sqlite3.Connection, url: str, jql: str, token: str,
            fetch) -> int:
    """Page through `url` (the `/rest/api/2/search/jql` endpoint) via its
    cursor-based `nextPageToken`/`isLast`. Atlassian retired the old
    `/rest/api/2/search` endpoint (410 Gone) along with its `startAt`/`total`
    offset pagination in favor of this cursor scheme. `fields` must be
    passed explicitly too — omitting it now returns bare `{"id": ...}` per
    issue (no `key`, no `fields`) instead of defaulting to a full payload.
    """
    inserted = 0
    next_token = None
    while True:
        params = {"jql": jql, "maxResults": 100, "fields": _FIELDS}
        if next_token:
            params["nextPageToken"] = next_token
        data = fetch(url, params, token)
        for issue in data["issues"]:
            _insert_issue(conn, issue)
            inserted += 1
        if data.get("isLast", True) or not data["issues"]:
            break
        next_token = data.get("nextPageToken")
        if not next_token:
            break
    return inserted


def _bind_default_fetch(email: str | None):
    def fetch(url, params, token):
        return _default_fetch(url, params, token, email=email)
    return fetch


def ingest_jira(conn: sqlite3.Connection, base_url: str, jql: str,
                token: str, email: str | None = None, fetch=None) -> int:
    fetch = fetch or _bind_default_fetch(email)
    inserted = _search(conn, f"{base_url}/rest/api/2/search/jql", jql, token,
                       fetch)
    conn.commit()
    return inserted


def ingest_jira_by_keys(conn: sqlite3.Connection, base_url: str,
                        keys, token: str, email: str | None = None,
                        fetch=None, batch: int = 50) -> int:
    """Fetch only the tickets named by `keys` (discovered from the component's
    commits/PRs/branches), in JQL `key in (...)` batches. This keeps ingestion
    scoped to the component regardless of how many Jira projects contribute.
    """
    fetch = fetch or _bind_default_fetch(email)
    url = f"{base_url}/rest/api/2/search/jql"
    keys = sorted(keys)
    inserted = 0
    for i in range(0, len(keys), batch):
        chunk = keys[i:i + batch]
        jql = "key in (" + ",".join(chunk) + ")"
        inserted += _search(conn, url, jql, token, fetch)
    conn.commit()
    return inserted
