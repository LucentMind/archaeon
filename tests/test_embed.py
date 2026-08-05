import numpy as np
import pytest
import requests

from archaeon import retrieval
from archaeon.retrieval import embed as embed_mod
from archaeon.db import connect


def _insert(conn, name, path, line, end_line):
    conn.execute(
        "INSERT INTO symbols(name, kind, path, line, end_line, signature, "
        "source) VALUES (?, 'function', ?, ?, ?, 'sig', 'tree-sitter')",
        (name, path, line, end_line))


def _fixture(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.c").write_text(
        "int f(void){return 1;}\nint g(void){return 2;}\n")
    conn = connect(tmp_path / "e.db")
    _insert(conn, "f", "src/a.c", 1, 1)
    _insert(conn, "g", "src/a.c", 2, 2)
    return conn


def test_build_index_embeds_and_is_idempotent(tmp_path, monkeypatch):
    conn = _fixture(tmp_path)
    calls = {"n": 0}

    def fake_embed(texts, model, endpoint, dims):
        calls["n"] += 1
        return [[float(len(t))] * dims for t in texts]

    monkeypatch.setattr(embed_mod, "embed_texts", fake_embed)

    r1 = embed_mod.build_embedding_index(conn, tmp_path, "m", "http://x", 4)
    assert r1["embedded"] == 2 and r1["ollama_available"] is True
    first_calls = calls["n"]

    # rerun under the same (model, dims): everything skipped, no new calls
    r2 = embed_mod.build_embedding_index(conn, tmp_path, "m", "http://x", 4)
    assert r2["embedded"] == 0
    assert calls["n"] == first_calls

    # a different model triggers re-embedding
    r3 = embed_mod.build_embedding_index(conn, tmp_path, "m2", "http://x", 4)
    assert r3["embedded"] == 2
    assert calls["n"] > first_calls


def test_build_index_degrades_when_ollama_unreachable(tmp_path, monkeypatch):
    conn = _fixture(tmp_path)

    def boom(texts, model, endpoint, dims):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr(embed_mod, "embed_texts", boom)
    r = embed_mod.build_embedding_index(conn, tmp_path, "m", "http://x", 4)
    assert r["ollama_available"] is False
    assert "connection refused" in r["error"]
    n = conn.execute("SELECT COUNT(*) AS c FROM symbol_vectors").fetchone()["c"]
    assert n == 0


def test_build_index_commits_partial_progress_on_mid_run_failure(tmp_path, monkeypatch):
    conn = _fixture(tmp_path)
    calls = {"n": 0}

    def fake_embed(texts, model, endpoint, dims):
        calls["n"] += 1
        if calls["n"] == 1:
            # First call succeeds
            return [[1.0, 0.0, 0.0, 0.0]]
        else:
            # Second call fails
            raise requests.RequestException("mid-run failure")

    monkeypatch.setattr(embed_mod, "embed_texts", fake_embed)
    r = embed_mod.build_embedding_index(conn, tmp_path, "m", "http://x", 4, batch=1)

    assert r["ollama_available"] is False
    assert r["embedded"] == 1
    n = conn.execute("SELECT COUNT(*) AS c FROM symbol_vectors WHERE model='m' AND dims=4").fetchone()["c"]
    assert n == 1


def test_oversized_text_is_truncated_before_send(tmp_path, monkeypatch):
    # A symbol far larger than the model can handle: without a cap it would be
    # sent whole and rejected. With max_tokens set, the assembled input handed
    # to embed_texts must be capped (head kept — enough signal for retrieval).
    (tmp_path / "src").mkdir()
    body = "int big(void){\n" + "  step();\n" * 5000 + "}\n"
    (tmp_path / "src" / "big.c").write_text(body)
    conn = connect(tmp_path / "e.db")
    _insert(conn, "big", "src/big.c", 1, body.count("\n"))

    seen = {"max_chars": 0}

    def fake_embed(texts, model, endpoint, dims):
        seen["max_chars"] = max(len(t) for t in texts)
        return [[1.0] * dims for _ in texts]

    monkeypatch.setattr(embed_mod, "embed_texts", fake_embed)
    r = embed_mod.build_embedding_index(
        conn, tmp_path, "m", "http://x", 4, max_tokens=2000)

    assert r["embedded"] == 1
    assert r["ollama_available"] is True
    # ~4 chars/token heuristic → assembled input stays within the token budget
    assert seen["max_chars"] <= 2000 * 4


def test_http_400_skips_only_offending_symbol_without_degrading(
        tmp_path, monkeypatch):
    # A per-request 400 (input the model rejects) must skip that one symbol and
    # keep going — NOT be mistaken for "ollama down" and abort the whole index.
    conn = _fixture(tmp_path)  # f (returns 1), g (returns 2)

    def fake_embed(texts, model, endpoint, dims):
        if any("return 2" in t for t in texts):
            resp = requests.Response()
            resp.status_code = 400
            raise requests.HTTPError("input rejected", response=resp)
        return [[1.0] * dims for _ in texts]

    monkeypatch.setattr(embed_mod, "embed_texts", fake_embed)
    r = embed_mod.build_embedding_index(
        conn, tmp_path, "m", "http://x", 4, batch=2)

    assert r["ollama_available"] is True   # not degraded
    assert r["embedded"] == 1              # f embedded
    assert r["unembeddable"] == 1          # g skipped
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM symbol_vectors").fetchone()["c"]
    assert n == 1


def test_load_vectors_roundtrip_and_cosine(tmp_path, monkeypatch):
    conn = _fixture(tmp_path)

    def fake_embed(texts, model, endpoint, dims):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(embed_mod, "embed_texts", fake_embed)
    embed_mod.build_embedding_index(conn, tmp_path, "m", "http://x", 4)
    vecs = embed_mod.load_vectors(conn, "m", 4)
    assert len(vecs) == 2
    a = next(iter(vecs.values()))
    assert embed_mod.cosine(a, a) == pytest.approx(1.0)
    assert embed_mod.cosine(a, np.zeros(4, dtype=np.float32)) == 0.0
