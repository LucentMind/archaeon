import re
import sqlite3
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

BLOCK_TAGS = {
    "p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "br", "tr", "table", "section", "article", "blockquote", "pre",
}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.chunks: list[str] = []
        self._stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        self._stack.append(tag)
        if tag in BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._stack:
            self._stack.reverse()
            self._stack.remove(tag)
            self._stack.reverse()
        if tag in BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data):
        if any(t in ("script", "style") for t in self._stack):
            return
        if "title" in self._stack:
            self.title += data
            return
        self.chunks.append(data)


def _normalize_body_text(chunks: list[str]) -> str:
    raw = "".join(chunks)
    lines = []
    for line in raw.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def ingest_wiki_export(conn: sqlite3.Connection, export_dir: Path) -> int:
    inserted = 0
    for html_file in sorted(export_dir.rglob("*.html")):
        try:
            parser = _TextExtractor()
            parser.feed(
                html_file.read_text(encoding="utf-8", errors="replace"))
            updated = datetime.fromtimestamp(
                html_file.stat().st_mtime, tz=timezone.utc).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO wiki_pages(id, title, body_text, "
                "updated) VALUES (?, ?, ?, ?)",
                (html_file.stem, parser.title.strip(),
                 _normalize_body_text(parser.chunks), updated))
            inserted += 1
        except Exception:
            continue
    conn.commit()
    return inserted
