"""Assemble the why-layer artifact corpus for a set of what-claims.

Pure git + SQL: no LLM anywhere in this module, so the whole retrieval
half of Pass 2 is testable without a model.
"""

from archeon.claims.pin import parse_ref
from archeon.retrieval.archaeology import (
    artifacts_for_commits, file_level_commits, shaping_commits)
from archeon.retrieval.bundle import estimate_tokens

# Sorts last when an artifact has no usable timestamp.
_NO_TS = ""


def spans_for_claims(claims) -> list:
    """Deduped (path, start, end, rev) for every pinned code evidence.

    Only pinned/dirty anchors carry trustworthy line numbers; anything else
    has no span to walk and is handled by the file-level fallback.
    """
    out, seen = [], set()
    for c in claims:
        for e in c.evidence:
            if e.kind != "code" or e.pin_status not in ("pinned", "dirty"):
                continue
            if not e.commit_sha or e.line_start is None or e.line_end is None:
                continue
            parsed = parse_ref(e.ref)
            if parsed is None:
                continue
            path = parsed[0]
            key = (path, e.line_start, e.line_end, e.commit_sha)
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def collect_artifacts(conn, repo_path, claims, why_cfg):
    """Walk every claim's pinned spans out to tickets and PRs."""
    cap = why_cfg.get("max_commits_per_span", 50)
    shas = []
    for path, start, end, rev in spans_for_claims(claims):
        found = shaping_commits(repo_path, path, start, end, rev=rev,
                                max_commits=cap)
        if not found:
            # Renamed, deleted, or otherwise unresolvable at that rev — fall
            # back to the file's own history rather than losing the claim.
            found = file_level_commits(repo_path, path, max_commits=cap)
        shas.extend(found)
    return artifacts_for_commits(conn, shas)


def _ticket_entries(conn, tickets: dict) -> list:
    out = []
    for key, shas in tickets.items():
        row = conn.execute(
            "SELECT key, summary, description, status, created, resolved "
            "FROM tickets WHERE key=?", (key,)).fetchone()
        if row is None:
            continue        # link points at a ticket never ingested
        body = f"{row['summary'] or ''}\n\n{row['description'] or ''}".strip()
        out.append({
            "ref": key, "kind": "ticket", "support": len(shas),
            "ts": row["resolved"] or row["created"] or _NO_TS,
            "text": f"=== {key} (ticket, {row['status'] or 'unknown'}) ===\n"
                    f"{body}\n",
        })
    return out


def _pr_entries(conn, prs: dict) -> list:
    out = []
    for number, shas in prs.items():
        row = conn.execute(
            "SELECT number, title, body, merged_at FROM prs WHERE number=?",
            (number,)).fetchone()
        if row is None:
            continue
        body = f"{row['title'] or ''}\n\n{row['body'] or ''}".strip()
        out.append({
            "ref": f"pr:{number}", "kind": "pr", "support": len(shas),
            "ts": row["merged_at"] or _NO_TS,
            "text": f"=== pr:{number} ===\n{body}\n",
        })
        # Review comments inherit the PR's support so a heavily-discussed PR
        # cannot crowd out other artifacts on comment count alone.
        for cm in conn.execute(
                "SELECT id, author, body, created FROM pr_comments "
                "WHERE pr_number=? ORDER BY created", (number,)):
            if not (cm["body"] or "").strip():
                continue
            out.append({
                "ref": f"pr_comment:{cm['id']}", "kind": "pr_comment",
                "support": len(shas), "ts": cm["created"] or _NO_TS,
                "text": f"=== pr_comment:{cm['id']} on pr:{number} "
                        f"(by {cm['author'] or 'unknown'}) ===\n"
                        f"{cm['body'].strip()}\n",
            })
    return out


def build_corpus(conn, refs, token_budget: int):
    """Render a token-bounded artifact corpus, best-supported first.

    Returns (text, manifest). The manifest's `ref` strings are exactly what
    the synthesizer must cite and what grounding resolves back.
    """
    entries = _ticket_entries(conn, refs.tickets) + \
        _pr_entries(conn, refs.prs)
    # Two stable passes, so the second key dominates: support DESC, then
    # timestamp DESC. An empty timestamp is the smallest string, so
    # reverse=True naturally sorts unknown-date artifacts last within their
    # support group, and the ref pass keeps full ties deterministic.
    entries.sort(key=lambda e: e["ref"])
    entries.sort(key=lambda e: (e["support"], e["ts"]), reverse=True)
    parts, manifest, total = [], [], 0
    for e in entries:
        t = estimate_tokens(e["text"])
        if manifest and total + t > token_budget:
            break
        parts.append(e["text"])
        manifest.append({"ref": e["ref"], "kind": e["kind"],
                         "support": e["support"]})
        total += t
    return "\n".join(parts), manifest
