import sqlite3
from pathlib import Path, PurePosixPath

from archeon.codegraph.clang_scan import clang_symbols
from archeon.codegraph.edges import extract_edges
from archeon.codegraph.ts_scan import C_SUFFIXES, CPP_SUFFIXES, ts_symbols

SOURCE_SUFFIXES = C_SUFFIXES | CPP_SUFFIXES | {".xc", ".s", ".asm"}


def _keep(rel: str, include: list[str] | None,
          exclude: list[str] | None) -> bool:
    # Kept iff (include empty OR matches an include) AND matches no exclude;
    # exclude wins. Uses PurePosixPath.full_match (py>=3.13) for `**` globs —
    # this is why pyproject pins requires-python >=3.13.
    p = PurePosixPath(rel)
    if include and not any(p.full_match(g) for g in include):
        return False
    if exclude and any(p.full_match(g) for g in exclude):
        return False
    return True


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def _insert_symbols(conn, rel_path: str, symbols: list[dict],
                    source: str) -> None:
    conn.executemany(
        "INSERT INTO symbols(name, kind, path, line, end_line, signature, "
        "source) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(s["name"], s["kind"], rel_path, s["line"], s["end_line"],
          s["signature"], source) for s in symbols])


def scan_component(conn: sqlite3.Connection, root: Path,
                   path_prefixes: list[str],
                   compile_db_dir: Path | None,
                   include: list[str] | None = None,
                   exclude: list[str] | None = None) -> dict:
    for prefix in path_prefixes:
        conn.execute("DELETE FROM symbols WHERE path LIKE ? ESCAPE '\\'",
                     (_like_prefix(prefix),))
        conn.execute("DELETE FROM scan_gaps WHERE path LIKE ? ESCAPE '\\'",
                     (_like_prefix(prefix),))
    stats = {"clang": 0, "tree_sitter": 0, "gaps": 0}
    for prefix in path_prefixes:
        base = root / prefix
        for f in sorted(p for p in base.rglob("*")
                        if p.suffix in SOURCE_SUFFIXES and p.is_file()):
            rel = f.relative_to(root).as_posix()
            if not _keep(rel, include, exclude):
                continue
            if compile_db_dir is not None:
                try:
                    _insert_symbols(conn, rel,
                                    clang_symbols(f, compile_db_dir), "clang")
                    stats["clang"] += 1
                    continue
                except RuntimeError:
                    pass  # fall through to tree-sitter
            try:
                _insert_symbols(conn, rel, ts_symbols(f), "tree-sitter")
                stats["tree_sitter"] += 1
            except (ValueError, OSError) as e:
                conn.execute(
                    "INSERT OR REPLACE INTO scan_gaps(path, reason) "
                    "VALUES (?, ?)", (rel, str(e)))
                stats["gaps"] += 1
    conn.commit()
    edges = extract_edges(conn, root)
    stats["ref_edges"] = edges["references"]
    stats["include_edges"] = edges["includes"]
    return stats
