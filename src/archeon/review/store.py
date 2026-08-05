import sqlite3
from pathlib import Path

import yaml

from archeon.claims.schema import claim_version

_BUCKET = {
    "machine_verified": "verified",
    "expert_accepted": "verified",
    "contested": "contested",
    "rejected": "rejected",
    "recovered": "unrecovered",
}


def verification_bucket(status: str) -> str:
    return _BUCKET.get(status, "unrecovered")


def load_claim_files(claims_dir):
    """Read a claims directory into (good, broken).

    A malformed or non-mapping file becomes a broken-card marker instead of
    aborting the load, so one bad file never hides the rest of the store.
    """
    good, broken = [], []
    for p in sorted(Path(claims_dir).glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "id" not in data:
                raise ValueError("claim file is not a mapping with an id")
            data["_version"] = claim_version(p)
            good.append(data)
        except Exception as e:  # noqa: BLE001 - any parse failure is a broken card
            broken.append({"id": p.stem, "broken": True, "error": str(e),
                           "_version": ""})
    return good, broken


def claim_card(d: dict) -> dict:
    if d.get("broken"):
        return {"id": d["id"], "broken": True, "error": d.get("error", ""),
                "version": d.get("_version", "")}
    status = d.get("status", "recovered")
    return {
        "id": d["id"],
        "type": d.get("type", ""),
        "statement": d.get("statement", ""),
        "status": status,
        "bucket": verification_bucket(status),
        "confidence": d.get("confidence", 0.5),
        "feature": d.get("feature", ""),
        "symbols": list(d.get("symbols") or []),
        "evidence": [
            {"kind": e.get("kind", ""), "ref": e.get("ref", ""),
             "role": e.get("role", "primary"), "excerpt": e.get("excerpt", "")}
            for e in (d.get("evidence") or []) if isinstance(e, dict)],
        "counter_evidence": [str(c) for c in (d.get("counter_evidence") or [])],
        "version": d.get("_version", ""),
    }


_ZERO = ("verified", "contested", "unrecovered", "rejected", "broken")


def _component_of(d: dict) -> str:
    return d.get("feature") or "(unparsed)"


def _flat_cluster(d: dict) -> str:
    for e in d.get("evidence", []) or []:
        ref = e.get("ref") if isinstance(e, dict) else None
        if ref:
            return ref.split(":")[0]
    return "(unfiled)"


def _db_cluster_label(conn, symbols):
    """Cluster label for a claim's first symbol that maps into cluster_members.

    Claims name symbols as strings; Spec A keys clusters by symbols.id. Join by
    name. Returns None when the DB lacks the tables or nothing matches (both
    degrade to flat grouping).
    """
    for name in symbols:
        try:
            row = conn.execute(
                "SELECT c.id AS cid, c.label AS label FROM symbols s "
                "JOIN cluster_members m ON m.symbol_id = s.id "
                "JOIN clusters c ON c.id = m.cluster_id "
                "WHERE s.name = ? LIMIT 1", (name,)).fetchone()
        except sqlite3.OperationalError:
            return None
        if row:
            return row["label"] or f"cluster-{row['cid']}"
    return None


def _cluster_key(d: dict, conn):
    if conn is not None:
        label = _db_cluster_label(conn, d.get("symbols", []))
        if label is not None:
            return label, True
    return _flat_cluster(d), False


def _blank(key_name: str, key_val: str) -> dict:
    row = {key_name: key_val, "total": 0}
    row.update({b: 0 for b in _ZERO})
    return row


def _tally(row: dict, d: dict) -> None:
    row["total"] += 1
    if d.get("broken"):
        row["broken"] += 1
    else:
        row[verification_bucket(d.get("status", "recovered"))] += 1


def components(claims_dir) -> list:
    good, broken = load_claim_files(claims_dir)
    out: dict[str, dict] = {}
    for d in good + broken:
        key = _component_of(d)
        out.setdefault(key, _blank("component", key))
        _tally(out[key], d)
    return sorted(out.values(), key=lambda c: -c["total"])


def clusters(claims_dir, component, conn=None) -> list:
    good, broken = load_claim_files(claims_dir)
    out: dict[str, dict] = {}
    for d in good:
        if _component_of(d) != component:
            continue
        key, clustered = _cluster_key(d, conn)
        row = out.setdefault(key, _blank("cluster", key))
        row["clustered"] = clustered
        _tally(row, d)
    for d in broken:
        if _component_of(d) == component:
            row = out.setdefault("(unfiled)", _blank("cluster", "(unfiled)"))
            row.setdefault("clustered", False)
            _tally(row, d)
    return sorted(out.values(), key=lambda c: -c["total"])


def claims_in(claims_dir, *, component=None, cluster=None, conn=None) -> list:
    good, broken = load_claim_files(claims_dir)
    cards = []
    for d in good:
        if component is not None and _component_of(d) != component:
            continue
        if cluster is not None and _cluster_key(d, conn)[0] != cluster:
            continue
        cards.append(claim_card(d))
    for d in broken:
        if component not in (None, "(unparsed)"):
            continue
        if cluster not in (None, "(unfiled)"):
            continue
        cards.append(claim_card(d))
    return cards


_TERMINAL = {"expert_accepted", "rejected"}


def _impact(d: dict, conn) -> int:
    symbols = d.get("symbols", [])
    if conn is not None:
        try:
            total = 0
            for name in symbols:
                r = conn.execute(
                    "SELECT COUNT(*) AS c FROM symbol_edges e "
                    "JOIN symbols s ON s.id = e.dst_id "
                    "WHERE s.name = ?", (name,)).fetchone()
                total += r["c"]
            if total:
                return total
        except sqlite3.OperationalError:
            pass
    return max(len(symbols), 1)


def queue(claims_dir, conn=None) -> list:
    good, _ = load_claim_files(claims_dir)
    items = []
    for d in good:
        if d.get("status", "recovered") in _TERMINAL:
            continue
        card = claim_card(d)
        card["uncertainty"] = 1.0 - float(d.get("confidence", 0.5))
        card["impact"] = _impact(d, conn)
        card["score"] = card["impact"] * card["uncertainty"]
        items.append(card)
    return sorted(items, key=lambda c: (c["bucket"] != "contested", -c["score"]))
