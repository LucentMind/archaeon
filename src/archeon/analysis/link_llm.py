import re
import sqlite3

_WORD_RE = re.compile(r"[a-z]{3,}")

PROMPT = """You link git commits to issue-tracker tickets.

Commit message:
{message}

Candidate tickets:
{candidates}

Reply with exactly one ticket key from the candidates if the commit
implements or fixes that ticket, or NONE if none clearly matches.
Reply with the key or NONE only."""


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def candidate_tickets(conn: sqlite3.Connection, sha: str,
                      window_days: int = 30,
                      limit: int = 5) -> list[sqlite3.Row]:
    commit = conn.execute("SELECT date, message FROM commits WHERE sha=?",
                          (sha,)).fetchone()
    rows = conn.execute(
        "SELECT * FROM tickets WHERE datetime(created) <= datetime(?, ?) AND "
        "(resolved IS NULL OR datetime(resolved) >= datetime(?, ?))",
        (commit["date"], f"+{window_days} days",
         commit["date"], f"-{window_days} days")).fetchall()
    msg_words = _words(commit["message"])
    scored = [(len(msg_words & _words(f"{r['summary']} {r['description']}")),
               r) for r in rows]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [r for score, r in scored[:limit] if score > 0]


def recover_links(conn: sqlite3.Connection, ask, max_commits: int = 200) -> int:
    unlinked = conn.execute(
        "SELECT sha, message FROM commits WHERE sha NOT IN "
        "(SELECT src_ref FROM links WHERE src_type='commit' AND "
        "dst_type='ticket') LIMIT ?", (max_commits,)).fetchall()
    inserted = 0
    for commit in unlinked:
        cands = candidate_tickets(conn, commit["sha"])
        if not cands:
            continue
        listing = "\n".join(f"- {c['key']}: {c['summary']}" for c in cands)
        try:
            answer = ask(PROMPT.format(
                message=commit["message"], candidates=listing)).strip()
        except Exception:
            # A single commit's classification call failing (e.g. the agent
            # backend erroring) shouldn't crash the whole batch — treat it
            # as "no match" and move on to the next commit.
            continue
        if answer in {c["key"] for c in cands}:
            cur = conn.execute(
                "INSERT OR IGNORE INTO links(src_type, src_ref, dst_type, "
                "dst_ref, method, confidence) VALUES ('commit', ?, "
                "'ticket', ?, 'llm', 0.7)", (commit["sha"], answer))
            inserted += cur.rowcount
    conn.commit()
    return inserted
