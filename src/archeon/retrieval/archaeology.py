"""Span-scoped git archaeology: which commits shaped a cited line range.

This is the Pass 2 entry point. It runs against the commit a piece of
evidence was *pinned* to (Spec B's ``evidence.commit_sha``), not HEAD —
line numbers only mean something against the commit they were captured
at, so anchoring is what makes this exact rather than approximate.
"""

from dataclasses import dataclass, field
from pathlib import Path

# Reuses the git connector's runner: it deliberately does not raise on a
# non-zero exit, so a missing path or bad rev degrades to [] here instead
# of aborting a whole why-run.
from archeon.connectors.git_connector import _run as git_run


def _shas(result, max_commits: int) -> list[str]:
    """Newest-first shas from a --format=%H log, deduped, capped."""
    if result.returncode != 0:
        return []
    out, seen = [], set()
    for line in result.stdout.splitlines():
        sha = line.strip()
        if not sha or sha in seen:
            continue
        seen.add(sha)
        out.append(sha)
        if len(out) >= max_commits:
            break
    return out


def shaping_commits(repo_path, path: str, start: int, end: int,
                    rev: str = "HEAD", max_commits: int = 50) -> list[str]:
    """Commits that changed lines ``start..end`` of ``path`` as of ``rev``.

    Newest first. Returns [] when the path, rev, or range does not resolve —
    a why-run must never abort on one bad anchor.
    """
    if start < 1 or end < start:
        return []
    return _shas(git_run(Path(repo_path), "log", f"-L{start},{end}:{path}",
                         rev, "--format=%H", "-s",
                         f"--max-count={max_commits}"), max_commits)


def file_level_commits(repo_path, path: str,
                       max_commits: int = 50) -> list[str]:
    """Whole-file history fallback for evidence with no usable span.

    Separate from ``shaping_commits`` because git rejects ``-L`` together
    with ``--follow`` ("--follow requires exactly one pathspec").
    """
    return _shas(git_run(Path(repo_path), "log", "--follow", "--format=%H",
                         f"--max-count={max_commits}", "--", path),
                 max_commits)


@dataclass
class ArtifactRefs:
    """Artifacts reached from a set of shaping commits.

    The dict values are the shas that reached each artifact; ``len`` of one
    is that artifact's *support*, which the corpus builder ranks by.
    """
    tickets: dict = field(default_factory=dict)   # key -> {sha}
    prs: dict = field(default_factory=dict)       # number -> {sha}
    unknown: set = field(default_factory=set)     # shas not in `commits`

    def _add(self, bucket: dict, key, sha: str) -> None:
        bucket.setdefault(key, set()).add(sha)


def artifacts_for_commits(conn, shas) -> ArtifactRefs:
    """Resolve shaping commits to ticket keys and PR numbers via `links`.

    Shas absent from `commits` are reported in `unknown` rather than dropped:
    span archaeology legitimately reaches commits outside the ingest scope
    (bot-filtered, or under a path prefix this component does not cover), and
    silently discarding them would overstate coverage.
    """
    refs = ArtifactRefs()
    for sha in dict.fromkeys(shas):        # dedupe, preserve order
        if conn.execute("SELECT 1 FROM commits WHERE sha=?",
                        (sha,)).fetchone() is None:
            refs.unknown.add(sha)
            continue
        for r in conn.execute(
                "SELECT dst_ref FROM links WHERE src_type='commit' "
                "AND src_ref=? AND dst_type='ticket'", (sha,)):
            refs._add(refs.tickets, r["dst_ref"], sha)
        # commit -> PR. merge_sha is the reliable path on squash-merging
        # repos; pr_commits covers repos that keep branch commits.
        pr_numbers = {r["src_ref"] for r in conn.execute(
            "SELECT src_ref FROM links WHERE src_type='pr' "
            "AND dst_type='commit' AND dst_ref=?", (sha,))}
        pr_numbers |= {str(r["pr_number"]) for r in conn.execute(
            "SELECT pr_number FROM pr_commits WHERE sha=?", (sha,))}
        for raw in pr_numbers:
            try:
                number = int(raw)
            except (TypeError, ValueError):
                continue
            refs._add(refs.prs, number, sha)
            # A ticket named by the PR corroborates the commit that reached it.
            for r in conn.execute(
                    "SELECT dst_ref FROM links WHERE src_type='pr' "
                    "AND src_ref=? AND dst_type='ticket'", (raw,)):
                refs._add(refs.tickets, r["dst_ref"], sha)
    return refs
