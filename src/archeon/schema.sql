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
  branch TEXT,
  merged_at TEXT,
  merge_sha TEXT
);
CREATE TABLE IF NOT EXISTS pr_commits(
  pr_number INTEGER,
  sha TEXT,
  PRIMARY KEY (pr_number, sha)
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
CREATE TABLE IF NOT EXISTS symbol_edges(
  src_id INTEGER,
  dst_id INTEGER,
  kind TEXT,
  weight REAL,
  PRIMARY KEY (src_id, dst_id, kind)
);
CREATE TABLE IF NOT EXISTS file_edges(
  src_path TEXT,
  dst_path TEXT,
  kind TEXT,
  weight REAL,
  PRIMARY KEY (src_path, dst_path, kind)
);
CREATE TABLE IF NOT EXISTS symbol_vectors(
  symbol_id INTEGER,
  model TEXT,
  dims INTEGER,
  vec BLOB,
  PRIMARY KEY (symbol_id, model, dims)
);
CREATE TABLE IF NOT EXISTS clusters(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  component TEXT,
  label TEXT,
  candidate_types TEXT
);
CREATE TABLE IF NOT EXISTS cluster_members(
  cluster_id INTEGER,
  symbol_id INTEGER,
  PRIMARY KEY (cluster_id, symbol_id)
);
