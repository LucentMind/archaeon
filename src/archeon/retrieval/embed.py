import numpy as np
import requests

from archeon.codegraph.symsource import symbol_rows

CODE_PROMPT = "Represent this C/C++ code for retrieval:\n"


def embed_texts(texts: list[str], model: str, endpoint: str,
                dims: int) -> list[list[float]]:
    resp = requests.post(
        f"{endpoint.rstrip('/')}/api/embed",
        json={"model": model, "input": texts}, timeout=120)
    resp.raise_for_status()
    embeddings = resp.json().get("embeddings")
    if embeddings is None:
        raise requests.RequestException(
            "Ollama response missing 'embeddings'")
    return [e[:dims] for e in embeddings]


def _is_400(e: requests.HTTPError) -> bool:
    return e.response is not None and e.response.status_code == 400


def build_embedding_index(conn, repo_path, model: str, endpoint: str,
                          dims: int, batch: int = 16,
                          max_tokens: int | None = None) -> dict:
    rows = symbol_rows(conn, repo_path)
    done = {r["symbol_id"] for r in conn.execute(
        "SELECT symbol_id FROM symbol_vectors WHERE model=? AND dims=?",
        (model, dims))}
    todo = [r for r in rows if r["id"] not in done]
    if not todo:
        return {"embedded": 0, "skipped": len(rows),
                "unembeddable": 0, "ollama_available": True}

    def assemble(r) -> str:
        # ~4 chars/token: cap the input so one huge symbol (vendored code,
        # generated asset blobs) can't blow past what the model accepts. The
        # head carries the retrieval signal — signature + opening lines.
        text = CODE_PROMPT + r["signature"] + "\n" + r["text"]
        if max_tokens is not None:
            text = text[:max_tokens * 4]
        return text

    def store(pairs) -> None:
        if pairs:
            conn.executemany(
                "INSERT OR REPLACE INTO symbol_vectors(symbol_id, model, "
                "dims, vec) VALUES (?, ?, ?, ?)",
                [(r["id"], model, dims,
                  np.asarray(v, dtype=np.float32).tobytes())
                 for r, v in pairs])

    embedded = 0
    unembeddable = 0
    try:
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            try:
                vecs = embed_texts([assemble(r) for r in chunk],
                                   model, endpoint, dims)
                pairs = list(zip(chunk, vecs))
            except requests.HTTPError as e:
                if not _is_400(e):
                    raise
                # A 400 is a rejected *request*, not a dead server. Isolate the
                # offending symbol(s) by retrying one at a time and skipping
                # only the ones the model won't accept.
                pairs = []
                for r in chunk:
                    try:
                        v = embed_texts([assemble(r)], model, endpoint, dims)
                    except requests.HTTPError as inner:
                        if not _is_400(inner):
                            raise
                        unembeddable += 1
                        continue
                    pairs.append((r, v[0]))
            store(pairs)
            embedded += len(pairs)
        conn.commit()
    except requests.RequestException as e:
        conn.commit()
        return {"embedded": embedded, "skipped": len(done),
                "unembeddable": unembeddable,
                "ollama_available": False, "error": str(e)}
    return {"embedded": embedded, "skipped": len(done),
            "unembeddable": unembeddable, "ollama_available": True}


def load_vectors(conn, model: str, dims: int) -> dict[int, np.ndarray]:
    out: dict[int, np.ndarray] = {}
    for r in conn.execute(
            "SELECT symbol_id, vec FROM symbol_vectors "
            "WHERE model=? AND dims=?", (model, dims)):
        out[r["symbol_id"]] = np.frombuffer(r["vec"], dtype=np.float32)
    return out


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))
