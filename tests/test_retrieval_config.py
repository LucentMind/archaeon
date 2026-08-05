from archeon import config as config_mod


def test_retrieval_defaults_when_absent():
    r = config_mod.retrieval({})
    assert r["embed_model"] == "qwen3-embedding:4b"
    assert r["embed_endpoint"] == "http://localhost:11434"
    assert r["embed_dims"] == 1024
    assert r["token_budget"] == 60000
    assert r["sim_top_k"] == 10


def test_retrieval_overrides_merge_over_defaults():
    r = config_mod.retrieval(
        {"retrieval": {"embed_model": "qwen3-embedding:0.6b", "embed_dims": 256}})
    assert r["embed_model"] == "qwen3-embedding:0.6b"
    assert r["embed_dims"] == 256
    # untouched keys keep their defaults
    assert r["token_budget"] == 60000
    assert r["w_references"] == 1.0


def test_why_defaults_apply_when_block_absent():
    w = config_mod.why({})
    assert w["max_commits_per_span"] == 50
    assert w["token_budget"] == 40000
    assert w["model"] is None


def test_why_block_overrides_defaults():
    w = config_mod.why({"why": {"token_budget": 1000}})
    assert w["token_budget"] == 1000
    assert w["max_commits_per_span"] == 50      # untouched default


def test_why_is_not_required_for_config_validation(tmp_path):
    # A config with no [why] section must still load.
    p = tmp_path / "a.toml"
    p.write_text(
        '[component]\nname="c"\ndb="e.db"\nrepo_path="."\n'
        'path_prefixes=["src/"]\n'
        '[jira]\nbase_url="u"\nproject_keys=["A"]\n'
        '[prs]\nrepo="o/r"\n[wiki]\nexport_dir="d"\n'
        '[llm]\ncheap_model="m"\n')
    assert config_mod.why(config_mod.load(p))["token_budget"] == 40000
