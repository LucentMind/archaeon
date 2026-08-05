from archeon.claims.claim_eval import evaluate_claims, load_labels
from archeon.claims.schema import Claim


def test_load_labels(tmp_path):
    p = tmp_path / "labels.csv"
    p.write_text("claim_id,correct\nCLM-0001,yes\nCLM-0002,no\n")
    labels = load_labels(p)
    assert labels == {"CLM-0001": True, "CLM-0002": False}


def test_evaluate_precision_per_layer():
    claims = [
        Claim(id="CLM-0001", type="threshold", statement="a", layer="what"),
        Claim(id="CLM-0002", type="threshold", statement="b", layer="what"),
        Claim(id="CLM-0003", type="conditional_rule", statement="c",
              layer="why"),
        Claim(id="CLM-0004", type="threshold", statement="d", layer="what"),
    ]
    # CLM-0004 unlabeled -> excluded from the metric
    labels = {"CLM-0001": True, "CLM-0002": False, "CLM-0003": True}
    result = evaluate_claims(claims, labels)
    assert result["what"]["n"] == 2 and result["what"]["correct"] == 1
    assert result["what"]["precision"] == 0.5
    assert result["why"]["precision"] == 1.0


def test_evaluate_precision_verified_only():
    # A synthesis batch with a real defect the verifier catches (contested)
    # should score differently pre- vs. post-verification.
    claims = [
        Claim(id="CLM-0001", type="threshold", statement="a", layer="what",
              status="machine_verified"),
        Claim(id="CLM-0002", type="threshold", statement="b", layer="what",
              status="machine_verified"),
        Claim(id="CLM-0003", type="threshold", statement="c", layer="what",
              status="contested"),
    ]
    # the contested claim is a real defect (expert says incorrect)
    labels = {"CLM-0001": True, "CLM-0002": True, "CLM-0003": False}
    result = evaluate_claims(claims, labels)
    assert result["what"]["n"] == 3 and result["what"]["correct"] == 2
    assert result["what"]["precision"] == 2 / 3
    assert result["what"]["verified_n"] == 2
    assert result["what"]["verified_correct"] == 2
    assert result["what"]["verified_precision"] == 1.0


def test_evaluate_precision_verified_zero_when_none_verified():
    claims = [Claim(id="CLM-0001", type="threshold", statement="a",
                    layer="what", status="contested")]
    result = evaluate_claims(claims, {"CLM-0001": True})
    assert result["what"]["verified_n"] == 0
    assert result["what"]["verified_precision"] == 0.0


def _why(cid, corroboration, status="machine_verified"):
    return Claim(id=cid, type="rationale", statement="s", layer="why",
                 status=status, corroboration=corroboration)


def test_corroborated_precision_excludes_code_inferred():
    claims = [_why("WHY-0001", "corroborated"),
              _why("WHY-0002", "corroborated"),
              _why("WHY-0003", "code_inferred")]
    labels = {"WHY-0001": True, "WHY-0002": False, "WHY-0003": True}
    s = evaluate_claims(claims, labels)["why"]
    assert s["n"] == 3                       # all labeled claims
    assert s["corroborated_n"] == 2          # code-inferred excluded
    assert s["corroborated_correct"] == 1
    assert s["corroborated_precision"] == 0.5
    # Sanity check the two ways an "excluded from denominator only" bug
    # could show up: neither matches the correct 0.5.
    assert s["corroborated_precision"] != 1 / 3   # WHY-0003 in denom, not num
    assert s["corroborated_precision"] != 2 / 3   # WHY-0003 in both


def test_corroborated_counts_are_independent_of_status():
    # A contested-but-corroborated claim still counts in the denominator.
    claims = [_why("WHY-0001", "corroborated", status="contested"),
              _why("WHY-0002", "corroborated")]
    labels = {"WHY-0001": True, "WHY-0002": True}
    s = evaluate_claims(claims, labels)["why"]
    assert s["n"] == 2
    assert s["correct"] == 2
    assert s["corroborated_n"] == 2
    assert s["corroborated_correct"] == 2
    assert s["corroborated_precision"] == 1.0
    assert s["verified_n"] == 1               # only one reached verified
    assert s["verified_correct"] == 1
    assert s["verified_precision"] == 1.0


def test_corroborated_precision_is_zero_when_none_corroborated():
    s = evaluate_claims([_why("WHY-0001", "code_inferred")],
                        {"WHY-0001": True})["why"]
    assert s["corroborated_n"] == 0
    assert s["corroborated_correct"] == 0
    assert s["corroborated_precision"] == 0.0
    # zero-denominator guard must not raise ZeroDivisionError
    assert isinstance(s["corroborated_precision"], float)


def test_what_layer_reports_zero_corroborated():
    what = Claim(id="CLM-0001", type="threshold", statement="s",
                 layer="what", status="machine_verified")
    s = evaluate_claims([what], {"CLM-0001": True})["what"]
    assert s["corroborated_n"] == 0
    assert s["corroborated_correct"] == 0
    assert s["corroborated_precision"] == 0.0
    # pre-existing keys keep their previous meaning, unaffected by the
    # new corroborated_* fields
    assert s["n"] == 1
    assert s["correct"] == 1
    assert s["precision"] == 1.0
    assert s["verified_n"] == 1
    assert s["verified_correct"] == 1
    assert s["verified_precision"] == 1.0
