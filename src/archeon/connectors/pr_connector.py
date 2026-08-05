import json
import shutil
import sqlite3
import subprocess

GH_TIMEOUT_SECONDS = 60


def _gh_fetch(endpoint: str, params: dict, hostname: str | None = None) -> list:
    """Fetch a REST endpoint via the GitHub CLI, using its stored auth.

    `endpoint` is a gh api path like `repos/org/repo/pulls`. `-X GET` forces a
    GET so the `-f` fields become query parameters (gh defaults to POST when
    fields are present). `hostname` targets a GitHub Enterprise host.

    A single call normally takes well under a second; a `gh` process can
    occasionally wedge on a stalled connection and hang indefinitely, which
    is invisible (and unrecoverable short of killing the process) without a
    timeout. `GH_TIMEOUT_SECONDS` turns that into a loud, immediate error
    naming the exact endpoint that stalled.
    """
    if shutil.which("gh") is None:
        raise RuntimeError(
            "gh CLI not found; install it and run `gh auth login`")
    args = ["gh", "api", "-X", "GET"]
    if hostname:
        args += ["--hostname", hostname]
    args.append(endpoint)
    for key, value in params.items():
        args += ["-f", f"{key}={value}"]
    try:
        result = subprocess.run(args, check=True, capture_output=True,
                                text=True, encoding="utf-8",
                                timeout=GH_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"gh api timed out after {GH_TIMEOUT_SECONDS}s on {endpoint} "
            f"(params={params})") from e
    return json.loads(result.stdout or "[]")


def _pages(fetch, endpoint: str):
    page = 1
    while True:
        batch = fetch(endpoint, {"state": "closed", "per_page": 100,
                                 "page": page})
        if not batch:
            return
        yield from batch
        page += 1


def ingest_prs(conn: sqlite3.Connection, repo: str, fetch=None,
               hostname: str | None = None, on_progress=None) -> int:
    """Discover PRs from the component's own commits (`commits/{sha}/pulls`)
    rather than enumerating every PR in the monorepo. Requires the git connector
    to have populated `commits` first; PRs touching no component commit are
    never fetched.

    `on_progress`, if given, is called as `on_progress(sha, i, total, inserted)`
    right before each commit is looked up — so if a call downstream hangs
    (see `GH_TIMEOUT_SECONDS`), the caller can see exactly which commit it
    stalled on rather than only finding out at the very end.
    """
    if fetch is None:
        def fetch(endpoint, params):
            return _gh_fetch(endpoint, params, hostname)
    shas = [r["sha"] for r in conn.execute("SELECT sha FROM commits")]
    total = len(shas)
    seen: set = set()
    inserted = 0
    for i, sha in enumerate(shas, 1):
        if on_progress:
            on_progress(sha, i, total, inserted)
        for pr in _pages(fetch, f"repos/{repo}/commits/{sha}/pulls"):
            number = pr["number"]
            if number in seen:
                continue
            seen.add(number)
            if not pr.get("merged_at"):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO prs(number, title, body, author, "
                "branch, merged_at, merge_sha) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (number, pr.get("title") or "", pr.get("body") or "",
                 (pr.get("user") or {}).get("login"),
                 (pr.get("head") or {}).get("ref"), pr["merged_at"],
                 pr.get("merge_commit_sha")))
            for c in _pages(fetch, f"repos/{repo}/pulls/{number}/comments"):
                conn.execute(
                    "INSERT OR REPLACE INTO pr_comments(id, pr_number, "
                    "author, body, path, created) VALUES (?, ?, ?, ?, ?, ?)",
                    (str(c["id"]), number,
                     (c.get("user") or {}).get("login"),
                     c.get("body") or "", c.get("path"), c.get("created_at")))
            for commit in _pages(fetch,
                                 f"repos/{repo}/pulls/{number}/commits"):
                conn.execute(
                    "INSERT OR REPLACE INTO pr_commits(pr_number, sha) "
                    "VALUES (?, ?)", (number, commit["sha"]))
            inserted += 1
    conn.commit()
    return inserted
