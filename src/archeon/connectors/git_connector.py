import re
import sqlite3
import subprocess
from pathlib import Path

REC_SEP = "\x01"
FIELD_SEP = "\x02"

DEFAULT_BOT_AUTHORS = [
    "dependabot[bot]", "dependabot-preview[bot]", "renovate[bot]",
    "github-actions[bot]", "mergify[bot]",
]
DEFAULT_BOT_MESSAGE_PATTERNS = [
    r"^Bump .+ from .+ to .+",
    r"^Update .+ requirement from ",
]


def _log(repo_path: Path, path_prefixes: list[str] | None = None) -> str:
    args = ["git", "-C", str(repo_path), "log", "--numstat",
            "--date=iso-strict", "--no-merges", "--reverse",
            f"--pretty=format:{REC_SEP}%H{FIELD_SEP}%an{FIELD_SEP}%ad"
            f"{FIELD_SEP}%B{FIELD_SEP}"]
    if path_prefixes:
        args.append("--")
        args.extend(path_prefixes)
    result = subprocess.run(args, check=True, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.stdout


def ingest_git(conn: sqlite3.Connection, repo_path: Path,
               path_prefixes: list[str] | None = None,
               exclude_authors: list[str] | None = None,
               exclude_message_patterns: list[str] | None = None) -> int:
    bots = {a.lower() for a in (DEFAULT_BOT_AUTHORS if exclude_authors is None
                                else exclude_authors)}
    patterns = [re.compile(p) for p in (
        DEFAULT_BOT_MESSAGE_PATTERNS if exclude_message_patterns is None
        else exclude_message_patterns)]
    inserted = 0
    for record in _log(repo_path, path_prefixes).split(REC_SEP):
        if not record.strip():
            continue
        sha, author, date, message, numstat = record.split(FIELD_SEP)
        message = message.strip()
        if author.lower() in bots or any(p.search(message) for p in patterns):
            continue  # dependency-bump / CI bot commit — no requirement signal
        files = []
        for line in numstat.strip().splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            add, delete, path = parts
            if path_prefixes and not any(
                    path.startswith(p) for p in path_prefixes):
                continue
            files.append((sha, path,
                          int(add) if add.isdigit() else 0,
                          int(delete) if delete.isdigit() else 0))
        if path_prefixes and not files:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO commits(sha, author, date, message) "
            "VALUES (?, ?, ?, ?)", (sha, author, date, message))
        conn.executemany(
            "INSERT OR REPLACE INTO commit_files(sha, path, additions, "
            "deletions) VALUES (?, ?, ?, ?)", files)
        inserted += 1
    conn.commit()
    return inserted


def _run(repo_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command without raising on non-zero exit — callers inspect
    returncode so a missing file/ref degrades to None rather than aborting."""
    return subprocess.run(["git", "-C", str(repo_path), *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def head_sha(repo_path: Path) -> str | None:
    r = _run(repo_path, "rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def blob_sha(repo_path: Path, path: str, rev: str = "HEAD") -> str | None:
    r = _run(repo_path, "rev-parse", f"{rev}:{path}")
    return r.stdout.strip() if r.returncode == 0 else None


def show_file(repo_path: Path, path: str, rev: str = "HEAD") -> list[str] | None:
    r = _run(repo_path, "show", f"{rev}:{path}")
    return r.stdout.splitlines() if r.returncode == 0 else None


def is_dirty(repo_path: Path) -> bool:
    r = _run(repo_path, "status", "--porcelain")
    return bool(r.stdout.strip())
