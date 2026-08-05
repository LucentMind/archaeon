import json
import re
import subprocess
from pathlib import Path

import pytest
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


from archeon.claims.pin import pin_claims  # noqa: E402
from archeon.claims.schema import Claim, Evidence, save_claims  # noqa: E402


def _setup_repo_with_multiline_file(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "src" / "f.c").write_text(
        "int f(void) {\n  return 42;\n}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
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
    return repo, config


def test_check_staleness_flags_only_edited_claim(tmp_path):
    repo, config = _setup_repo_with_multiline_file(tmp_path)
    claims_dir = tmp_path / "claims"
    good = Claim(id="CLM-0001", type="threshold", statement="signature",
                 evidence=[Evidence(kind="code", ref="src/f.c:1")])
    bad = Claim(id="CLM-0002", type="threshold", statement="return value",
                evidence=[Evidence(kind="code", ref="src/f.c:2")])
    pin_claims([good, bad], repo)
    save_claims([good, bad], claims_dir)
    # Edit exactly the region CLM-0002 cites.
    (repo / "src" / "f.c").write_text("int f(void) {\n  return 7;\n}\n")

    runner = CliRunner()
    r = runner.invoke(main, ["check-staleness", "--config", str(config),
                             "--claims", str(claims_dir)])
    assert r.exit_code == 0, r.output
    assert "STALE" in r.output and "CLM-0002" in r.output
    assert "CLM-0001" not in r.output  # untouched claim not flagged


def test_check_staleness_reports_unpinnable(tmp_path):
    repo, config = _setup_repo_with_multiline_file(tmp_path)
    claims_dir = tmp_path / "claims"
    c = Claim(id="CLM-0001", type="threshold", statement="ghost",
              evidence=[Evidence(kind="code", ref="src/ghost.c:1")])
    pin_claims([c], repo)
    save_claims([c], claims_dir)

    runner = CliRunner()
    r = runner.invoke(main, ["check-staleness", "--config", str(config),
                             "--claims", str(claims_dir)])
    assert r.exit_code == 0, r.output
    assert "UNPINNABLE" in r.output and "CLM-0001" in r.output


def test_claims_eval_prints_corroborated_gate_and_no_blended_total(tmp_path):
    claims_dir = tmp_path / "claims"
    claims = [
        Claim(id="WHY-0001", type="rationale", statement="s", layer="why",
              status="machine_verified", corroboration="corroborated"),
        Claim(id="WHY-0002", type="rationale", statement="s", layer="why",
              status="machine_verified", corroboration="code_inferred"),
        Claim(id="CLM-0001", type="threshold", statement="s", layer="what",
              status="machine_verified"),
    ]
    save_claims(claims, claims_dir)
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "claim_id,correct\nWHY-0001,yes\nWHY-0002,yes\nCLM-0001,yes\n")

    runner = CliRunner()
    r = runner.invoke(main, ["claims-eval", "--claims", str(claims_dir),
                             "--labels", str(labels)])
    assert r.exit_code == 0, r.output
    # The why-layer corroborated gate appears, scoped to corroborated claims
    # only (WHY-0002 is code_inferred and must not inflate the count).
    assert ("why-layer precision (corroborated only): 1.000 (1/1) "
            "[gate 0.80: PASS]") in r.output
    # The what-layer has no corroborated claims at all -> no such line for it.
    assert "what-layer precision (corroborated only)" not in r.output
    # The design bans a blended what+why figure: no combined/overall line,
    # and exactly one corroborated-gate line total (why-layer only).
    assert r.output.count("(corroborated only)") == 1
    lowered = r.output.lower()
    assert "overall" not in lowered and "combined" not in lowered
    assert "blended" not in lowered


def test_embed_degrades_when_ollama_down(tmp_path, monkeypatch):
    config = _setup(tmp_path)
    runner = CliRunner()
    assert runner.invoke(main, ["scan", "--config", str(config)]).exit_code == 0

    import archeon.retrieval.embed as embed_mod
    import requests as _rq

    def boom(texts, model, endpoint, dims):
        raise _rq.RequestException("refused")
    monkeypatch.setattr(embed_mod, "embed_texts", boom)

    r = runner.invoke(main, ["embed", "--config", str(config)])
    assert r.exit_code == 0, r.output
    assert "ollama" in r.output.lower()  # degradation is reported, not a crash


def test_cluster_runs_graph_only_without_ollama(tmp_path, monkeypatch):
    config = _setup(tmp_path)
    runner = CliRunner()
    assert runner.invoke(main, ["scan", "--config", str(config)]).exit_code == 0

    import archeon.retrieval.embed as embed_mod
    import requests as _rq
    monkeypatch.setattr(embed_mod, "embed_texts",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _rq.RequestException("refused")))
    # cheap-model labelling must not hit the network in the test
    import archeon.retrieval.cluster as cluster_mod
    monkeypatch.setattr(cluster_mod, "label_cluster", lambda rows, ask: ("", ""))

    r = runner.invoke(main, ["cluster", "--config", str(config)])
    assert r.exit_code == 0, r.output
    assert "clusters:" in r.output


def test_synthesize_feature_without_clusters_uses_fallback(tmp_path, monkeypatch):
    config = _setup(tmp_path)
    runner = CliRunner()
    assert runner.invoke(main, ["scan", "--config", str(config)]).exit_code == 0

    import archeon.claims.recover as recover_mod
    from archeon.claims.schema import Claim

    captured = {}

    def fake_synth(feature, bundle, ask):
        captured["bundle"] = bundle
        return [Claim(id="CLM-0001", type="threshold", statement="s",
                      feature=feature, layer="what", status="recovered")]

    def fake_verify(claims, bundle, ask):
        for c in claims:
            c.status = "machine_verified"

    # Patch at the source module; cli imports these names lazily at call time.
    monkeypatch.setattr(recover_mod, "synthesize_claims", fake_synth)
    monkeypatch.setattr(recover_mod, "verify_claims", fake_verify)

    out = tmp_path / "claims_out"
    r = runner.invoke(main, ["synthesize", "--config", str(config),
                             "--feature", "src/", "--out", str(out)])
    assert r.exit_code == 0, r.output
    # With no clusters, the --feature fallback must still run synthesis:
    assert "claims: 1" in r.output
    assert captured.get("bundle")  # a non-empty ad-hoc bundle was built


def test_synthesize_requires_exactly_one_target(tmp_path):
    config = _setup(tmp_path)
    runner = CliRunner()
    r = runner.invoke(main, ["synthesize", "--config", str(config)])
    assert r.exit_code != 0
    assert "exactly one" in r.output  # zero targets is a usage error message


def test_synthesize_writes_run_cost_json(tmp_path, monkeypatch):
    config = _setup(tmp_path)
    runner = CliRunner()
    scan_r = runner.invoke(main, ["scan", "--config", str(config)])
    assert scan_r.exit_code == 0

    import archeon.claims.recover as recover_mod
    from archeon.claims.schema import Claim

    def fake_synth(feature, bundle, ask):
        return [Claim(id="CLM-0001", type="threshold", statement="s",
                      feature=feature, layer="what", status="recovered")]

    def fake_verify(claims, bundle, ask):
        for c in claims:
            c.status = "machine_verified"

    monkeypatch.setattr(recover_mod, "synthesize_claims", fake_synth)
    monkeypatch.setattr(recover_mod, "verify_claims", fake_verify)

    out = tmp_path / "claims_out"
    r = runner.invoke(main, ["synthesize", "--config", str(config),
                             "--feature", "src/", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "API-equivalent" in r.output
    assert "not billed" in r.output

    data = json.loads((out / "run_cost.json").read_text())
    assert data["command"] == "synthesize"
    assert data["billed"] is False
    assert "subscription auth" in data["note"]
    assert set(data) >= {"generated_at", "total_usd", "calls", "failed_calls",
                         "by_stage", "by_model"}
    # The stubs never reach the LLM, so this is a valid zero record.
    assert data["calls"] == 0
    assert data["failed_calls"] == 0
    assert data["total_usd"] == 0.0


def test_synthesize_reports_unknown_route_when_var_set(tmp_path, monkeypatch):
    """Companion to test_synthesize_writes_run_cost_json (which pins the
    default subscription branch): here a route var is set to prove the
    other side of the tri-state actually reaches both the JSON file and
    the printed line for a real CLI invocation, not just CostMeter in
    isolation (tests/test_cost.py already covers that in isolation).
    `tests/conftest.py`'s autouse fixture clears the three route vars
    before every test; setting one here with monkeypatch happens after
    that fixture ran, so it sticks for this test only.
    """
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://x")

    config = _setup(tmp_path)
    runner = CliRunner()
    scan_r = runner.invoke(main, ["scan", "--config", str(config)])
    assert scan_r.exit_code == 0, scan_r.output

    import archeon.claims.recover as recover_mod
    from archeon.claims.schema import Claim

    def fake_synth(feature, bundle, ask):
        return [Claim(id="CLM-0001", type="threshold", statement="s",
                      feature=feature, layer="what", status="recovered")]

    def fake_verify(claims, bundle, ask):
        for c in claims:
            c.status = "machine_verified"

    monkeypatch.setattr(recover_mod, "synthesize_claims", fake_synth)
    monkeypatch.setattr(recover_mod, "verify_claims", fake_verify)

    out = tmp_path / "claims_out"
    r = runner.invoke(main, ["synthesize", "--config", str(config),
                             "--feature", "src/", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "not billed" not in r.output
    assert "unknown" in r.output

    data = json.loads((out / "run_cost.json").read_text())
    assert data["billed"] is None
    assert "not billed" not in data["note"]


def test_cluster_prints_cost_summary(tmp_path, monkeypatch):
    config = _setup(tmp_path)
    runner = CliRunner()
    scan_r = runner.invoke(main, ["scan", "--config", str(config)])
    assert scan_r.exit_code == 0

    import requests as _rq

    import archeon.retrieval.embed as embed_mod
    monkeypatch.setattr(embed_mod, "embed_texts",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _rq.RequestException("refused")))
    import archeon.retrieval.cluster as cluster_mod
    monkeypatch.setattr(cluster_mod, "label_cluster",
                        lambda rows, ask: ("", ""))

    # cluster has no output directory, so it is print-only. Pin that inside
    # an isolated cwd: CliRunner does NOT sandbox the filesystem by default,
    # so a stray relative write would land in the repo's own cwd and never
    # in tmp_path -- which would make a `not (tmp_path /
    # "run_cost.json").exists()` assertion vacuously true.
    with runner.isolated_filesystem() as fs:
        r = runner.invoke(main, ["cluster", "--config", str(config)])
        assert r.exit_code == 0, r.output
        stray = [str(p) for p in Path(fs).rglob("*.json")]
    assert stray == []
    assert "clusters:" in r.output
    assert "API-equivalent" in r.output


def _fake_query_sequence(result_texts, usd=0.05):
    """Build a fake ``archeon.llm.query`` that yields one terminal
    ResultMessage (subtype "success") per call, carrying the SDK's real
    cost fields, so the real ``AgentClassifier._ask`` runs end to end and
    therefore actually drives ``meter.record`` — unlike the ``label_fn`` /
    ``synthesize_claims`` / ``verify_claims`` stubs above, which are handed
    the ``ask`` callable but never invoke it. Mirrors the
    ``_fake_query_factory`` / ``_FakeCostedResult`` pattern in
    tests/test_llm.py. One call is consumed from ``result_texts`` per
    query(), in order.

    ``usd`` may be a single float applied to every call, or a sequence with
    one value per call (same length/order as ``result_texts``) — used to
    make calls dollar-distinguishable so a stage-literal swap is
    detectable (a swap between two calls of equal cost would otherwise be
    symmetric and produce byte-identical output).
    """
    calls = {"i": 0}

    class _FakeCostedResult:
        subtype = "success"

        def __init__(self, text, usd_value):
            self.result = text
            self.total_cost_usd = usd_value
            self.num_turns = 1
            self.usage = {"input_tokens": 100, "output_tokens": 50,
                         "cache_read_input_tokens": 0,
                         "cache_creation_input_tokens": 0}

    async def fake_query(prompt, options):
        i = calls["i"]
        calls["i"] += 1
        usd_value = usd[i] if isinstance(usd, (list, tuple)) else usd
        yield _FakeCostedResult(result_texts[i], usd_value)

    return fake_query


def test_synthesize_meter_records_both_stages_via_real_classifier(
        tmp_path, monkeypatch):
    """Unlike test_synthesize_writes_run_cost_json (which stubs synthesize_
    claims/verify_claims so `ask` is never called), this drives the real
    synthesize_claims/verify_claims through the real AgentClassifier._ask,
    with only the SDK's `query()` faked. That is the only way to prove the
    ONE meter built before the `for label, cid in targets:` loop is actually
    shared by both the "synthesize" and "verify" classifiers built inside
    it — the central wiring constraint of this task.
    """
    pytest.importorskip("claude_agent_sdk")
    import archeon.llm as llm_mod

    config = _setup(tmp_path)
    runner = CliRunner()
    scan_r = runner.invoke(main, ["scan", "--config", str(config)])
    assert scan_r.exit_code == 0

    synth_json = (
        '[{"type": "threshold", "statement": "f returns 1", '
        '"symbols": ["f"], '
        '"evidence": [{"ref": "src/a.c:1", "excerpt": "return 1;"}]}]'
    )
    verify_json = '{"supported": true, "confidence": 0.9, "counter": ""}'
    # Distinct per-call dollar amounts: a stage-literal swap between the
    # synthesize/verify AgentClassifier constructions would otherwise be
    # symmetric (1 call each, same $) and produce byte-identical JSON.
    synth_usd, verify_usd = 0.07, 0.11
    monkeypatch.setattr(
        llm_mod, "query",
        _fake_query_sequence([synth_json, verify_json],
                             usd=[synth_usd, verify_usd]))

    out = tmp_path / "claims_out"
    r = runner.invoke(main, ["synthesize", "--config", str(config),
                             "--feature", "src/", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "claims: 1" in r.output
    assert "machine_verified: 1" in r.output

    data = json.loads((out / "run_cost.json").read_text())
    # One synth call + one verify call, both landing in the SAME meter.
    assert data["calls"] == 2
    assert data["total_usd"] > 0.0
    assert set(data["by_stage"]) == {"synthesize", "verify"}
    assert data["by_stage"]["synthesize"]["calls"] == 1
    assert data["by_stage"]["verify"]["calls"] == 1
    # The dollar amount must land under the CORRECT stage key: a swap of
    # the two stage= literals would still show 1 call each but move the
    # money to the wrong bucket.
    assert data["by_stage"]["synthesize"]["usd"] == pytest.approx(synth_usd)
    assert data["by_stage"]["verify"]["usd"] == pytest.approx(verify_usd)


def test_synthesize_all_clusters_meter_accumulates_across_targets(
        tmp_path, monkeypatch):
    """Finding 1 coverage: test_synthesize_meter_records_both_stages_via_
    real_classifier only drives a single-element `targets` list (one
    --feature), where the loop body runs exactly once — for one iteration,
    `meter = CostMeter()` before vs. as the first line INSIDE the `for
    label, cid in targets:` loop is behaviorally identical (Python's `for`
    has no block scope). The actual thing "build it once before the loop"
    protects against is multiple iterations. This drives --all-clusters
    with two clusters so the loop body runs twice, and asserts the meter
    ACCUMULATES across iterations rather than resetting each time.
    """
    import sqlite3

    pytest.importorskip("claude_agent_sdk")
    import archeon.llm as llm_mod

    config = _setup(tmp_path)
    runner = CliRunner()
    scan_r = runner.invoke(main, ["scan", "--config", str(config)])
    assert scan_r.exit_code == 0, scan_r.output

    # Two clusters, both bundling the single symbol (`f` in src/a.c) that
    # `scan` found in the fixture repo. bundle_for_cluster only needs a
    # cluster_members row whose symbol_id resolves via symbol_rows(); it
    # never enforces that a symbol belongs to exactly one cluster, and
    # pack_symbols always emits at least the first (highest-ranked)
    # symbol, so this is enough to get a non-empty bundle for BOTH
    # clusters without contriving a second real symbol in the fixture.
    db_path = tmp_path / "e.db"
    raw_conn = sqlite3.connect(str(db_path))
    symbol_id = raw_conn.execute(
        "SELECT id FROM symbols LIMIT 1").fetchone()[0]
    raw_conn.execute(
        "INSERT INTO clusters (id, component, label) "
        "VALUES (1, 'demo', 'Cluster A')")
    raw_conn.execute(
        "INSERT INTO clusters (id, component, label) "
        "VALUES (2, 'demo', 'Cluster B')")
    raw_conn.execute(
        "INSERT INTO cluster_members (cluster_id, symbol_id) "
        "VALUES (1, ?)", (symbol_id,))
    raw_conn.execute(
        "INSERT INTO cluster_members (cluster_id, symbol_id) "
        "VALUES (2, ?)", (symbol_id,))
    raw_conn.commit()
    raw_conn.close()

    def synth_for(n):
        return ('[{"type": "threshold", "statement": "claim %d", '
               '"symbols": ["f"], "evidence": [{"ref": "src/a.c:1", '
               '"excerpt": "return 1;"}]}]') % n

    verify_json = '{"supported": true, "confidence": 0.9, "counter": ""}'
    # Call order is synth-then-verify per target; with 2 clusters that is
    # [synth1, verify1, synth2, verify2]. Distinct per-call dollar amounts
    # let the accumulation assertion below distinguish "summed all 4"
    # from "only the last iteration's fresh meter survived".
    result_texts = [synth_for(1), verify_json, synth_for(2), verify_json]
    call_usd = [0.01, 0.02, 0.03, 0.04]
    monkeypatch.setattr(
        llm_mod, "query",
        _fake_query_sequence(result_texts, usd=call_usd))

    out = tmp_path / "claims_out"
    r = runner.invoke(main, ["synthesize", "--config", str(config),
                             "--all-clusters", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "claims: 2" in r.output

    data = json.loads((out / "run_cost.json").read_text())
    # If `meter = CostMeter()` were the first line INSIDE the loop instead
    # of built once before it, each iteration would get a fresh meter and
    # only the LAST iteration's (1 synth + 1 verify = 2 calls, $0.03 +
    # $0.04) would survive to be summarized/written — not the full
    # 4-call, $0.10 accumulation asserted here.
    assert data["calls"] == 4
    assert data["total_usd"] == pytest.approx(sum(call_usd))
    assert data["by_stage"]["synthesize"]["calls"] == 2
    assert data["by_stage"]["verify"]["calls"] == 2
    assert data["by_stage"]["synthesize"]["usd"] == pytest.approx(0.04)
    assert data["by_stage"]["verify"]["usd"] == pytest.approx(0.06)


def test_synthesize_records_an_errored_verify_call(tmp_path, monkeypatch):
    """A terminal error result (overload, turn exhaustion) spends quota but
    carries no text, and — mirroring the real SDK — the fake `query()`
    raises right after yielding it instead of just ending the generator
    (see claude_agent_sdk._internal.query.py: a message with `is_error`
    makes the CLI process exit non-zero, and query() surfaces that as an
    exception). That means `AgentClassifier.ask` here does not return
    `""`; it propagates the exception, so this exercises verify_claims's
    real `except Exception` branch (src/archeon/claims/recover.py:109),
    not its separate unparsable-JSON branch. verify_claims catches it and
    degrades the claim to `contested`, so the run still exits 0 — which is
    exactly why the cost report has to show that call and flag it as
    failed, or a long --all-clusters run hides its own burn. This also
    proves the recorded cost survives the raise: `record` runs in `_ask`
    before the exception reaches `ask`'s caller.
    """
    pytest.importorskip("claude_agent_sdk")
    import archeon.llm as llm_mod

    config = _setup(tmp_path)
    runner = CliRunner()
    scan_r = runner.invoke(main, ["scan", "--config", str(config)])
    assert scan_r.exit_code == 0, scan_r.output

    synth_json = (
        '[{"type": "threshold", "statement": "f returns 1", '
        '"symbols": ["f"], '
        '"evidence": [{"ref": "src/a.c:1", "excerpt": "return 1;"}]}]'
    )

    class _Result:
        """Terminal ResultMessage; error subtypes have no `.result`."""

        def __init__(self, subtype, usd, text=None):
            self.subtype = subtype
            self.total_cost_usd = usd
            self.num_turns = 1
            self.usage = {"input_tokens": 100, "output_tokens": 10,
                          "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0}
            if text is not None:
                self.result = text

    messages = [_Result("success", 0.07, synth_json),
                _Result("error_during_execution", 0.13)]
    seen = {"i": 0}

    async def fake_query(prompt, options):
        i = seen["i"]
        seen["i"] += 1
        yield messages[i]
        if messages[i].subtype != "success":
            # Real SDK behavior: the CLI exits non-zero on an is_error
            # result and query() raises rather than ending cleanly.
            raise RuntimeError("simulated CLI error exit")

    monkeypatch.setattr(llm_mod, "query", fake_query)

    out = tmp_path / "claims_out"
    r = runner.invoke(main, ["synthesize", "--config", str(config),
                             "--feature", "src/", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "claims: 1" in r.output
    assert "machine_verified: 0" in r.output  # the verify never answered
    assert "1 of 2 calls failed" in r.output  # ...but it still cost money

    data = json.loads((out / "run_cost.json").read_text())
    assert data["calls"] == 2
    assert data["failed_calls"] == 1
    assert data["total_usd"] == pytest.approx(0.20)
    assert data["by_stage"]["verify"]["calls"] == 1
    assert data["by_stage"]["verify"]["usd"] == pytest.approx(0.13)


def test_cluster_meter_records_label_call_via_real_classifier(
        tmp_path, monkeypatch):
    """Companion to test_cluster_prints_cost_summary: that test stubs
    label_cluster so `ask` is never called and the zero-record summary is
    trivially satisfied. Here label_cluster is left real and only the SDK's
    `query()` is faked, so the "cluster-label" classifier the CLI builds
    actually records into the meter. Empirically, this tiny single-symbol
    fixture DOES yield exactly one cluster with one member, so label_fn is
    invoked once (verified by hand before writing this test).
    """
    pytest.importorskip("claude_agent_sdk")
    import archeon.llm as llm_mod

    config = _setup(tmp_path)
    runner = CliRunner()
    scan_r = runner.invoke(main, ["scan", "--config", str(config)])
    assert scan_r.exit_code == 0

    import archeon.retrieval.embed as embed_mod
    import requests as _rq
    monkeypatch.setattr(embed_mod, "embed_texts",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _rq.RequestException("refused")))

    label_json = '{"label": "Flow Control", "candidate_types": ["threshold"]}'
    monkeypatch.setattr(
        llm_mod, "query", _fake_query_sequence([label_json]))

    r = runner.invoke(main, ["cluster", "--config", str(config)])
    assert r.exit_code == 0, r.output
    assert "clusters: 1" in r.output
    assert "Flow Control" in r.output

    lines = [ln for ln in r.output.splitlines() if "cluster-label" in ln]
    assert len(lines) == 1
    assert "1 calls" in lines[0]
    assert "$0.0500" in lines[0]


# ---------------------------------------------------------------------------
# `archeon why` (Task 10)
# ---------------------------------------------------------------------------

import yaml  # noqa: E402

from archeon.db import connect  # noqa: E402
from archeon.retrieval.archaeology import ArtifactRefs  # noqa: E402


def _cfg(tmp_path, db_path, repo):
    p = tmp_path / "a.toml"
    p.write_text(
        f'[component]\nname="c"\ndb="{db_path.as_posix()}"\n'
        f'repo_path="{repo.as_posix()}"\npath_prefixes=["src/"]\n'
        '[jira]\nbase_url="u"\nproject_keys=["EMB"]\n'
        '[prs]\nrepo="o/r"\n[wiki]\nexport_dir="d"\n'
        '[llm]\ncheap_model="m"\n')
    return p


def _claim_file(claims_dir, cid="CLM-0001", feature="nav",
                status="machine_verified"):
    claims_dir.mkdir(parents=True, exist_ok=True)
    (claims_dir / f"{cid}.yaml").write_text(yaml.safe_dump({
        "id": cid, "type": "threshold", "statement": "s", "feature": feature,
        "layer": "what", "status": status, "confidence": 0.9,
        "symbols": ["f"], "evidence": [
            {"kind": "code", "ref": "src/a.c:1-2", "role": "primary",
             "excerpt": "x", "commit_sha": "sha1", "blob_sha": None,
             "line_start": 1, "line_end": 2, "content_hash": "h",
             "pin_status": "pinned"}],
        "counter_evidence": [], "corroboration": None, "explains": [],
    }, sort_keys=False))


def test_why_hard_fails_when_the_lake_has_no_artifacts(tmp_path):
    db = tmp_path / "e.db"
    connect(db)                              # empty lake
    repo = tmp_path / "repo"
    repo.mkdir()
    claims = tmp_path / "claims"
    _claim_file(claims)
    r = CliRunner().invoke(main, [
        "why", "--config", str(_cfg(tmp_path, db, repo)),
        "--claims", str(claims)])
    assert r.exit_code == 1
    assert "ingest-prs" in r.output
    # Strengthened: the message must point at every ingest command needed,
    # not just PRs -- an operator following only part of the hint would
    # still have an empty lake.
    assert "ingest-git" in r.output
    assert "ingest-jira" in r.output
    # A hard-fail before any classifier is constructed must not write
    # anything into the claims directory.
    assert not (claims / "why_cost.json").exists()


def test_why_hard_fails_when_no_claims_exist(tmp_path):
    db = tmp_path / "e.db"
    conn = connect(db)
    conn.execute("INSERT INTO tickets(key, summary) VALUES ('EMB-1', 's')")
    conn.commit()
    repo = tmp_path / "repo"
    repo.mkdir()
    r = CliRunner().invoke(main, [
        "why", "--config", str(_cfg(tmp_path, db, repo)),
        "--claims", str(tmp_path / "missing")])
    assert r.exit_code == 1
    assert "synthesize" in r.output


def test_why_checks_claims_dir_before_the_lake_when_both_would_fail(tmp_path):
    """Pins the priority of the two hard-fails (constraint: claims dir
    checked FIRST). Neither existing brief test actually forces this
    ordering: the "no artifacts" test has a real claims dir, and the
    "no claims" test has a lake with a ticket (so the lake precondition
    would pass anyway). Only a case where BOTH preconditions would fail
    can distinguish "claims checked first" from "lake checked first".
    """
    db = tmp_path / "e.db"
    connect(db)                              # empty lake: no tickets, no prs
    repo = tmp_path / "repo"
    repo.mkdir()
    r = CliRunner().invoke(main, [
        "why", "--config", str(_cfg(tmp_path, db, repo)),
        "--claims", str(tmp_path / "missing")])
    assert r.exit_code == 1
    assert "synthesize" in r.output
    assert "ingest-prs" not in r.output


def test_why_preserves_an_existing_run_cost_json(tmp_path):
    db = tmp_path / "e.db"
    conn = connect(db)
    conn.execute("INSERT INTO tickets(key, summary) VALUES ('EMB-1', 's')")
    conn.commit()
    repo = tmp_path / "repo"
    repo.mkdir()                 # deliberately NOT a git repo
    claims = tmp_path / "claims"
    _claim_file(claims)
    # synthesize's cost report already lives in this directory.
    (claims / "run_cost.json").write_text('{"command": "synthesize"}')

    r = CliRunner().invoke(main, [
        "why", "--config", str(_cfg(tmp_path, db, repo)),
        "--claims", str(claims)])

    # No LLM is stubbed because none is reached: `repo` is not a git repo, so
    # archaeology yields no shaping commits, the corpus is empty, and the
    # group is skipped before any classifier is constructed. That is exactly
    # the "never invent a rationale without artifacts" path.
    assert r.exit_code == 0, r.output
    assert "groups without artifacts: 1" in r.output
    # Strengthened: no why-claim was produced and no WHY-*.yaml written.
    assert "why-claims: 0" in r.output
    assert "commits outside the lake: 0" in r.output
    assert list(claims.glob("WHY-*.yaml")) == []
    # synthesize's report survives untouched, and why wrote its own.
    assert json.loads((claims / "run_cost.json").read_text())["command"] == \
        "synthesize"
    why_cost = json.loads((claims / "why_cost.json").read_text())
    assert why_cost["command"] == "why"
    assert why_cost["calls"] == 0


class _FakeWhyMsg:
    """Minimal terminal ResultMessage double, mirroring
    tests/test_llm.py's _FakeCostedResult, so a fake AgentClassifier can
    still exercise the real CostMeter.record path."""
    subtype = "success"

    def __init__(self, usd):
        self.total_cost_usd = usd
        self.num_turns = 1
        self.usage = {"input_tokens": 10, "output_tokens": 5,
                     "cache_read_input_tokens": 0,
                     "cache_creation_input_tokens": 0}


class _FakeWhyClassifier:
    """Stands in for archeon.llm.AgentClassifier. Chooses its canned reply
    from `stage` (why-synth vs why-verify) rather than going through the SDK,
    while still recording into the shared meter so the cost JSON's by_stage
    keys can be checked against the exact hyphenated stage literals the
    task requires."""

    def __init__(self, model, system_prompt, max_turns=1, meter=None,
                stage=""):
        self.model = model
        self.stage = stage
        self.meter = meter

    def ask(self, prompt):
        if self.meter is not None:
            self.meter.record(_FakeWhyMsg(0.01), self.stage, self.model)
        if self.stage == "why-synth":
            return json.dumps([{
                "type": "rationale",
                "statement": "Chosen to keep p95 latency low",
                "explains": ["CLM-0001"],
                "evidence": [{"ref": "EMB-1", "excerpt": "because reasons"}],
            }])
        if self.stage == "why-verify":
            return json.dumps(
                {"supported": True, "confidence": 0.9, "counter": ""})
        raise AssertionError(f"unexpected stage {self.stage!r}")


def test_why_writes_why_claims_with_hyphenated_stage_labels(
        tmp_path, monkeypatch):
    """A full successful run: real synthesize_why_claims/ground_citations/
    verify_why_claims/pin_claims/save_claims, with only the artifact
    archaeology (collect_artifacts) and the LLM (AgentClassifier) faked.
    Proves WHY-*.yaml actually lands on disk and that the cost JSON's
    by_stage buckets use exactly "why-synth"/"why-verify" -- the vocabulary
    constraint the brief's own three tests never exercise, because in all
    of them either no classifier is built or (for the hard-fail tests) the
    command errors before reaching one.
    """
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "src" / "a.c").write_text("int f(void) {\n  return 1;\n}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add f")

    db = tmp_path / "e.db"
    conn = connect(db)
    conn.execute(
        "INSERT INTO tickets(key, summary, description, status, created, "
        "resolved) VALUES ('EMB-1', 'Do the thing', 'because reasons', "
        "'Done', '2026-01-01', '2026-02-01')")
    conn.commit()

    claims = tmp_path / "claims"
    _claim_file(claims)

    import archeon.claims.why_corpus as why_corpus_mod
    monkeypatch.setattr(
        why_corpus_mod, "collect_artifacts",
        lambda conn, repo_path, claims, why_cfg: ArtifactRefs(
            tickets={"EMB-1": {"sha1"}}, prs={}, unknown=set()))

    import archeon.llm as llm_mod
    monkeypatch.setattr(llm_mod, "AgentClassifier", _FakeWhyClassifier)

    r = CliRunner().invoke(main, [
        "why", "--config", str(_cfg(tmp_path, db, repo)),
        "--claims", str(claims)])
    assert r.exit_code == 0, r.output
    assert "why-claims: 1" in r.output
    assert "machine_verified: 1" in r.output
    assert "code_inferred: 0" in r.output

    files = sorted(p.name for p in claims.glob("WHY-*.yaml"))
    assert files == ["WHY-0001.yaml"]

    from archeon.claims.schema import load_claims
    saved = {c.id: c for c in load_claims(claims)}
    why_claim = saved["WHY-0001"]
    assert why_claim.layer == "why"
    assert why_claim.corroboration == "corroborated"
    assert why_claim.status == "machine_verified"
    assert why_claim.explains == ["CLM-0001"]

    cost = json.loads((claims / "why_cost.json").read_text())
    assert cost["command"] == "why"
    assert set(cost["by_stage"]) == {"why-synth", "why-verify"}
    assert cost["by_stage"]["why-synth"]["calls"] == 1
    assert cost["by_stage"]["why-verify"]["calls"] == 1


def test_why_feature_filters_to_matching_group(tmp_path, monkeypatch):
    """--feature must scope the grouped claims, not just cosmetically label
    the run: with two feature groups and an archaeology stub that always
    finds nothing, an unfiltered run reports two skipped groups and a
    --feature-scoped run reports exactly one.
    """
    db = tmp_path / "e.db"
    conn = connect(db)
    conn.execute("INSERT INTO tickets(key, summary) VALUES ('EMB-1', 's')")
    conn.commit()
    repo = tmp_path / "repo"
    repo.mkdir()                 # not a git repo: archaeology finds nothing

    claims = tmp_path / "claims"
    _claim_file(claims, cid="CLM-0001", feature="nav")
    _claim_file(claims, cid="CLM-0002", feature="checkout")

    cfg = _cfg(tmp_path, db, repo)
    runner = CliRunner()

    r_all = runner.invoke(main, ["why", "--config", str(cfg),
                                 "--claims", str(claims)])
    assert r_all.exit_code == 0, r_all.output
    assert "groups without artifacts: 2" in r_all.output

    r_nav = runner.invoke(main, ["why", "--config", str(cfg),
                                 "--claims", str(claims),
                                 "--feature", "nav"])
    assert r_nav.exit_code == 0, r_nav.output
    assert "groups without artifacts: 1" in r_nav.output


# ---------------------------------------------------------------------------
# B1: `why` must only enrich machine_verified what-claims
# ---------------------------------------------------------------------------

def test_why_only_enriches_machine_verified_what_claims(tmp_path, monkeypatch):
    db = tmp_path / "e.db"
    conn = connect(db)
    conn.execute("INSERT INTO tickets(key, summary) VALUES ('EMB-1', 's')")
    conn.commit()
    repo = tmp_path / "repo"
    repo.mkdir()

    claims = tmp_path / "claims"
    _claim_file(claims, cid="CLM-0001", feature="nav",
                status="machine_verified")
    _claim_file(claims, cid="CLM-0002", feature="nav", status="contested")

    seen_ids = []
    import archeon.claims.why_corpus as why_corpus_mod

    def fake_collect(conn, repo_path, members, why_cfg):
        seen_ids.extend(c.id for c in members)
        return ArtifactRefs(tickets={"EMB-1": {"sha1"}}, prs={}, unknown=set())
    monkeypatch.setattr(why_corpus_mod, "collect_artifacts", fake_collect)

    import archeon.llm as llm_mod
    monkeypatch.setattr(llm_mod, "AgentClassifier", _FakeWhyClassifier)

    r = CliRunner().invoke(main, [
        "why", "--config", str(_cfg(tmp_path, db, repo)),
        "--claims", str(claims)])
    assert r.exit_code == 0, r.output
    # Only the machine_verified claim must have been fed into archaeology;
    # the contested one must never be treated as "verified in Pass 1".
    assert seen_ids == ["CLM-0001"]


def test_why_error_message_explains_only_verified_are_enriched(tmp_path):
    db = tmp_path / "e.db"
    conn = connect(db)
    conn.execute("INSERT INTO tickets(key, summary) VALUES ('EMB-1', 's')")
    conn.commit()
    repo = tmp_path / "repo"
    repo.mkdir()
    claims = tmp_path / "claims"
    _claim_file(claims, cid="CLM-0001", feature="nav", status="contested")

    r = CliRunner().invoke(main, [
        "why", "--config", str(_cfg(tmp_path, db, repo)),
        "--claims", str(claims)])
    assert r.exit_code == 1
    # A user with an all-contested directory must not be left guessing why
    # nothing was enriched.
    assert "machine_verified" in r.output


# ---------------------------------------------------------------------------
# B3: re-running `why` must not orphan or silently renumber WHY-claims
# ---------------------------------------------------------------------------

def _setup_why_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "src" / "a.c").write_text("int f(void) {\n  return 1;\n}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add f")

    db = tmp_path / "e.db"
    conn = connect(db)
    conn.execute(
        "INSERT INTO tickets(key, summary, description, status, created, "
        "resolved) VALUES ('EMB-1', 'Do the thing', 'because reasons', "
        "'Done', '2026-01-01', '2026-02-01')")
    conn.commit()
    return repo, db


class _DynamicExplainsWhyClassifier:
    """Like _FakeWhyClassifier, but reads which CLM-#### id to `explain`
    straight out of the prompt (via _format_what_claims) instead of a
    hardcoded "CLM-0001". Needed whenever a test drives more than one
    feature group: a hardcoded explains id would only ever match the group
    that happens to own CLM-0001, silently starving every other group's
    claim (its `explains` would filter to empty and get discarded)."""

    def __init__(self, model, system_prompt, max_turns=1, meter=None,
                stage=""):
        self.model = model
        self.stage = stage
        self.meter = meter

    def ask(self, prompt):
        if self.meter is not None:
            self.meter.record(_FakeWhyMsg(0.01), self.stage, self.model)
        if self.stage == "why-synth":
            m = re.search(r"CLM-\d+", prompt)
            explains = [m.group(0)] if m else []
            return json.dumps([{
                "type": "rationale",
                "statement": "Chosen to keep p95 latency low",
                "explains": explains,
                "evidence": [{"ref": "EMB-1", "excerpt": "because reasons"}],
            }])
        return json.dumps(
            {"supported": True, "confidence": 0.9, "counter": ""})


def test_why_full_rerun_leaves_no_orphan_why_claims(tmp_path, monkeypatch):
    repo, db = _setup_why_repo(tmp_path)
    claims = tmp_path / "claims"
    _claim_file(claims, cid="CLM-0001", feature="nav")
    _claim_file(claims, cid="CLM-0002", feature="checkout")

    import archeon.claims.why_corpus as why_corpus_mod
    monkeypatch.setattr(
        why_corpus_mod, "collect_artifacts",
        lambda conn, repo_path, members, why_cfg: ArtifactRefs(
            tickets={"EMB-1": {"sha1"}}, prs={}, unknown=set()))
    import archeon.llm as llm_mod
    monkeypatch.setattr(llm_mod, "AgentClassifier",
                        _DynamicExplainsWhyClassifier)

    cfg = _cfg(tmp_path, db, repo)
    runner = CliRunner()
    # First (full) run: both feature groups produce a why-claim each ->
    # WHY-0001/0002.
    r1 = runner.invoke(main, ["why", "--config", str(cfg),
                             "--claims", str(claims)])
    assert r1.exit_code == 0, r1.output
    assert sorted(p.name for p in claims.glob("WHY-*.yaml")) == \
        ["WHY-0001.yaml", "WHY-0002.yaml"]

    # Second (also full) run, but now only the "nav" group has any
    # artifacts -> it must produce fewer claims than run 1. A full re-run
    # must leave exactly this run's output, not an orphaned WHY-0002 from
    # the previous run pointing at claims this run never even considered.
    monkeypatch.setattr(
        why_corpus_mod, "collect_artifacts",
        lambda conn, repo_path, members, why_cfg: ArtifactRefs(
            tickets={"EMB-1": {"sha1"}}, prs={}, unknown=set())
            if members[0].feature == "nav" else
            ArtifactRefs(tickets={}, prs={}, unknown=set()))
    r2 = runner.invoke(main, ["why", "--config", str(cfg),
                             "--claims", str(claims)])
    assert r2.exit_code == 0, r2.output
    assert sorted(p.name for p in claims.glob("WHY-*.yaml")) == \
        ["WHY-0001.yaml"]
    # CLM-* what-claims must never be touched.
    assert sorted(p.name for p in claims.glob("CLM-*.yaml")) == \
        ["CLM-0001.yaml", "CLM-0002.yaml"]


def test_why_two_feature_runs_both_survive_with_distinct_ids(
        tmp_path, monkeypatch):
    repo, db = _setup_why_repo(tmp_path)
    claims = tmp_path / "claims"
    _claim_file(claims, cid="CLM-0001", feature="nav")
    _claim_file(claims, cid="CLM-0002", feature="checkout")

    import archeon.claims.why_corpus as why_corpus_mod
    monkeypatch.setattr(
        why_corpus_mod, "collect_artifacts",
        lambda conn, repo_path, members, why_cfg: ArtifactRefs(
            tickets={"EMB-1": {"sha1"}}, prs={}, unknown=set()))
    import archeon.llm as llm_mod
    monkeypatch.setattr(llm_mod, "AgentClassifier",
                        _DynamicExplainsWhyClassifier)

    cfg = _cfg(tmp_path, db, repo)
    runner = CliRunner()
    r_nav = runner.invoke(main, ["why", "--config", str(cfg),
                                "--claims", str(claims),
                                "--feature", "nav"])
    assert r_nav.exit_code == 0, r_nav.output
    r_checkout = runner.invoke(main, ["why", "--config", str(cfg),
                                      "--claims", str(claims),
                                      "--feature", "checkout"])
    assert r_checkout.exit_code == 0, r_checkout.output

    files = sorted(p.name for p in claims.glob("WHY-*.yaml"))
    assert len(files) == 2                    # both features' claims present
    ids = [f.replace(".yaml", "") for f in files]
    assert len(set(ids)) == 2                 # non-colliding ids

    from archeon.claims.schema import load_claims
    why_claims = {c.id: c for c in load_claims(claims) if c.layer == "why"}
    features = {c.feature for c in why_claims.values()}
    assert features == {"nav", "checkout"}


# ---------------------------------------------------------------------------
# N2: grounding stats must be surfaced, not discarded
# ---------------------------------------------------------------------------

def test_why_prints_citations_grounded_and_dropped_counts(tmp_path,
                                                           monkeypatch):
    repo, db = _setup_why_repo(tmp_path)
    claims = tmp_path / "claims"
    _claim_file(claims, cid="CLM-0001", feature="nav")

    import archeon.claims.why_corpus as why_corpus_mod
    monkeypatch.setattr(
        why_corpus_mod, "collect_artifacts",
        lambda conn, repo_path, members, why_cfg: ArtifactRefs(
            tickets={"EMB-1": {"sha1"}}, prs={}, unknown=set()))

    class _MixedGroundingClassifier(_FakeWhyClassifier):
        def ask(self, prompt):
            if self.meter is not None:
                self.meter.record(_FakeWhyMsg(0.01), self.stage, self.model)
            if self.stage == "why-synth":
                return json.dumps([{
                    "type": "rationale",
                    "statement": "Chosen to keep p95 latency low",
                    "explains": ["CLM-0001"],
                    "evidence": [
                        {"ref": "EMB-1", "excerpt": "because reasons"},
                        {"ref": "EMB-1", "excerpt": "a fabricated quote"},
                    ],
                }])
            return json.dumps(
                {"supported": True, "confidence": 0.9, "counter": ""})

    import archeon.llm as llm_mod
    monkeypatch.setattr(llm_mod, "AgentClassifier", _MixedGroundingClassifier)

    r = CliRunner().invoke(main, [
        "why", "--config", str(_cfg(tmp_path, db, repo)),
        "--claims", str(claims)])
    assert r.exit_code == 0, r.output
    # One real quote grounds; the fabricated one is dropped by
    # ground_citations. The pipeline's only fabrication/loss measurement
    # must not be thrown away.
    assert "citations grounded: 1  dropped: 1" in r.output


# ---------------------------------------------------------------------------
# Issue 1: a run that produces zero why-claims must not wipe prior output
# ---------------------------------------------------------------------------

def _why_claim_file(claims_dir, cid="WHY-0001", feature="nav",
                    status="expert_accepted"):
    claims_dir.mkdir(parents=True, exist_ok=True)
    (claims_dir / f"{cid}.yaml").write_text(yaml.safe_dump({
        "id": cid, "type": "rationale", "statement": "human-reviewed why",
        "feature": feature, "layer": "why", "status": status,
        "confidence": 0.9, "symbols": [], "evidence": [],
        "counter_evidence": [], "corroboration": "corroborated",
        "explains": ["CLM-0001"],
    }, sort_keys=False))


def test_why_synthesis_failure_for_every_group_preserves_prior_why_claims(
        tmp_path, monkeypatch):
    """Issue 1(a): if why-synthesis raises for every group (e.g. a backend
    outage), the run must not delete pre-existing WHY-*.yaml -- a human's
    expert_accepted review state on those claims is not recoverable by
    re-running, and the old code deleted the whole WHY-* directory
    unconditionally before this loop even ran.
    """
    repo, db = _setup_why_repo(tmp_path)
    claims = tmp_path / "claims"
    _claim_file(claims, cid="CLM-0001", feature="nav")
    _why_claim_file(claims, cid="WHY-0001", feature="nav",
                    status="expert_accepted")
    prior_content = (claims / "WHY-0001.yaml").read_text()

    import archeon.claims.why_corpus as why_corpus_mod
    monkeypatch.setattr(
        why_corpus_mod, "collect_artifacts",
        lambda conn, repo_path, members, why_cfg: ArtifactRefs(
            tickets={"EMB-1": {"sha1"}}, prs={}, unknown=set()))

    import archeon.claims.why as why_mod

    def _boom(*a, **k):
        raise RuntimeError("backend outage")
    monkeypatch.setattr(why_mod, "synthesize_why_claims", _boom)

    r = CliRunner().invoke(main, [
        "why", "--config", str(_cfg(tmp_path, db, repo)),
        "--claims", str(claims)])

    assert r.exit_code != 0
    assert (claims / "WHY-0001.yaml").is_file()
    assert (claims / "WHY-0001.yaml").read_text() == prior_content


def test_why_no_artifacts_for_any_group_preserves_prior_why_claims(tmp_path):
    """Issue 1(b): archaeology/build_corpus finding zero artifacts for
    every group (wrong repo_path, a re-cloned repo whose shas no longer
    match the lake, or ingest not yet covering these commits) is a
    legitimate outcome -- but it is not the same as "the correct new state
    is empty", so it must not wipe prior WHY-*.yaml either. This is a valid
    run, so it must still exit 0.
    """
    db = tmp_path / "e.db"
    conn = connect(db)
    conn.execute("INSERT INTO tickets(key, summary) VALUES ('EMB-1', 's')")
    conn.commit()
    repo = tmp_path / "repo"
    repo.mkdir()                 # not a git repo: archaeology finds nothing

    claims = tmp_path / "claims"
    _claim_file(claims, cid="CLM-0001", feature="nav")
    _why_claim_file(claims, cid="WHY-0001", feature="nav",
                    status="expert_accepted")
    prior_content = (claims / "WHY-0001.yaml").read_text()

    r = CliRunner().invoke(main, [
        "why", "--config", str(_cfg(tmp_path, db, repo)),
        "--claims", str(claims)])

    assert r.exit_code == 0, r.output
    assert "groups without artifacts: 1" in r.output
    assert (claims / "WHY-0001.yaml").is_file()
    assert (claims / "WHY-0001.yaml").read_text() == prior_content


# ---------------------------------------------------------------------------
# Issue 2: _why_num must tolerate a non-string (YAML integer) claim id
# ---------------------------------------------------------------------------

def test_why_feature_run_tolerates_non_string_why_claim_id(
        tmp_path, monkeypatch):
    """A hand-edited WHY-*.yaml with an unquoted numeric YAML id (`id: 5`)
    used to crash a --feature run: `_why_num` fed the id straight to
    `re.fullmatch`, which raises TypeError on a non-string/bytes argument.
    """
    repo, db = _setup_why_repo(tmp_path)
    claims = tmp_path / "claims"
    _claim_file(claims, cid="CLM-0001", feature="nav")
    _claim_file(claims, cid="CLM-0002", feature="checkout")
    # Hand-edited why-claim with a bare YAML integer id (not quoted).
    (claims / "WHY-int-id.yaml").write_text(yaml.safe_dump({
        "id": 5, "type": "rationale", "statement": "s", "feature": "checkout",
        "layer": "why", "status": "recovered", "confidence": 0.5,
        "symbols": [], "evidence": [], "counter_evidence": [],
        "corroboration": "corroborated", "explains": ["CLM-0002"],
    }, sort_keys=False))

    import archeon.claims.why_corpus as why_corpus_mod
    monkeypatch.setattr(
        why_corpus_mod, "collect_artifacts",
        lambda conn, repo_path, members, why_cfg: ArtifactRefs(
            tickets={"EMB-1": {"sha1"}}, prs={}, unknown=set()))
    import archeon.llm as llm_mod
    monkeypatch.setattr(llm_mod, "AgentClassifier", _FakeWhyClassifier)

    r = CliRunner().invoke(main, [
        "why", "--config", str(_cfg(tmp_path, db, repo)),
        "--claims", str(claims), "--feature", "nav"])
    assert r.exit_code == 0, r.output


# ---------------------------------------------------------------------------
# Issue 3: expert_accepted what-claims must be enriched too
# ---------------------------------------------------------------------------

def test_why_enriches_expert_accepted_what_claims_too(tmp_path, monkeypatch):
    """expert_accepted is a HUMAN review outcome -- strictly stronger
    evidence than machine_verified -- so `why` must enrich it too, not
    silently skip the most trustworthy what-claims in the directory.
    """
    db = tmp_path / "e.db"
    conn = connect(db)
    conn.execute("INSERT INTO tickets(key, summary) VALUES ('EMB-1', 's')")
    conn.commit()
    repo = tmp_path / "repo"
    repo.mkdir()

    claims = tmp_path / "claims"
    _claim_file(claims, cid="CLM-0001", feature="nav",
                status="expert_accepted")
    _claim_file(claims, cid="CLM-0002", feature="nav", status="contested")

    seen_ids = []
    import archeon.claims.why_corpus as why_corpus_mod

    def fake_collect(conn, repo_path, members, why_cfg):
        seen_ids.extend(c.id for c in members)
        return ArtifactRefs(tickets={"EMB-1": {"sha1"}}, prs={}, unknown=set())
    monkeypatch.setattr(why_corpus_mod, "collect_artifacts", fake_collect)

    import archeon.llm as llm_mod
    monkeypatch.setattr(llm_mod, "AgentClassifier", _FakeWhyClassifier)

    r = CliRunner().invoke(main, [
        "why", "--config", str(_cfg(tmp_path, db, repo)),
        "--claims", str(claims)])
    assert r.exit_code == 0, r.output
    # Only the expert_accepted claim must have been fed into archaeology;
    # the contested one must still be excluded.
    assert seen_ids == ["CLM-0001"]


# ---------------------------------------------------------------------------
# Issue 1(c): a full run where SOME groups complete and OTHERS error must be
# a true partial replace, not a full wipe or a full preserve.
# ---------------------------------------------------------------------------

def test_why_partial_failure_preserves_only_the_errored_groups_why_claims(
        tmp_path, monkeypatch):
    """A full run (no --feature) with two feature groups where one group's
    why-synthesis raises and the other completes must: replace the
    completed group's prior WHY-*.yaml, leave the errored group's prior
    WHY-*.yaml (which may carry a human's expert_accepted review) untouched,
    number new claims past every existing WHY- id, and leave CLM-*.yaml
    alone. This is the one branch of the Issue-1 data-loss fix
    (`elif errored_labels:` in cli_why) with no prior coverage.
    """
    repo, db = _setup_why_repo(tmp_path)
    claims = tmp_path / "claims"
    _claim_file(claims, cid="CLM-0001", feature="nav")
    _claim_file(claims, cid="CLM-0002", feature="checkout")
    clm1_content = (claims / "CLM-0001.yaml").read_text()
    clm2_content = (claims / "CLM-0002.yaml").read_text()

    # Pre-existing why-claims, one per group, with distinct ids. "checkout"
    # will error this run -- its expert_accepted file documents the review
    # state that must not be silently destroyed. "nav" will complete -- its
    # merely-"recovered" prior file is expected to be replaced.
    _why_claim_file(claims, cid="WHY-0001", feature="checkout",
                    status="expert_accepted")
    _why_claim_file(claims, cid="WHY-0002", feature="nav",
                    status="recovered")
    checkout_prior_content = (claims / "WHY-0001.yaml").read_text()

    import archeon.claims.why_corpus as why_corpus_mod
    # Artifacts available for both groups (feature-agnostic), so neither is
    # skipped for having an empty corpus.
    monkeypatch.setattr(
        why_corpus_mod, "collect_artifacts",
        lambda conn, repo_path, members, why_cfg: ArtifactRefs(
            tickets={"EMB-1": {"sha1"}}, prs={}, unknown=set()))

    class _PartialFailureWhyClassifier:
        """why-synth raises for the "checkout" group and succeeds for
        "nav". Groups are iterated in `sorted(groups.items())` order (i.e.
        "checkout" before "nav"), and WHY_SYNTH_PROMPT interpolates
        "Feature: {feature}" verbatim (see
        archeon.claims.why.WHY_SYNTH_PROMPT/_format_what_claims), so
        branching on that exact text reliably distinguishes the two
        groups' synthesis calls. The same class is also constructed for
        the why-verify stage, for whichever claims the "nav" group
        produces -- it must return valid supporting JSON there too, or the
        completed group would itself end up contested/empty.
        """
        def __init__(self, model, system_prompt, max_turns=1, meter=None,
                    stage=""):
            self.model = model
            self.stage = stage
            self.meter = meter

        def ask(self, prompt):
            if self.meter is not None:
                self.meter.record(_FakeWhyMsg(0.01), self.stage, self.model)
            if self.stage == "why-synth":
                if "Feature: checkout" in prompt:
                    raise RuntimeError("synthesis backend outage")
                assert "Feature: nav" in prompt
                m = re.search(r"CLM-\d+", prompt)
                explains = [m.group(0)] if m else []
                return json.dumps([{
                    "type": "rationale",
                    "statement": "Chosen to keep p95 latency low",
                    "explains": explains,
                    "evidence": [
                        {"ref": "EMB-1", "excerpt": "because reasons"}],
                }])
            return json.dumps(
                {"supported": True, "confidence": 0.9, "counter": ""})

    import archeon.llm as llm_mod
    monkeypatch.setattr(llm_mod, "AgentClassifier",
                        _PartialFailureWhyClassifier)

    r = CliRunner().invoke(main, [
        "why", "--config", str(_cfg(tmp_path, db, repo)),
        "--claims", str(claims)])

    assert r.exit_code == 0, r.output       # partial failure is not total

    # The errored group's prior why-claim survives, byte-for-byte, including
    # its expert_accepted status -- not merely "the file still exists".
    assert (claims / "WHY-0001.yaml").is_file()
    assert (claims / "WHY-0001.yaml").read_text() == checkout_prior_content
    assert yaml.safe_load(checkout_prior_content)["status"] == \
        "expert_accepted"

    # The completed group's prior why-claim is gone.
    assert not (claims / "WHY-0002.yaml").is_file()

    # At least one new why-claim exists, numbered PAST every pre-existing
    # WHY- id (0001, 0002) so it cannot collide with the preserved file.
    remaining = sorted(p.name for p in claims.glob("WHY-*.yaml"))
    new_files = [n for n in remaining if n != "WHY-0001.yaml"]
    assert new_files, "expected at least one new why-claim from the " \
                      "completed group"
    for name in new_files:
        num = int(name[len("WHY-"):-len(".yaml")])
        assert num > 2

    # CLM-*.yaml (what-claims) must never be touched.
    assert (claims / "CLM-0001.yaml").read_text() == clm1_content
    assert (claims / "CLM-0002.yaml").read_text() == clm2_content

    # stderr carries the synthesis-failure warning for the errored group.
    assert "why-synthesis failed for checkout" in r.output
