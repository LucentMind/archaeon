# Archeon P0 — Evidence Lake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic evidence lake for one golden component — git/Jira/PR/wiki connectors, C/C++ code graph (clang with tree-sitter fallback), change-coupling stats, and issue↔commit link recovery with a measurable precision/recall harness.

**Architecture:** A Python package `archeon` with a SQLite evidence database. Connectors normalize external sources into typed tables; analysis modules (coupling, link heuristics, LLM link recovery) read and write the same DB; a `click` CLI wires the stages. No LLM anywhere except the final link-recovery stage (Haiku, mockable client).

**Tech Stack:** Python ≥3.12, uv, pytest, sqlite3 (stdlib), click, requests, tree-sitter (+ tree-sitter-c, tree-sitter-cpp), libclang (pip wheel, bundles the DLL), anthropic.

## Global Constraints

- Per spec §2: cheap/expensive model split is configuration — model names are never hardcoded outside config defaults; P0's only LLM call uses the cheap tier (default `claude-haiku-4-5-20251001`).
- Per spec §4.1: the evidence lake is deterministic except `link_llm`; every other module must be pure code, no network calls except through injected fetchers.
- Per spec §13: unparsed source files are recorded in `scan_gaps`, never silently skipped.
- Per spec §6: all processing is scoped to one component (config-driven path filters).
- LLM link confidence is stored per link (`method`, `confidence` columns); heuristic key-regex links get confidence 1.0, LLM links 0.7.
- Secrets (Jira/GitHub tokens, Anthropic key) come from environment variables only, never config files.
- All new code lives under `src/archeon/`, tests under `tests/`, mirroring module names.
- Windows is the dev platform: no shell-outs except `git`; paths via `pathlib`.

---

### Task 1: Project scaffold and evidence DB core

**Files:**
- Create: `pyproject.toml`
- Create: `src/archeon/__init__.py`
- Create: `src/archeon/db.py`
- Create: `src/archeon/schema.sql`
- Create: `tests/test_db.py`
- Create: `.gitignore`

**Interfaces:**
- Produces: `archeon.db.connect(path: str | Path) -> sqlite3.Connection` — opens/creates the DB, applies `schema.sql` (idempotent), `row_factory = sqlite3.Row`, foreign keys ON. All later tasks call this.
- Produces: tables `commits, commit_files, tickets, prs, pr_comments, wiki_pages, symbols, scan_gaps, coupling, links`.

- [ ] **Step 1: Write pyproject and gitignore**

`pyproject.toml`:

```toml
[project]
name = "archeon"
version = "0.1.0"
description = "Evidence lake: recover requirements evidence from code and artifacts"
requires-python = ">=3.12"
dependencies = [
    "click>=8.1",
    "requests>=2.32",
    "tree-sitter>=0.23",
    "tree-sitter-c>=0.23",
    "tree-sitter-cpp>=0.23",
    "libclang>=18",
    "anthropic>=0.40",
]

[dependency-groups]
dev = ["pytest>=8"]

[project.scripts]
archeon = "archeon.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/archeon"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`.gitignore`:

```
.venv/
__pycache__/
*.egg-info/
*.db
.pytest_cache/
```

Run: `uv sync` — expected: resolves and installs without error.

- [ ] **Step 2: Write the failing test**

`tests/test_db.py`:

```python
from archeon.db import connect


def test_connect_creates_schema(tmp_path):
    conn = connect(tmp_path / "evidence.db")
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r["name"] for r in rows}
    assert {"commits", "commit_files", "tickets", "prs", "pr_comments",
            "wiki_pages", "symbols", "scan_gaps", "coupling",
            "links"} <= names


def test_connect_is_idempotent_and_persists(tmp_path):
    db = tmp_path / "evidence.db"
    conn = connect(db)
    conn.execute("INSERT INTO commits(sha, author, date, message) "
                 "VALUES ('abc', 'a', '2026-01-01', 'msg')")
    conn.commit()
    conn.close()
    conn2 = connect(db)
    row = conn2.execute("SELECT message FROM commits WHERE sha='abc'").fetchone()
    assert row["message"] == "msg"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'archeon'` (or ImportError on `connect`).

- [ ] **Step 4: Write the implementation**

`src/archeon/__init__.py`: empty file.

`src/archeon/schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS commits(
  sha TEXT PRIMARY KEY,
  author TEXT,
  date TEXT,
  message TEXT
);
CREATE TABLE IF NOT EXISTS commit_files(
  sha TEXT,
  path TEXT,
  additions INTEGER,
  deletions INTEGER,
  PRIMARY KEY (sha, path)
);
CREATE TABLE IF NOT EXISTS tickets(
  key TEXT PRIMARY KEY,
  summary TEXT,
  description TEXT,
  status TEXT,
  created TEXT,
  resolved TEXT
);
CREATE TABLE IF NOT EXISTS prs(
  number INTEGER PRIMARY KEY,
  title TEXT,
  body TEXT,
  author TEXT,
  merged_at TEXT,
  merge_sha TEXT
);
CREATE TABLE IF NOT EXISTS pr_comments(
  id TEXT PRIMARY KEY,
  pr_number INTEGER,
  author TEXT,
  body TEXT,
  path TEXT,
  created TEXT
);
CREATE TABLE IF NOT EXISTS wiki_pages(
  id TEXT PRIMARY KEY,
  title TEXT,
  body_text TEXT,
  updated TEXT
);
CREATE TABLE IF NOT EXISTS symbols(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT,
  kind TEXT,
  path TEXT,
  line INTEGER,
  end_line INTEGER,
  signature TEXT,
  source TEXT
);
CREATE TABLE IF NOT EXISTS scan_gaps(
  path TEXT PRIMARY KEY,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS coupling(
  path_a TEXT,
  path_b TEXT,
  co_changes INTEGER,
  support_a INTEGER,
  support_b INTEGER,
  PRIMARY KEY (path_a, path_b)
);
CREATE TABLE IF NOT EXISTS links(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  src_type TEXT,
  src_ref TEXT,
  dst_type TEXT,
  dst_ref TEXT,
  method TEXT,
  confidence REAL,
  UNIQUE (src_type, src_ref, dst_type, dst_ref, method)
);
```

`src/archeon/db.py`:

```python
import sqlite3
from importlib import resources
from pathlib import Path


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    schema = resources.files("archeon").joinpath("schema.sql").read_text()
    conn.executescript(schema)
    conn.commit()
    return conn
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_db.py -v`
Expected: 2 PASSED. (If `schema.sql` isn't found, add `[tool.hatch.build.targets.wheel] include` is not needed — `uv` installs the project editable by default and `resources` reads from source; verify before working around.)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .gitignore src/archeon tests
git commit -m "feat: project scaffold and evidence DB schema"
```

---

### Task 2: Git connector

**Files:**
- Create: `src/archeon/connectors/__init__.py`
- Create: `src/archeon/connectors/git_connector.py`
- Create: `tests/test_git_connector.py`

**Interfaces:**
- Consumes: `archeon.db.connect`.
- Produces: `ingest_git(conn, repo_path: Path, path_prefixes: list[str] | None = None) -> int` — parses `git log` of the repo, inserts into `commits` and `commit_files` (paths filtered by any of `path_prefixes` if given; a commit is kept if it touches at least one matching file, and only matching files are stored). Returns number of commits inserted. Idempotent via `INSERT OR REPLACE`.

- [ ] **Step 1: Write the failing test**

`tests/test_git_connector.py`:

```python
import subprocess
from pathlib import Path

from archeon.connectors.git_connector import ingest_git
from archeon.db import connect


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "src").mkdir()
    (repo / "src" / "a.c").write_text("int a;\n")
    (repo / "other.txt").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "EMB-1: initial\n\nbody line")
    (repo / "src" / "a.c").write_text("int a;\nint b;\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tweak a")
    return repo


def test_ingest_git_inserts_commits_and_files(tmp_path):
    repo = _make_repo(tmp_path)
    conn = connect(tmp_path / "e.db")
    n = ingest_git(conn, repo)
    assert n == 2
    msgs = [r["message"] for r in
            conn.execute("SELECT message FROM commits ORDER BY date")]
    assert any("EMB-1: initial" in m for m in msgs)
    files = conn.execute("SELECT DISTINCT path FROM commit_files").fetchall()
    assert {"src/a.c", "other.txt"} == {r["path"] for r in files}


def test_path_prefix_filter(tmp_path):
    repo = _make_repo(tmp_path)
    conn = connect(tmp_path / "e.db")
    n = ingest_git(conn, repo, path_prefixes=["src/"])
    assert n == 2
    files = {r["path"] for r in
             conn.execute("SELECT DISTINCT path FROM commit_files")}
    assert files == {"src/a.c"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_git_connector.py -v`
Expected: FAIL with `ModuleNotFoundError` on `archeon.connectors.git_connector`.

- [ ] **Step 3: Write the implementation**

`src/archeon/connectors/__init__.py`: empty file.

`src/archeon/connectors/git_connector.py`:

```python
import sqlite3
import subprocess
from pathlib import Path

REC_SEP = "\x01"
FIELD_SEP = "\x02"


def _log(repo_path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), "log", "--numstat",
         "--date=iso-strict", "--no-merges", "--reverse",
         f"--pretty=format:{REC_SEP}%H{FIELD_SEP}%an{FIELD_SEP}%ad{FIELD_SEP}%B{FIELD_SEP}"],
        check=True, capture_output=True, text=True, encoding="utf-8",
        errors="replace")
    return result.stdout


def ingest_git(conn: sqlite3.Connection, repo_path: Path,
               path_prefixes: list[str] | None = None) -> int:
    inserted = 0
    for record in _log(repo_path).split(REC_SEP):
        if not record.strip():
            continue
        sha, author, date, message, numstat = record.split(FIELD_SEP)
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
            "VALUES (?, ?, ?, ?)", (sha, author, date, message.strip()))
        conn.executemany(
            "INSERT OR REPLACE INTO commit_files(sha, path, additions, "
            "deletions) VALUES (?, ?, ?, ?)", files)
        inserted += 1
    conn.commit()
    return inserted
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_git_connector.py -v`
Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/archeon/connectors tests/test_git_connector.py
git commit -m "feat: git connector ingesting commits and touched files"
```

---

### Task 3: Jira connector

**Files:**
- Create: `src/archeon/connectors/jira_connector.py`
- Create: `tests/test_jira_connector.py`

**Interfaces:**
- Consumes: `archeon.db.connect`.
- Produces: `ingest_jira(conn, base_url: str, jql: str, token: str, fetch=None) -> int` — pages through Jira REST `search`, inserts into `tickets`, returns count. `fetch(url, params, token) -> dict` is injectable; default implementation uses `requests` with bearer auth. Tests never hit the network.

- [ ] **Step 1: Write the failing test**

`tests/test_jira_connector.py`:

```python
from archeon.connectors.jira_connector import ingest_jira
from archeon.db import connect

PAGE1 = {
    "startAt": 0, "maxResults": 1, "total": 2,
    "issues": [{
        "key": "EMB-1",
        "fields": {"summary": "Thermal shutdown",
                   "description": "50 ms budget",
                   "status": {"name": "Done"},
                   "created": "2025-01-01T00:00:00.000+0000",
                   "resolutiondate": "2025-02-01T00:00:00.000+0000"}}]}
PAGE2 = {
    "startAt": 1, "maxResults": 1, "total": 2,
    "issues": [{
        "key": "EMB-2",
        "fields": {"summary": "Debounce", "description": None,
                   "status": {"name": "Open"},
                   "created": "2025-03-01T00:00:00.000+0000",
                   "resolutiondate": None}}]}


def fake_fetch(url, params, token):
    assert url == "https://jira.example/rest/api/2/search"
    assert token == "tkn"
    return PAGE1 if params["startAt"] == 0 else PAGE2


def test_ingest_jira_pages_and_inserts(tmp_path):
    conn = connect(tmp_path / "e.db")
    n = ingest_jira(conn, "https://jira.example", "project = EMB",
                    "tkn", fetch=fake_fetch)
    assert n == 2
    row = conn.execute("SELECT * FROM tickets WHERE key='EMB-1'").fetchone()
    assert row["summary"] == "Thermal shutdown"
    assert row["status"] == "Done"
    row2 = conn.execute("SELECT * FROM tickets WHERE key='EMB-2'").fetchone()
    assert row2["description"] == ""
    assert row2["resolved"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jira_connector.py -v`
Expected: FAIL with `ModuleNotFoundError` on `jira_connector`.

- [ ] **Step 3: Write the implementation**

`src/archeon/connectors/jira_connector.py`:

```python
import sqlite3

import requests


def _default_fetch(url: str, params: dict, token: str) -> dict:
    resp = requests.get(url, params=params,
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=30)
    resp.raise_for_status()
    return resp.json()


def ingest_jira(conn: sqlite3.Connection, base_url: str, jql: str,
                token: str, fetch=None) -> int:
    fetch = fetch or _default_fetch
    url = f"{base_url}/rest/api/2/search"
    start, inserted = 0, 0
    while True:
        data = fetch(url, {"jql": jql, "startAt": start,
                           "maxResults": 100}, token)
        for issue in data["issues"]:
            f = issue["fields"]
            conn.execute(
                "INSERT OR REPLACE INTO tickets(key, summary, description, "
                "status, created, resolved) VALUES (?, ?, ?, ?, ?, ?)",
                (issue["key"], f.get("summary") or "",
                 f.get("description") or "",
                 (f.get("status") or {}).get("name"),
                 f.get("created"), f.get("resolutiondate")))
            inserted += 1
        start += len(data["issues"])
        if start >= data["total"] or not data["issues"]:
            break
    conn.commit()
    return inserted
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_jira_connector.py -v`
Expected: 1 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/archeon/connectors/jira_connector.py tests/test_jira_connector.py
git commit -m "feat: jira connector with injectable fetcher"
```

---

### Task 4: Pull-request connector (GitHub-style REST)

**Files:**
- Create: `src/archeon/connectors/pr_connector.py`
- Create: `tests/test_pr_connector.py`

**Interfaces:**
- Consumes: `archeon.db.connect`.
- Produces: `ingest_prs(conn, api_base: str, repo: str, token: str, fetch=None) -> int` — pages through merged PRs and their review comments, fills `prs` and `pr_comments`, returns PR count. `fetch(url, params, token) -> list[dict]` injectable; default uses `requests`. Other git hosts become future connector plugins with the same table contract.

- [ ] **Step 1: Write the failing test**

`tests/test_pr_connector.py`:

```python
from archeon.connectors.pr_connector import ingest_prs
from archeon.db import connect

PRS = [{"number": 482, "title": "Add debounce",
        "body": "Fixes EMB-2", "user": {"login": "dev1"},
        "merged_at": "2025-04-01T00:00:00Z", "merge_commit_sha": "beef"},
       {"number": 483, "title": "WIP", "body": "", "user": {"login": "dev2"},
        "merged_at": None, "merge_commit_sha": None}]
COMMENTS = [{"id": 9001, "user": {"login": "dev2"},
             "body": "added debounce so transient spikes don't trip shutdown",
             "path": "fault_handler.c",
             "created_at": "2025-03-30T00:00:00Z"}]


def fake_fetch(url, params, token):
    if url.endswith("/pulls"):
        return PRS if params["page"] == 1 else []
    if url.endswith("/pulls/482/comments"):
        return COMMENTS if params["page"] == 1 else []
    raise AssertionError(f"unexpected url {url}")


def test_ingest_prs_merged_only_with_comments(tmp_path):
    conn = connect(tmp_path / "e.db")
    n = ingest_prs(conn, "https://api.github.example", "org/repo", "tkn",
                   fetch=fake_fetch)
    assert n == 1
    pr = conn.execute("SELECT * FROM prs WHERE number=482").fetchone()
    assert pr["merge_sha"] == "beef"
    c = conn.execute("SELECT * FROM pr_comments").fetchone()
    assert c["pr_number"] == 482
    assert "debounce" in c["body"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pr_connector.py -v`
Expected: FAIL with `ModuleNotFoundError` on `pr_connector`.

- [ ] **Step 3: Write the implementation**

`src/archeon/connectors/pr_connector.py`:

```python
import sqlite3

import requests


def _default_fetch(url: str, params: dict, token: str) -> list:
    resp = requests.get(url, params=params,
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=30)
    resp.raise_for_status()
    return resp.json()


def _pages(fetch, url: str, token: str):
    page = 1
    while True:
        batch = fetch(url, {"state": "closed", "per_page": 100,
                            "page": page}, token)
        if not batch:
            return
        yield from batch
        page += 1


def ingest_prs(conn: sqlite3.Connection, api_base: str, repo: str,
               token: str, fetch=None) -> int:
    fetch = fetch or _default_fetch
    base = f"{api_base}/repos/{repo}"
    inserted = 0
    for pr in _pages(fetch, f"{base}/pulls", token):
        if not pr.get("merged_at"):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO prs(number, title, body, author, "
            "merged_at, merge_sha) VALUES (?, ?, ?, ?, ?, ?)",
            (pr["number"], pr.get("title") or "", pr.get("body") or "",
             (pr.get("user") or {}).get("login"), pr["merged_at"],
             pr.get("merge_commit_sha")))
        for c in _pages(fetch, f"{base}/pulls/{pr['number']}/comments",
                        token):
            conn.execute(
                "INSERT OR REPLACE INTO pr_comments(id, pr_number, author, "
                "body, path, created) VALUES (?, ?, ?, ?, ?, ?)",
                (str(c["id"]), pr["number"],
                 (c.get("user") or {}).get("login"),
                 c.get("body") or "", c.get("path"), c.get("created_at")))
        inserted += 1
    conn.commit()
    return inserted
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pr_connector.py -v`
Expected: 1 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/archeon/connectors/pr_connector.py tests/test_pr_connector.py
git commit -m "feat: PR connector for merged PRs and review comments"
```

---

### Task 5: Confluence export connector

**Files:**
- Create: `src/archeon/connectors/wiki_connector.py`
- Create: `tests/test_wiki_connector.py`

**Interfaces:**
- Consumes: `archeon.db.connect`.
- Produces: `ingest_wiki_export(conn, export_dir: Path) -> int` — walks a Confluence HTML space export directory, extracts `<title>` and visible text from each `.html` file (stdlib `HTMLParser`, scripts/styles stripped), inserts into `wiki_pages` (id = filename stem), returns page count.

- [ ] **Step 1: Write the failing test**

`tests/test_wiki_connector.py`:

```python
from archeon.connectors.wiki_connector import ingest_wiki_export
from archeon.db import connect

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_wiki_connector.py -v`
Expected: FAIL with `ModuleNotFoundError` on `wiki_connector`.

- [ ] **Step 3: Write the implementation**

`src/archeon/connectors/wiki_connector.py`:

```python
import sqlite3
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.chunks: list[str] = []
        self._stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        self._stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self._stack:
            self._stack.reverse()
            self._stack.remove(tag)
            self._stack.reverse()

    def handle_data(self, data):
        if any(t in ("script", "style") for t in self._stack):
            return
        if "title" in self._stack:
            self.title += data
            return
        text = data.strip()
        if text:
            self.chunks.append(text)


def ingest_wiki_export(conn: sqlite3.Connection, export_dir: Path) -> int:
    inserted = 0
    for html_file in sorted(export_dir.rglob("*.html")):
        parser = _TextExtractor()
        parser.feed(html_file.read_text(encoding="utf-8", errors="replace"))
        updated = datetime.fromtimestamp(
            html_file.stat().st_mtime, tz=timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO wiki_pages(id, title, body_text, "
            "updated) VALUES (?, ?, ?, ?)",
            (html_file.stem, parser.title.strip(),
             "\n".join(parser.chunks), updated))
        inserted += 1
    conn.commit()
    return inserted
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_wiki_connector.py -v`
Expected: 1 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/archeon/connectors/wiki_connector.py tests/test_wiki_connector.py
git commit -m "feat: confluence HTML export connector"
```

---

### Task 6: Heuristic link extraction

**Files:**
- Create: `src/archeon/analysis/__init__.py`
- Create: `src/archeon/analysis/link_heuristics.py`
- Create: `tests/test_link_heuristics.py`

**Interfaces:**
- Consumes: tables `commits`, `prs` populated by Tasks 2 and 4.
- Produces: `extract_heuristic_links(conn, project_keys: list[str]) -> int` — writes rows into `links`:
  - commit→ticket: ticket key regex in commit message, `method='key_regex'`, confidence 1.0, `src_type='commit'`, `dst_type='ticket'`.
  - pr→ticket: key regex in PR title or body, `method='key_regex'`.
  - pr→commit: from `prs.merge_sha`, `method='merge_sha'`.
  Returns number of links written. Only keys of tickets that exist in `tickets` are linked (a mentioned-but-unknown key is noise, not a link).
- Produces: `key_pattern(project_keys: list[str]) -> re.Pattern` — the shared ticket-key regex helper.

- [ ] **Step 1: Write the failing test**

`tests/test_link_heuristics.py`:

```python
from archeon.analysis.link_heuristics import extract_heuristic_links
from archeon.db import connect


def _seed(conn):
    conn.execute("INSERT INTO tickets(key, summary) VALUES ('EMB-1', 's')")
    conn.execute("INSERT INTO tickets(key, summary) VALUES ('EMB-2', 's')")
    conn.execute("INSERT INTO commits(sha, author, date, message) VALUES "
                 "('c1', 'a', 'd', 'EMB-1: fix thermal shutdown')")
    conn.execute("INSERT INTO commits(sha, author, date, message) VALUES "
                 "('c2', 'a', 'd', 'refactor, see EMB-99 maybe')")
    conn.execute("INSERT INTO prs(number, title, body, author, merged_at, "
                 "merge_sha) VALUES (482, 'Add debounce', 'Fixes EMB-2', "
                 "'dev', 'd', 'c2')")
    conn.commit()


def test_extract_heuristic_links(tmp_path):
    conn = connect(tmp_path / "e.db")
    _seed(conn)
    n = extract_heuristic_links(conn, ["EMB"])
    rows = {(r["src_type"], r["src_ref"], r["dst_type"], r["dst_ref"],
             r["method"]) for r in conn.execute("SELECT * FROM links")}
    assert ("commit", "c1", "ticket", "EMB-1", "key_regex") in rows
    assert ("pr", "482", "ticket", "EMB-2", "key_regex") in rows
    assert ("pr", "482", "commit", "c2", "merge_sha") in rows
    assert not any(r[3] == "EMB-99" for r in rows)  # unknown ticket ignored
    assert n == len(rows) == 3
    # idempotent
    assert extract_heuristic_links(conn, ["EMB"]) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_link_heuristics.py -v`
Expected: FAIL with `ModuleNotFoundError` on `link_heuristics`.

- [ ] **Step 3: Write the implementation**

`src/archeon/analysis/__init__.py`: empty file.

`src/archeon/analysis/link_heuristics.py`:

```python
import re
import sqlite3


def key_pattern(project_keys: list[str]) -> re.Pattern:
    alternation = "|".join(re.escape(k) for k in project_keys)
    return re.compile(rf"\b(?:{alternation})-\d+\b")


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
    for row in conn.execute("SELECT number, title, body, merge_sha FROM prs"):
        text = f"{row['title']}\n{row['body']}"
        for key in set(pattern.findall(text)) & known:
            written += _insert(conn, "pr", row["number"], "ticket", key,
                               "key_regex", 1.0)
        if row["merge_sha"]:
            written += _insert(conn, "pr", row["number"], "commit",
                               row["merge_sha"], "merge_sha", 1.0)
    conn.commit()
    return written
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_link_heuristics.py -v`
Expected: 1 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/archeon/analysis tests/test_link_heuristics.py
git commit -m "feat: heuristic commit/PR/ticket link extraction"
```

---

### Task 7: Change-coupling analysis

**Files:**
- Create: `src/archeon/analysis/coupling.py`
- Create: `tests/test_coupling.py`

**Interfaces:**
- Consumes: `commit_files` populated by Task 2.
- Produces: `compute_coupling(conn, max_files_per_commit: int = 30) -> int` — recomputes the `coupling` table from scratch (DELETE then insert): for every unordered file pair co-changed in a commit (commits touching more than `max_files_per_commit` files are skipped as bulk/refactor noise), stores `co_changes` plus each file's total change count (`support_a`, `support_b`). Pair key is sorted so (a,b) is stored once. Returns pair count.
- Produces: `strongest_pairs(conn, limit: int = 20) -> list[sqlite3.Row]` — pairs ordered by `co_changes * 1.0 / MIN(support_a, support_b)` descending, for the stats CLI.

- [ ] **Step 1: Write the failing test**

`tests/test_coupling.py`:

```python
from archeon.analysis.coupling import compute_coupling, strongest_pairs
from archeon.db import connect


def _seed(conn):
    rows = [("c1", "a.c"), ("c1", "b.c"),
            ("c2", "a.c"), ("c2", "b.c"),
            ("c3", "a.c"), ("c3", "z.c")]
    for sha, path in rows:
        conn.execute("INSERT OR IGNORE INTO commits(sha, author, date, "
                     "message) VALUES (?, '', '', '')", (sha,))
        conn.execute("INSERT INTO commit_files(sha, path, additions, "
                     "deletions) VALUES (?, ?, 1, 0)", (sha, path))
    conn.commit()


def test_compute_coupling_counts_pairs(tmp_path):
    conn = connect(tmp_path / "e.db")
    _seed(conn)
    n = compute_coupling(conn)
    assert n == 2  # distinct pairs: (a.c, b.c) and (a.c, z.c)
    ab = conn.execute("SELECT * FROM coupling WHERE path_a='a.c' AND "
                      "path_b='b.c'").fetchone()
    assert ab["co_changes"] == 2
    assert ab["support_a"] == 3   # a.c changed in 3 commits
    assert ab["support_b"] == 2   # b.c changed in 2 commits
    top = strongest_pairs(conn, limit=1)[0]
    assert (top["path_a"], top["path_b"]) == ("a.c", "b.c")


def test_bulk_commits_skipped(tmp_path):
    conn = connect(tmp_path / "e.db")
    conn.execute("INSERT INTO commits(sha, author, date, message) "
                 "VALUES ('big', '', '', '')")
    for i in range(40):
        conn.execute("INSERT INTO commit_files(sha, path, additions, "
                     "deletions) VALUES ('big', ?, 1, 0)", (f"f{i}.c",))
    conn.commit()
    assert compute_coupling(conn, max_files_per_commit=30) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_coupling.py -v`
Expected: FAIL with `ModuleNotFoundError` on `coupling`.

- [ ] **Step 3: Write the implementation**

`src/archeon/analysis/coupling.py`:

```python
import sqlite3
from collections import Counter
from itertools import combinations


def compute_coupling(conn: sqlite3.Connection,
                     max_files_per_commit: int = 30) -> int:
    conn.execute("DELETE FROM coupling")
    support: Counter = Counter()
    pairs: Counter = Counter()
    by_commit: dict[str, list[str]] = {}
    for row in conn.execute("SELECT sha, path FROM commit_files"):
        by_commit.setdefault(row["sha"], []).append(row["path"])
    for files in by_commit.values():
        if len(files) > max_files_per_commit:
            continue
        for path in files:
            support[path] += 1
        for a, b in combinations(sorted(files), 2):
            pairs[(a, b)] += 1
    conn.executemany(
        "INSERT INTO coupling(path_a, path_b, co_changes, support_a, "
        "support_b) VALUES (?, ?, ?, ?, ?)",
        [(a, b, co, support[a], support[b])
         for (a, b), co in pairs.items()])
    conn.commit()
    return len(pairs)


def strongest_pairs(conn: sqlite3.Connection, limit: int = 20):
    return conn.execute(
        "SELECT *, co_changes * 1.0 / MIN(support_a, support_b) AS strength "
        "FROM coupling ORDER BY strength DESC, co_changes DESC LIMIT ?",
        (limit,)).fetchall()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_coupling.py -v`
Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/archeon/analysis/coupling.py tests/test_coupling.py
git commit -m "feat: change-coupling analysis over commit history"
```

---

### Task 8: Code graph — tree-sitter scan with clang upgrade and gap recording

**Files:**
- Create: `src/archeon/codegraph/__init__.py`
- Create: `src/archeon/codegraph/ts_scan.py`
- Create: `src/archeon/codegraph/clang_scan.py`
- Create: `src/archeon/codegraph/scan.py`
- Create: `tests/test_ts_scan.py`
- Create: `tests/test_clang_scan.py`
- Create: `tests/test_scan_merge.py`

**Interfaces:**
- Consumes: `archeon.db.connect`.
- Produces (ts_scan): `ts_symbols(path: Path) -> list[dict]` — dicts `{name, kind, line, end_line, signature}` for function definitions and struct declarations via tree-sitter (`.c/.h` → C grammar; `.cc/.cpp/.cxx/.hpp` → C++ grammar). Raises `ValueError` on other suffixes.
- Produces (clang_scan): `clang_symbols(file_path: Path, compile_db_dir: Path) -> list[dict]` — same dict shape via libclang using `compile_commands.json`; raises `RuntimeError` if the file has no compile command or libclang fails.
- Produces (scan): `scan_component(conn, root: Path, path_prefixes: list[str], compile_db_dir: Path | None) -> dict` — walks source files under the prefixes; tries clang first (when a compile DB is given), falls back to tree-sitter, records failures in `scan_gaps` with a reason; writes `symbols` rows with `source` column `'clang'` or `'tree-sitter'`; returns `{"clang": n1, "tree_sitter": n2, "gaps": n3}` (file counts). Re-runs clear previous `symbols`/`scan_gaps` for the scanned prefixes first.

- [ ] **Step 1: Write the failing tree-sitter test**

`tests/test_ts_scan.py`:

```python
from pathlib import Path

from archeon.codegraph.ts_scan import ts_symbols

C_SRC = """
struct motor_state { int temp; };

static int clamp(int v) { return v; }

int enter_state(int s) {
    return clamp(s);
}
"""


def test_ts_symbols_functions_and_structs(tmp_path):
    f = tmp_path / "m.c"
    f.write_text(C_SRC)
    syms = ts_symbols(f)
    by_name = {s["name"]: s for s in syms}
    assert by_name["enter_state"]["kind"] == "function"
    assert by_name["clamp"]["kind"] == "function"
    assert by_name["motor_state"]["kind"] == "struct"
    assert by_name["enter_state"]["line"] > by_name["clamp"]["line"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ts_scan.py -v`
Expected: FAIL with `ModuleNotFoundError` on `ts_scan`.

- [ ] **Step 3: Implement tree-sitter scan**

`src/archeon/codegraph/__init__.py`: empty file.

`src/archeon/codegraph/ts_scan.py`:

```python
from pathlib import Path

import tree_sitter_c
import tree_sitter_cpp
from tree_sitter import Language, Parser

C_SUFFIXES = {".c", ".h"}
CPP_SUFFIXES = {".cc", ".cpp", ".cxx", ".hpp"}


def _language(path: Path) -> Language:
    if path.suffix in C_SUFFIXES:
        return Language(tree_sitter_c.language())
    if path.suffix in CPP_SUFFIXES:
        return Language(tree_sitter_cpp.language())
    raise ValueError(f"unsupported suffix: {path.suffix}")


def _identifier(node, src: bytes) -> str | None:
    if node.type in ("identifier", "field_identifier", "type_identifier",
                     "qualified_identifier"):
        return src[node.start_byte:node.end_byte].decode(
            "utf-8", errors="replace")
    for child in node.children:
        name = _identifier(child, src)
        if name:
            return name
    return None


def ts_symbols(path: Path) -> list[dict]:
    src = path.read_bytes()
    tree = Parser(_language(path)).parse(src)
    symbols = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "function_definition":
            declarator = node.child_by_field_name("declarator")
            name = _identifier(declarator, src) if declarator else None
            if name:
                sig = src[node.start_byte:node.end_byte].split(b"{")[0]
                symbols.append({
                    "name": name, "kind": "function",
                    "line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "signature": sig.decode("utf-8",
                                            errors="replace").strip()})
        elif node.type in ("struct_specifier", "class_specifier"):
            name_node = node.child_by_field_name("name")
            if name_node is not None and node.child_by_field_name("body"):
                symbols.append({
                    "name": src[name_node.start_byte:name_node.end_byte]
                    .decode("utf-8", errors="replace"),
                    "kind": "struct" if node.type == "struct_specifier"
                    else "class",
                    "line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "signature": ""})
        stack.extend(node.children)
    return sorted(symbols, key=lambda s: s["line"])
```

Run: `uv run pytest tests/test_ts_scan.py -v`
Expected: 1 PASSED.

- [ ] **Step 4: Write the failing clang test**

`tests/test_clang_scan.py`:

```python
import json

import pytest

from archeon.codegraph.clang_scan import clang_symbols

C_SRC = "int enter_state(int s) { return s; }\n"


def test_clang_symbols(tmp_path):
    pytest.importorskip("clang.cindex")
    src = tmp_path / "m.c"
    src.write_text(C_SRC)
    (tmp_path / "compile_commands.json").write_text(json.dumps([
        {"directory": str(tmp_path), "file": str(src),
         "arguments": ["cc", "-c", str(src)]}]))
    try:
        syms = clang_symbols(src, tmp_path)
    except RuntimeError as e:
        pytest.skip(f"libclang unavailable: {e}")
    names = {s["name"] for s in syms}
    assert "enter_state" in names
    fn = next(s for s in syms if s["name"] == "enter_state")
    assert fn["kind"] == "function"
    assert fn["line"] == 1


def test_clang_symbols_missing_compile_command(tmp_path):
    pytest.importorskip("clang.cindex")
    src = tmp_path / "orphan.c"
    src.write_text(C_SRC)
    (tmp_path / "compile_commands.json").write_text("[]")
    with pytest.raises(RuntimeError):
        clang_symbols(src, tmp_path)
```

- [ ] **Step 5: Run clang test to verify it fails**

Run: `uv run pytest tests/test_clang_scan.py -v`
Expected: FAIL with `ModuleNotFoundError` on `clang_scan`.

- [ ] **Step 6: Implement clang scan**

`src/archeon/codegraph/clang_scan.py`:

```python
from pathlib import Path


def clang_symbols(file_path: Path, compile_db_dir: Path) -> list[dict]:
    try:
        from clang import cindex
        index = cindex.Index.create()
        compdb = cindex.CompilationDatabase.fromDirectory(str(compile_db_dir))
    except Exception as e:  # library load or DB load failure
        raise RuntimeError(f"clang init failed: {e}") from e
    commands = compdb.getCompileCommands(str(file_path))
    if not commands:
        raise RuntimeError(f"no compile command for {file_path}")
    args = [a for a in list(commands[0].arguments)[1:]
            if a not in ("-c", str(file_path))]
    try:
        tu = index.parse(str(file_path), args=args)
    except Exception as e:
        raise RuntimeError(f"clang parse failed: {e}") from e
    kinds = {
        "FUNCTION_DECL": "function",
        "CXX_METHOD": "function",
        "STRUCT_DECL": "struct",
        "CLASS_DECL": "class",
    }
    symbols = []
    for cursor in tu.cursor.walk_preorder():
        kind = kinds.get(cursor.kind.name)
        if not kind or not cursor.is_definition():
            continue
        if cursor.location.file is None or \
                Path(str(cursor.location.file)) != file_path:
            continue
        symbols.append({
            "name": cursor.spelling, "kind": kind,
            "line": cursor.extent.start.line,
            "end_line": cursor.extent.end.line,
            "signature": cursor.displayname})
    return sorted(symbols, key=lambda s: s["line"])
```

Run: `uv run pytest tests/test_clang_scan.py -v`
Expected: 2 PASSED (or SKIPPED with a libclang-unavailable message — acceptable only on machines without the wheel's native library; CI/dev machines with `uv sync` completed should PASS).

- [ ] **Step 7: Write the failing merge test**

`tests/test_scan_merge.py`:

```python
from archeon.codegraph.scan import scan_component
from archeon.db import connect

GOOD = "int f(void) { return 1; }\n"


def test_scan_falls_back_and_records_gaps(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "ok.c").write_text(GOOD)
    (root / "src" / "weird.xc").write_text("not c")
    conn = connect(tmp_path / "e.db")

    stats = scan_component(conn, root, ["src/"], compile_db_dir=None)
    assert stats["tree_sitter"] == 1
    assert stats["clang"] == 0
    assert stats["gaps"] == 1
    sym = conn.execute("SELECT * FROM symbols WHERE name='f'").fetchone()
    assert sym["source"] == "tree-sitter"
    assert sym["path"] == "src/ok.c"
    gap = conn.execute("SELECT * FROM scan_gaps").fetchone()
    assert gap["path"] == "src/weird.xc"


def test_rescan_replaces_previous_results(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "ok.c").write_text(GOOD)
    conn = connect(tmp_path / "e.db")
    scan_component(conn, root, ["src/"], compile_db_dir=None)
    scan_component(conn, root, ["src/"], compile_db_dir=None)
    count = conn.execute("SELECT COUNT(*) AS c FROM symbols").fetchone()["c"]
    assert count == 1
```

- [ ] **Step 8: Run merge test to verify it fails**

Run: `uv run pytest tests/test_scan_merge.py -v`
Expected: FAIL with `ModuleNotFoundError` on `scan`.

- [ ] **Step 9: Implement the merged scan**

`src/archeon/codegraph/scan.py`:

```python
import sqlite3
from pathlib import Path

from archeon.codegraph.clang_scan import clang_symbols
from archeon.codegraph.ts_scan import C_SUFFIXES, CPP_SUFFIXES, ts_symbols

SOURCE_SUFFIXES = C_SUFFIXES | CPP_SUFFIXES | {".xc", ".s", ".asm"}


def _insert_symbols(conn, rel_path: str, symbols: list[dict],
                    source: str) -> None:
    conn.executemany(
        "INSERT INTO symbols(name, kind, path, line, end_line, signature, "
        "source) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(s["name"], s["kind"], rel_path, s["line"], s["end_line"],
          s["signature"], source) for s in symbols])


def scan_component(conn: sqlite3.Connection, root: Path,
                   path_prefixes: list[str],
                   compile_db_dir: Path | None) -> dict:
    for prefix in path_prefixes:
        conn.execute("DELETE FROM symbols WHERE path LIKE ?", (prefix + "%",))
        conn.execute("DELETE FROM scan_gaps WHERE path LIKE ?",
                     (prefix + "%",))
    stats = {"clang": 0, "tree_sitter": 0, "gaps": 0}
    for prefix in path_prefixes:
        base = root / prefix
        for f in sorted(p for p in base.rglob("*")
                        if p.suffix in SOURCE_SUFFIXES and p.is_file()):
            rel = f.relative_to(root).as_posix()
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
    return stats
```

- [ ] **Step 10: Run all codegraph tests**

Run: `uv run pytest tests/test_ts_scan.py tests/test_clang_scan.py tests/test_scan_merge.py -v`
Expected: all PASSED (clang tests may SKIP only where the native library truly can't load).

- [ ] **Step 11: Commit**

```bash
git add src/archeon/codegraph tests/test_ts_scan.py tests/test_clang_scan.py tests/test_scan_merge.py
git commit -m "feat: C/C++ code graph with clang-first scan and gap recording"
```

---

### Task 9: Link-recovery evaluation harness

**Files:**
- Create: `src/archeon/analysis/link_eval.py`
- Create: `tests/test_link_eval.py`

**Interfaces:**
- Consumes: `links` table (Tasks 6 and 10 write it).
- Produces: `load_gold(csv_path: Path) -> tuple[set[tuple[str, str]], set[str]]` — reads a hand-labeled CSV with header `sha,ticket_key`; a row with an empty `ticket_key` means "this commit has no ticket". Returns `(gold_pairs, sampled_shas)`.
- Produces: `evaluate(conn, gold_pairs: set, sampled_shas: set, methods: list[str] | None = None) -> dict` — compares predicted commit→ticket links (restricted to sampled shas, optionally to given `methods`) against gold. Returns `{"precision": float, "recall": float, "predicted": int, "gold": int, "true_positives": int}`. Division-by-zero yields 0.0. This metric is the P0 exit deliverable.

- [ ] **Step 1: Write the failing test**

`tests/test_link_eval.py`:

```python
from archeon.analysis.link_eval import evaluate, load_gold
from archeon.db import connect

GOLD_CSV = """sha,ticket_key
c1,EMB-1
c2,
c3,EMB-3
"""


def _predict(conn, sha, key, method="key_regex"):
    conn.execute(
        "INSERT INTO links(src_type, src_ref, dst_type, dst_ref, method, "
        "confidence) VALUES ('commit', ?, 'ticket', ?, ?, 0.9)",
        (sha, key, method))


def test_load_gold(tmp_path):
    p = tmp_path / "gold.csv"
    p.write_text(GOLD_CSV)
    gold, sampled = load_gold(p)
    assert gold == {("c1", "EMB-1"), ("c3", "EMB-3")}
    assert sampled == {"c1", "c2", "c3"}


def test_evaluate_precision_recall(tmp_path):
    p = tmp_path / "gold.csv"
    p.write_text(GOLD_CSV)
    gold, sampled = load_gold(p)
    conn = connect(tmp_path / "e.db")
    _predict(conn, "c1", "EMB-1")          # true positive
    _predict(conn, "c2", "EMB-9")          # false positive
    _predict(conn, "c99", "EMB-1")         # outside sample: ignored
    conn.commit()
    m = evaluate(conn, gold, sampled)
    assert m["true_positives"] == 1
    assert m["predicted"] == 2
    assert m["precision"] == 0.5
    assert m["recall"] == 0.5              # 1 of 2 gold pairs found


def test_evaluate_empty_predictions(tmp_path):
    conn = connect(tmp_path / "e.db")
    m = evaluate(conn, {("c1", "EMB-1")}, {"c1"})
    assert m["precision"] == 0.0 and m["recall"] == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_link_eval.py -v`
Expected: FAIL with `ModuleNotFoundError` on `link_eval`.

- [ ] **Step 3: Write the implementation**

`src/archeon/analysis/link_eval.py`:

```python
import csv
import sqlite3
from pathlib import Path


def load_gold(csv_path: Path) -> tuple[set[tuple[str, str]], set[str]]:
    gold: set[tuple[str, str]] = set()
    sampled: set[str] = set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sha = row["sha"].strip()
            key = (row["ticket_key"] or "").strip()
            sampled.add(sha)
            if key:
                gold.add((sha, key))
    return gold, sampled


def evaluate(conn: sqlite3.Connection, gold_pairs: set,
             sampled_shas: set, methods: list[str] | None = None) -> dict:
    query = ("SELECT src_ref, dst_ref FROM links "
             "WHERE src_type='commit' AND dst_type='ticket'")
    params: list = []
    if methods:
        query += f" AND method IN ({','.join('?' * len(methods))})"
        params = list(methods)
    predicted = {(r["src_ref"], r["dst_ref"])
                 for r in conn.execute(query, params)
                 if r["src_ref"] in sampled_shas}
    tp = len(predicted & gold_pairs)
    precision = tp / len(predicted) if predicted else 0.0
    recall = tp / len(gold_pairs) if gold_pairs else 0.0
    return {"precision": precision, "recall": recall,
            "predicted": len(predicted), "gold": len(gold_pairs),
            "true_positives": tp}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_link_eval.py -v`
Expected: 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/archeon/analysis/link_eval.py tests/test_link_eval.py
git commit -m "feat: link-recovery precision/recall evaluation harness"
```

---

### Task 10: LLM link recovery (cheap tier)

**Files:**
- Create: `src/archeon/analysis/link_llm.py`
- Create: `tests/test_link_llm.py`

**Interfaces:**
- Consumes: `commits`, `tickets`, `links` tables (Tasks 2, 3, 6).
- Produces: `candidate_tickets(conn, sha: str, window_days: int = 30, limit: int = 5) -> list[sqlite3.Row]` — tickets created before/resolved after (± window) the commit date, ranked by word overlap between commit message and ticket summary+description.
- Produces: `recover_links(conn, client, model: str, max_commits: int = 200) -> int` — for each commit with no existing commit→ticket link (any method), asks the model to pick one candidate ticket key or NONE, inserts `method='llm'`, confidence 0.7. `client` is an `anthropic.Anthropic`-compatible object (`client.messages.create(...)` returning an object whose `content[0].text` is the answer); tests pass a fake. Hard cap `max_commits` bounds cost.

- [ ] **Step 1: Write the failing test**

`tests/test_link_llm.py`:

```python
from archeon.analysis.link_llm import candidate_tickets, recover_links
from archeon.db import connect


class FakeResponse:
    def __init__(self, text):
        self.content = [type("Block", (), {"text": text})()]


class FakeClient:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    class _Messages:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            self.outer.calls.append(kwargs)
            return FakeResponse(self.outer.answers.pop(0))

    @property
    def messages(self):
        return FakeClient._Messages(self)


def _seed(conn):
    conn.execute("INSERT INTO tickets(key, summary, description, created, "
                 "resolved) VALUES ('EMB-1', 'thermal shutdown budget', "
                 "'', '2025-01-01T00:00:00', '2025-06-01T00:00:00')")
    conn.execute("INSERT INTO tickets(key, summary, description, created, "
                 "resolved) VALUES ('EMB-2', 'ui polish', '', "
                 "'2025-01-01T00:00:00', '2025-06-01T00:00:00')")
    conn.execute("INSERT INTO commits(sha, author, date, message) VALUES "
                 "('c1', 'a', '2025-03-01T00:00:00', "
                 "'fix thermal shutdown timing')")
    conn.execute("INSERT INTO commits(sha, author, date, message) VALUES "
                 "('c2', 'a', '2025-03-02T00:00:00', 'polish ui colors')")
    conn.commit()


def test_candidate_tickets_ranked_by_overlap(tmp_path):
    conn = connect(tmp_path / "e.db")
    _seed(conn)
    cands = candidate_tickets(conn, "c1")
    assert cands[0]["key"] == "EMB-1"


def test_recover_links_inserts_llm_links(tmp_path):
    conn = connect(tmp_path / "e.db")
    _seed(conn)
    client = FakeClient(answers=["EMB-1", "NONE"])
    n = recover_links(conn, client, model="cheap-model")
    assert n == 1
    row = conn.execute("SELECT * FROM links WHERE method='llm'").fetchone()
    assert (row["src_ref"], row["dst_ref"]) == ("c1", "EMB-1")
    assert row["confidence"] == 0.7
    assert client.calls[0]["model"] == "cheap-model"


def test_recover_links_skips_already_linked(tmp_path):
    conn = connect(tmp_path / "e.db")
    _seed(conn)
    conn.execute("INSERT INTO links(src_type, src_ref, dst_type, dst_ref, "
                 "method, confidence) VALUES ('commit', 'c1', 'ticket', "
                 "'EMB-1', 'key_regex', 1.0)")
    conn.commit()
    client = FakeClient(answers=["NONE"])
    recover_links(conn, client, model="cheap-model")
    assert len(client.calls) == 1  # only c2 was asked about
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_link_llm.py -v`
Expected: FAIL with `ModuleNotFoundError` on `link_llm`.

- [ ] **Step 3: Write the implementation**

`src/archeon/analysis/link_llm.py`:

```python
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
        "SELECT * FROM tickets WHERE created <= datetime(?, ?) AND "
        "(resolved IS NULL OR resolved >= datetime(?, ?))",
        (commit["date"], f"+{window_days} days",
         commit["date"], f"-{window_days} days")).fetchall()
    msg_words = _words(commit["message"])
    scored = [(len(msg_words & _words(f"{r['summary']} {r['description']}")),
               r) for r in rows]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [r for score, r in scored[:limit] if score > 0]


def recover_links(conn: sqlite3.Connection, client, model: str,
                  max_commits: int = 200) -> int:
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
        response = client.messages.create(
            model=model, max_tokens=16,
            messages=[{"role": "user", "content": PROMPT.format(
                message=commit["message"], candidates=listing)}])
        answer = response.content[0].text.strip()
        if answer in {c["key"] for c in cands}:
            conn.execute(
                "INSERT OR IGNORE INTO links(src_type, src_ref, dst_type, "
                "dst_ref, method, confidence) VALUES ('commit', ?, "
                "'ticket', ?, 'llm', 0.7)", (commit["sha"], answer))
            inserted += 1
    conn.commit()
    return inserted
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_link_llm.py -v`
Expected: 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/archeon/analysis/link_llm.py tests/test_link_llm.py
git commit -m "feat: cheap-tier LLM link recovery for unlinked commits"
```

---

### Task 11: Component config and CLI

**Files:**
- Create: `src/archeon/config.py`
- Create: `src/archeon/cli.py`
- Create: `tests/test_cli.py`
- Create: `README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: `archeon.config.load(path: Path) -> dict` — parses `archeon.toml` (stdlib `tomllib`) and validates required keys.
- Produces: CLI `archeon` with commands `ingest-git`, `ingest-jira`, `ingest-prs`, `ingest-wiki`, `link`, `link-llm`, `coupling`, `scan`, `eval`, `stats` — each takes `--config archeon.toml` (default `./archeon.toml`) and operates on the DB named in config. Tokens from env vars `ARCHEON_JIRA_TOKEN`, `ARCHEON_GIT_TOKEN`, `ANTHROPIC_API_KEY`.

Example `archeon.toml` (documented in README):

```toml
[component]
name = "motor_ctrl"
db = "evidence.db"
repo_path = "C:/work/monorepo"
path_prefixes = ["src/motor_ctrl/"]
compile_db_dir = "C:/work/monorepo/build"   # optional

[jira]
base_url = "https://jira.internal.example"
jql = "project = EMB AND component = motor_ctrl"
project_keys = ["EMB"]

[prs]
api_base = "https://api.github.example"
repo = "org/monorepo"

[wiki]
export_dir = "C:/work/confluence_export"

[llm]
cheap_model = "claude-haiku-4-5-20251001"
max_commits = 200
```

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:

```python
import subprocess
from pathlib import Path

from click.testing import CliRunner

from archeon.cli import main


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def _setup(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "src" / "a.c").write_text("int f(void) { return 1; }\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "EMB-1: add f")
    config = tmp_path / "archeon.toml"
    config.write_text(f"""
[component]
name = "demo"
db = "{(tmp_path / 'e.db').as_posix()}"
repo_path = "{repo.as_posix()}"
path_prefixes = ["src/"]

[jira]
base_url = "https://unused"
jql = "unused"
project_keys = ["EMB"]

[prs]
api_base = "https://unused"
repo = "o/r"

[wiki]
export_dir = "{(tmp_path / 'wiki').as_posix()}"

[llm]
cheap_model = "claude-haiku-4-5-20251001"
max_commits = 10
""")
    return config


def test_ingest_git_scan_link_stats(tmp_path):
    config = _setup(tmp_path)
    runner = CliRunner()
    r1 = runner.invoke(main, ["ingest-git", "--config", str(config)])
    assert r1.exit_code == 0, r1.output
    assert "commits: 1" in r1.output
    r2 = runner.invoke(main, ["scan", "--config", str(config)])
    assert r2.exit_code == 0, r2.output
    r3 = runner.invoke(main, ["coupling", "--config", str(config)])
    assert r3.exit_code == 0, r3.output
    r4 = runner.invoke(main, ["stats", "--config", str(config)])
    assert r4.exit_code == 0, r4.output
    assert "commits" in r4.output and "symbols" in r4.output


def test_eval_command(tmp_path):
    config = _setup(tmp_path)
    gold = tmp_path / "gold.csv"
    gold.write_text("sha,ticket_key\nc1,EMB-1\n")
    runner = CliRunner()
    r = runner.invoke(main, ["eval", "--config", str(config),
                             "--gold", str(gold)])
    assert r.exit_code == 0, r.output
    assert "precision" in r.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError` on `archeon.cli`.

- [ ] **Step 3: Write the implementation**

`src/archeon/config.py`:

```python
import tomllib
from pathlib import Path

REQUIRED = {"component": ["name", "db", "repo_path", "path_prefixes"]}


def load(path: Path) -> dict:
    with open(path, "rb") as f:
        config = tomllib.load(f)
    for section, keys in REQUIRED.items():
        if section not in config:
            raise ValueError(f"missing [{section}] in {path}")
        for key in keys:
            if key not in config[section]:
                raise ValueError(f"missing {section}.{key} in {path}")
    return config
```

`src/archeon/cli.py`:

```python
import os
from pathlib import Path

import click

from archeon import config as config_mod
from archeon.analysis.coupling import compute_coupling, strongest_pairs
from archeon.analysis.link_eval import evaluate, load_gold
from archeon.analysis.link_heuristics import extract_heuristic_links
from archeon.analysis.link_llm import recover_links
from archeon.codegraph.scan import scan_component
from archeon.connectors.git_connector import ingest_git
from archeon.connectors.jira_connector import ingest_jira
from archeon.connectors.pr_connector import ingest_prs
from archeon.connectors.wiki_connector import ingest_wiki_export
from archeon.db import connect


def _load(config_path: str):
    cfg = config_mod.load(Path(config_path))
    conn = connect(cfg["component"]["db"])
    return cfg, conn


config_option = click.option("--config", "config_path",
                             default="archeon.toml", show_default=True)


@click.group()
def main():
    """Archeon evidence lake."""


@main.command("ingest-git")
@config_option
def cli_ingest_git(config_path):
    cfg, conn = _load(config_path)
    n = ingest_git(conn, Path(cfg["component"]["repo_path"]),
                   cfg["component"]["path_prefixes"])
    click.echo(f"commits: {n}")


@main.command("ingest-jira")
@config_option
def cli_ingest_jira(config_path):
    cfg, conn = _load(config_path)
    n = ingest_jira(conn, cfg["jira"]["base_url"], cfg["jira"]["jql"],
                    os.environ["ARCHEON_JIRA_TOKEN"])
    click.echo(f"tickets: {n}")


@main.command("ingest-prs")
@config_option
def cli_ingest_prs(config_path):
    cfg, conn = _load(config_path)
    n = ingest_prs(conn, cfg["prs"]["api_base"], cfg["prs"]["repo"],
                   os.environ["ARCHEON_GIT_TOKEN"])
    click.echo(f"prs: {n}")


@main.command("ingest-wiki")
@config_option
def cli_ingest_wiki(config_path):
    cfg, conn = _load(config_path)
    n = ingest_wiki_export(conn, Path(cfg["wiki"]["export_dir"]))
    click.echo(f"pages: {n}")


@main.command("link")
@config_option
def cli_link(config_path):
    cfg, conn = _load(config_path)
    n = extract_heuristic_links(conn, cfg["jira"]["project_keys"])
    click.echo(f"links: {n}")


@main.command("link-llm")
@config_option
def cli_link_llm(config_path):
    import anthropic
    cfg, conn = _load(config_path)
    client = anthropic.Anthropic()
    n = recover_links(conn, client, cfg["llm"]["cheap_model"],
                      cfg["llm"].get("max_commits", 200))
    click.echo(f"llm links: {n}")


@main.command("coupling")
@config_option
def cli_coupling(config_path):
    cfg, conn = _load(config_path)
    n = compute_coupling(conn)
    click.echo(f"pairs: {n}")


@main.command("scan")
@config_option
def cli_scan(config_path):
    cfg, conn = _load(config_path)
    compile_db = cfg["component"].get("compile_db_dir")
    stats = scan_component(conn, Path(cfg["component"]["repo_path"]),
                           cfg["component"]["path_prefixes"],
                           Path(compile_db) if compile_db else None)
    click.echo(f"clang: {stats['clang']}  tree-sitter: "
               f"{stats['tree_sitter']}  gaps: {stats['gaps']}")


@main.command("eval")
@config_option
@click.option("--gold", "gold_path", required=True)
@click.option("--method", "methods", multiple=True)
def cli_eval(config_path, gold_path, methods):
    cfg, conn = _load(config_path)
    gold, sampled = load_gold(Path(gold_path))
    m = evaluate(conn, gold, sampled, list(methods) or None)
    click.echo(f"precision: {m['precision']:.3f}  recall: "
               f"{m['recall']:.3f}  predicted: {m['predicted']}  "
               f"gold: {m['gold']}")


@main.command("stats")
@config_option
def cli_stats(config_path):
    cfg, conn = _load(config_path)
    for table in ("commits", "tickets", "prs", "pr_comments", "wiki_pages",
                  "symbols", "scan_gaps", "links", "coupling"):
        count = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        click.echo(f"{table}: {count}")
    top = strongest_pairs(conn, 5)
    if top:
        click.echo("top coupling:")
        for r in top:
            click.echo(f"  {r['path_a']} <-> {r['path_b']} "
                       f"({r['co_changes']} co-changes)")
```

`README.md`:

```markdown
# Archeon

Evidence lake (P0): recovers requirements evidence from a codebase and its
artifacts. See docs/superpowers/specs/2026-07-23-archeon-design.md.

## Quickstart

    uv sync
    cp archeon.example.toml archeon.toml   # edit paths and Jira/PR settings
    set ARCHEON_JIRA_TOKEN=...             # or $env:ARCHEON_JIRA_TOKEN
    set ARCHEON_GIT_TOKEN=...
    uv run archeon ingest-git
    uv run archeon ingest-jira
    uv run archeon ingest-prs
    uv run archeon ingest-wiki
    uv run archeon scan
    uv run archeon coupling
    uv run archeon link
    uv run archeon link-llm                # needs ANTHROPIC_API_KEY
    uv run archeon stats

## Measuring link-recovery quality (P0 exit metric)

Hand-label a random sample of commits into `gold.csv`:

    sha,ticket_key
    abc123,EMB-42
    def456,

(empty ticket_key = commit genuinely has no ticket). Then:

    uv run archeon eval --gold gold.csv                  # all methods
    uv run archeon eval --gold gold.csv --method key_regex
    uv run archeon eval --gold gold.csv --method llm

## Tests

    uv run pytest
```

Also create `archeon.example.toml` with the example config from this task's header (copy it verbatim).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 2 PASSED.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: all tests PASS (clang tests may SKIP only where the native library can't load).

- [ ] **Step 6: Commit**

```bash
git add src/archeon/config.py src/archeon/cli.py tests/test_cli.py README.md archeon.example.toml
git commit -m "feat: component config and archeon CLI"
```

---

## P0 exit checklist (manual, on the golden component)

Not tasks for the executing engineer — this is what the plan's owner does with the built tool:

1. Write `archeon.toml` for the golden component; run all `ingest-*`, `scan`, `coupling`, `link`.
2. Hand-label ~100 randomly sampled commits into `gold.csv` (expect roughly half to have no ticket, per the research's ~42% linked baseline).
3. Run `eval` for `key_regex` alone, then with `llm` included. Record precision/recall for both in `docs/research/` — this is the P0 deliverable that decides whether P1 synthesis has a good-enough evidence graph to stand on.
4. Check `scan` gap count; unparsed files list becomes the input for analyzer improvements.
