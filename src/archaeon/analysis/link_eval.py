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
