from archaeon.review import render


def _card(type_, symbols, statement="does a thing"):
    return {"id": "X", "type": type_, "symbols": symbols,
            "statement": statement}


def test_state_transition_is_mermaid_state_with_two_nodes():
    spec = render.render_spec(_card("state_transition", ["Idle", "Active"]))
    assert spec["mode"] == "mermaid" and spec["kind"] == "state"
    assert "Idle --> Active" in spec["src"]
    assert spec["caption"] == "does a thing"


def test_interaction_sequence_is_mermaid_sequence():
    spec = render.render_spec(_card("interaction_sequence", ["Caller", "Callee"]))
    assert spec["mode"] == "mermaid" and spec["kind"] == "sequence"
    assert "sequenceDiagram" in spec["src"]
    assert "Caller" in spec["src"] and "Callee" in spec["src"]


def test_threshold_is_table_of_symbols_and_statement():
    spec = render.render_spec(_card("threshold", ["MAX_TEMP"], "temp <= 90"))
    assert spec["mode"] == "table"
    flat = [cell for row in spec["rows"] for cell in row]
    assert "MAX_TEMP" in flat and "temp <= 90" in flat


def test_unmapped_types_fall_back_to_prose():
    for t in ("conditional_rule", "invariant", "timing_budget", "mystery", ""):
        spec = render.render_spec(_card(t, ["A"], "the rule"))
        assert spec == {"mode": "prose", "text": "the rule"}


def test_mermaid_node_ids_are_sanitized():
    spec = render.render_spec(_card("state_transition", ["A::b()", "C d"]))
    # non-alnum chars replaced so Mermaid parses the node ids
    assert "::" not in spec["src"] and "()" not in spec["src"]


def test_non_string_symbols_are_coerced():
    spec = render.render_spec(_card("state_transition", [200, "Active"]))
    assert spec["mode"] == "mermaid" and spec["kind"] == "state"
    assert "200" in spec["src"] and "Active" in spec["src"]
