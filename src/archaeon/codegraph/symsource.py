from pathlib import Path


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace(
        "_", "\\_")
    return escaped + "%"


def symbol_rows(conn, repo_path, prefix: str | None = None) -> list[dict]:
    """Symbols joined with their on-disk source text.

    The symbols table stores path + line range but not code (the `source`
    column is the scanner name). Read each file once and slice [line,end_line].
    """
    repo_path = Path(repo_path)
    sql = ("SELECT id, name, kind, path, line, end_line, signature "
           "FROM symbols")
    params: tuple = ()
    if prefix:
        sql += " WHERE path LIKE ? ESCAPE '\\'"
        params = (_like_prefix(prefix),)
    file_lines: dict[str, list[str] | None] = {}
    out: list[dict] = []
    for r in conn.execute(sql, params):
        path = r["path"]
        if path not in file_lines:
            f = repo_path / path
            file_lines[path] = (
                f.read_text(encoding="utf-8", errors="replace").splitlines()
                if f.is_file() else None)
        lines = file_lines[path]
        if lines is None:
            text = ""
        else:
            text = "\n".join(lines[r["line"] - 1:r["end_line"]])
        out.append({
            "id": r["id"], "name": r["name"], "kind": r["kind"],
            "path": path, "line": r["line"], "end_line": r["end_line"],
            "signature": r["signature"] or "", "text": text})
    return out
