import tomllib
from pathlib import Path

REQUIRED = {
    "component": ["name", "db", "repo_path", "path_prefixes"],
    "jira": ["base_url", "project_keys"],
    "prs": ["repo"],
    "wiki": ["export_dir"],
    "llm": ["cheap_model"],
}

RETRIEVAL_DEFAULTS = {
    "embed_model": "qwen3-embedding:4b",
    "embed_endpoint": "http://localhost:11434",
    "embed_dims": 1024,
    "embed_max_tokens": 2000,
    "token_budget": 60000,
    "w_references": 1.0,
    "w_includes": 0.5,
    "w_coupling": 0.5,
    "w_embedding": 1.0,
    "sim_top_k": 10,
    "max_cross_file_pairs": 400,
}

WHY_DEFAULTS = {
    "max_commits_per_span": 50,   # cap on git log -L archaeology per span
    "token_budget": 40000,        # artifact corpus budget per cluster
    "model": None,                # falls back to llm.expensive_model
}


def retrieval(config: dict) -> dict:
    """Merge the optional [retrieval] block over the code defaults.

    Kept out of REQUIRED so existing configs (and the P0/spike tests) keep
    validating without a [retrieval] section; embeddings degrade to graph-only
    at runtime anyway.
    """
    merged = dict(RETRIEVAL_DEFAULTS)
    merged.update(config.get("retrieval", {}))
    return merged


def why(config: dict) -> dict:
    """Merge the optional [why] block over the code defaults.

    Kept out of REQUIRED, like [retrieval], so existing configs keep
    validating without a [why] section.
    """
    merged = dict(WHY_DEFAULTS)
    merged.update(config.get("why", {}))
    return merged


def load(path: Path) -> dict:
    with open(path, "rb") as f:
        config = tomllib.load(f)
    for section, keys in REQUIRED.items():
        if section not in config:
            raise ValueError(f"missing [{section}] in {path}")
        for key in keys:
            if key not in config[section]:
                raise ValueError(f"missing {section}.{key} in {path}")
    return config
