import re
import sqlite3


def key_pattern(project_keys: list[str]) -> re.Pattern:
    alternation = "|".join(re.escape(k) for k in project_keys)
    return re.compile(rf"\b(?:{alternation})-\d+\b")


def discover_ticket_keys(conn: sqlite3.Connection,
                         project_keys: list[str]) -> set[str]:
    """Collect every ticket key referenced by the component's commits, PR
    titles/bodies, and branch names. This drives key-based Jira fetching so we
    pull only the tickets the component actually touches, not whole projects.
    """
    pattern = key_pattern(project_keys)
    keys: set[str] = set()
    for row in conn.execute("SELECT message FROM commits"):
        keys.update(pattern.findall(row["message"]))
    for row in conn.execute("SELECT title, body, branch FROM prs"):
        keys.update(pattern.findall(
            f"{row['title']}\n{row['body']}\n{row['branch'] or ''}"))
    return keys


def _insert(conn, src_type, src_ref, dst_type, dst_ref, method,
            confidence) -> int:
    cur = conn.execute(
        "INSERT OR IGNORE INTO links(src_type, src_ref, dst_type, dst_ref, "
        "method, confidence) VALUES (?, ?, ?, ?, ?, ?)",
        (src_type, str(src_ref), dst_type, str(dst_ref), method, confidence))
    return cur.rowcount


def extract_heuristic_links(conn: sqlite3.Connection,
                            project_keys: list[str]) -> int:
    pattern = key_pattern(project_keys)
    known = {r["key"] for r in conn.execute("SELECT key FROM tickets")}
    written = 0
    for row in conn.execute("SELECT sha, message FROM commits"):
        for key in set(pattern.findall(row["message"])) & known:
            written += _insert(conn, "commit", row["sha"], "ticket", key,
                               "key_regex", 1.0)
    for row in conn.execute(
            "SELECT number, title, body, branch, merge_sha FROM prs"):
        text = f"{row['title']}\n{row['body']}"
        for key in set(pattern.findall(text)) & known:
            written += _insert(conn, "pr", row["number"], "ticket", key,
                               "key_regex", 1.0)
        # tickets named in the PR branch (e.g. feature/EMB-123-...)
        for key in set(pattern.findall(row["branch"] or "")) & known:
            written += _insert(conn, "pr", row["number"], "ticket", key,
                               "branch_regex", 1.0)
        if row["merge_sha"]:
            written += _insert(conn, "pr", row["number"], "commit",
                               row["merge_sha"], "merge_sha", 1.0)
    written += _inherit_pr_tickets_to_commits(conn)
    conn.commit()
    return written


def _inherit_pr_tickets_to_commits(conn: sqlite3.Connection) -> int:
    """Propagate a PR's ticket to the commits that belong to the PR.

    Most commits carry no ticket key, but their PR does (in its title, body,
    or branch). For each such PR, link the ticket to every member commit that
    is present in the commits table — the PR's own commits (merge/rebase) plus
    its merge/squash commit. This is a why-layer/traceability link, weaker than
    a literal key match, so confidence is 0.9. Rebase merges rewrite SHAs, so
    only their final (merge_sha) commit is reachable this way.
    """
    known_commits = {r["sha"] for r in conn.execute("SELECT sha FROM commits")}
    pr_tickets: dict[str, set] = {}
    for row in conn.execute(
            "SELECT src_ref, dst_ref FROM links "
            "WHERE src_type='pr' AND dst_type='ticket'"):
        pr_tickets.setdefault(row["src_ref"], set()).add(row["dst_ref"])
    written = 0
    for row in conn.execute("SELECT number, merge_sha FROM prs"):
        tickets = pr_tickets.get(str(row["number"]))
        if not tickets:
            continue
        members = {r["sha"] for r in conn.execute(
            "SELECT sha FROM pr_commits WHERE pr_number=?", (row["number"],))}
        if row["merge_sha"]:
            members.add(row["merge_sha"])
        for sha in members & known_commits:
            for key in tickets:
                written += _insert(conn, "commit", sha, "ticket", key,
                                   "pr_inherited", 0.9)
    return written
