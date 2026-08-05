def _node(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in str(name)).strip("_")
    return (cleaned[:40] or "n")


def _state_src(card: dict) -> str:
    syms = card.get("symbols", [])
    lines = ["stateDiagram-v2"]
    if len(syms) >= 2:
        lines.append(f"    {_node(syms[0])} --> {_node(syms[1])}")
    elif syms:
        lines.append(f"    [*] --> {_node(syms[0])}")
    else:
        lines.append("    [*] --> Unknown")
    return "\n".join(lines)


def _sequence_src(card: dict) -> str:
    parts = [_node(s) for s in (card.get("symbols") or ["actor"])[:6]]
    lines = ["sequenceDiagram"]
    lines += [f"    participant {p}" for p in parts]
    if len(parts) == 1:
        lines.append(f"    {parts[0]}->>{parts[0]}: self")
    else:
        lines += [f"    {a}->>{b}: call" for a, b in zip(parts, parts[1:])]
    return "\n".join(lines)


def _threshold_rows(card: dict) -> list:
    rows = [["symbol", s] for s in card.get("symbols", [])]
    rows.append(["statement", card.get("statement", "")])
    return rows


def render_spec(card: dict) -> dict:
    t = card.get("type", "")
    if t == "state_transition":
        return {"mode": "mermaid", "kind": "state", "src": _state_src(card),
                "caption": card.get("statement", "")}
    if t == "interaction_sequence":
        return {"mode": "mermaid", "kind": "sequence", "src": _sequence_src(card),
                "caption": card.get("statement", "")}
    if t == "threshold":
        return {"mode": "table", "columns": ["field", "value"],
                "rows": _threshold_rows(card)}
    return {"mode": "prose", "text": card.get("statement", "")}
