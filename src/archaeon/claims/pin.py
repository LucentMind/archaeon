import hashlib
import re
from pathlib import Path

from archaeon.connectors.git_connector import (
    blob_sha, head_sha, is_dirty, show_file)

_REF_RE = re.compile(r"^(?P<path>.+):(?P<start>\d+)(?:-(?P<end>\d+))?$")


def parse_ref(ref: str) -> tuple[str, int, int] | None:
    """Parse 'path:line' or 'path:start-end' into (path, start, end).
    Returns None for anything that is not a well-formed ref (hallucinated
    prose, empty, or an inverted range)."""
    m = _REF_RE.match((ref or "").strip())
    if not m:
        return None
    start = int(m.group("start"))
    end = int(m.group("end")) if m.group("end") else start
    if end < start:
        return None
    return m.group("path"), start, end


def normalize(lines: list[str]) -> list[str]:
    """The single shared normalizer used by BOTH anchor capture and the
    staleness check, so they can never diverge. Strips trailing whitespace
    per line and drops blank-only lines; leading indentation is preserved."""
    return [s.rstrip() for s in lines if s.rstrip()]


def content_hash(norm_lines: list[str]) -> str:
    return hashlib.sha256("\n".join(norm_lines).encode("utf-8")).hexdigest()


def _span(lines: list[str], start: int, end: int) -> list[str] | None:
    """1-indexed inclusive slice; None if out of the file's bounds."""
    if start < 1 or end > len(lines) or start > end:
        return None
    return lines[start - 1:end]


def _resolve_ref_path(ref_path: str, known_paths) -> str | None:
    """Map a ref path that doesn't resolve from the repo root to a unique full
    repo-relative path. The synthesizer often emits a basename-only ref
    (e.g. 'navigator_impl.hpp') or a partial trailing path; git can't resolve
    it, so it would degrade to unpinnable. Given the run's known full path set,
    match by path-component suffix and accept only a unique hit. Returns None
    when there is no known path set, no match, or an ambiguous one (a basename
    shared by several known files stays unpinnable rather than guessing)."""
    if not known_paths:
        return None
    needle = (ref_path or "").strip().lstrip("./")
    if not needle:
        return None
    matches = [kp for kp in known_paths
               if kp == needle or kp.endswith("/" + needle)]
    return matches[0] if len(matches) == 1 else None


def pin_evidence(evidence, repo_path, known_paths=None) -> None:
    """Capture a commit-pinned anchor on one Evidence in place. Degrades
    per-evidence: a bad ref, missing file, or out-of-bounds range sets
    pin_status='unpinnable' and never raises (a run must never abort here).

    `known_paths` (repo-relative paths seen this run) is an optional fallback
    resolver: when a ref's path can't be resolved at HEAD, a bare-basename or
    partial ref that maps to exactly one known path is anchored against it and
    the evidence.ref is rewritten to that full path so is_stale can re-derive
    it later.

    An evidence already carrying a "pinned" or "dirty" anchor is left alone.
    Re-deriving line numbers from HEAD would read whatever now occupies
    those lines if the file changed since the anchor was captured, silently
    re-pointing it at unrelated content while still reporting a clean
    pin_status (see Spec: why-layer B2). This matters most for a why-claim's
    copied code hypothesis, whose entire trustworthiness rests on it being
    captured once, during `synthesize`, and never touched again."""
    if evidence.pin_status in ("pinned", "dirty"):
        return
    repo_path = Path(repo_path)
    parsed = parse_ref(evidence.ref)
    if not parsed:
        evidence.pin_status = "unpinnable"
        return
    path, start, end = parsed
    sha = head_sha(repo_path)
    if not sha:
        evidence.pin_status = "unpinnable"
        return
    lines = show_file(repo_path, path, "HEAD")
    if lines is None:
        # Ref path didn't resolve from the repo root — try resolving a bare or
        # partial ref to a unique known full path before giving up.
        resolved = _resolve_ref_path(path, known_paths)
        lines = show_file(repo_path, resolved, "HEAD") if resolved else None
        if lines is None:
            evidence.pin_status = "unpinnable"
            return
        path = resolved
        evidence.ref = f"{path}:{start}" if end == start else \
            f"{path}:{start}-{end}"
    span = _span(lines, start, end)
    if span is None:
        evidence.pin_status = "unpinnable"
        return
    norm = normalize(span)
    if not norm:
        # A blank/whitespace-only span normalizes to empty: its content_hash
        # would be the empty-string hash and is_stale could never detect an
        # edit, so a 'pinned' status would be misleading. Refuse to pin it.
        evidence.pin_status = "unpinnable"
        return
    evidence.commit_sha = sha
    evidence.blob_sha = blob_sha(repo_path, path, "HEAD")
    evidence.line_start = start
    evidence.line_end = end
    evidence.content_hash = content_hash(norm)
    evidence.pin_status = "dirty" if is_dirty(repo_path) else "pinned"


def pin_claims(claims, repo_path, known_paths=None) -> None:
    """Pin every code evidence of every claim (non-code evidence, e.g.
    tickets/PR comments, has no repo anchor and is skipped). `known_paths` is
    passed through to resolve basename-only refs (see pin_evidence)."""
    for c in claims:
        for e in c.evidence:
            if e.kind == "code":
                pin_evidence(e, repo_path, known_paths=known_paths)


def is_stale(evidence, repo_path) -> bool:
    """True iff a pinned code anchor's normalized block can no longer be
    located in the current working-tree file. Legacy/unpinnable evidence is
    never 'stale' (surfaced separately). Never raises."""
    if evidence.pin_status not in ("pinned", "dirty"):
        return False
    if not evidence.content_hash or not evidence.commit_sha:
        return False
    parsed = parse_ref(evidence.ref)
    if not parsed or evidence.line_start is None or evidence.line_end is None:
        return False
    path = parsed[0]
    repo_path = Path(repo_path)
    # Recover the pinned block (and its normalized length) from the immutable
    # commit the anchor was captured against.
    pinned = show_file(repo_path, path, evidence.commit_sha)
    span = _span(pinned, evidence.line_start, evidence.line_end) if pinned \
        else None
    if pinned is None or span is None:
        return True  # anchored content no longer recoverable -> drifted
    block = normalize(span)
    current_file = repo_path / path
    if not current_file.is_file():
        return True  # cited file gone (moved/deleted) -> stale, not silent
    try:
        text = current_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True  # unreadable working-tree file -> treat as drifted
    current = normalize(text.splitlines())
    length = len(block)
    for i in range(len(current) - length + 1):
        if content_hash(current[i:i + length]) == evidence.content_hash:
            return False  # relocated -> not stale (survives line drift)
    return True


def stale_claims(claims, repo_path) -> list:
    """Claims with any stale primary code evidence."""
    return [c for c in claims
            if any(is_stale(e, repo_path) for e in c.evidence
                   if e.role == "primary" and e.kind == "code")]
