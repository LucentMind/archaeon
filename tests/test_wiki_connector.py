from archaeon.connectors.wiki_connector import ingest_wiki_export
from archaeon.db import connect

HTML = """<html><head><title>Thermal design</title>
<style>p { color: red }</style></head>
<body><h1>Thermal design</h1><p>Shutdown within 50 ms.</p>
<script>ignored()</script></body></html>"""


def test_ingest_wiki_export(tmp_path):
    export = tmp_path / "export"
    export.mkdir()
    (export / "12345.html").write_text(HTML, encoding="utf-8")
    (export / "notes.txt").write_text("skip me")
    conn = connect(tmp_path / "e.db")
    n = ingest_wiki_export(conn, export)
    assert n == 1
    row = conn.execute("SELECT * FROM wiki_pages").fetchone()
    assert row["id"] == "12345"
    assert row["title"] == "Thermal design"
    assert "Shutdown within 50 ms." in row["body_text"]
    assert "ignored" not in row["body_text"]
    assert "color: red" not in row["body_text"]


def test_inline_tags_stay_on_one_line(tmp_path):
    html = (
        "<html><head><title>Links</title></head>"
        '<body><p>Click <a href="x">here</a> to continue.</p></body>'
        "</html>"
    )
    export = tmp_path / "export"
    export.mkdir()
    (export / "1.html").write_text(html, encoding="utf-8")
    conn = connect(tmp_path / "e.db")
    n = ingest_wiki_export(conn, export)
    assert n == 1
    row = conn.execute("SELECT * FROM wiki_pages").fetchone()
    assert "Click here to continue." in row["body_text"]


def test_unreadable_file_is_skipped(tmp_path):
    export = tmp_path / "export"
    export.mkdir()
    (export / "12345.html").write_text(HTML, encoding="utf-8")
    (export / "broken.html").mkdir()
    conn = connect(tmp_path / "e.db")
    n = ingest_wiki_export(conn, export)
    assert n == 1
    row = conn.execute("SELECT * FROM wiki_pages").fetchone()
    assert row["id"] == "12345"
    assert "Shutdown within 50 ms." in row["body_text"]
