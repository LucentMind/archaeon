import json
import os
import re
from pathlib import Path

import click

from archaeon import config as config_mod
from archaeon.analysis.coupling import compute_coupling, strongest_pairs
from archaeon.analysis.link_eval import evaluate, load_gold
from archaeon.analysis.link_heuristics import (
    discover_ticket_keys, extract_heuristic_links)
from archaeon.analysis.link_llm import recover_links
from archaeon.codegraph.scan import scan_component
from archaeon.connectors.git_connector import ingest_git
from archaeon.connectors.jira_connector import ingest_jira_by_keys
from archaeon.connectors.pr_connector import ingest_prs
from archaeon.connectors.wiki_connector import ingest_wiki_export
from archaeon.db import connect


def _load(config_path: str):
    cfg = config_mod.load(Path(config_path))
    conn = connect(cfg["component"]["db"])
    return cfg, conn


config_option = click.option("--config", "config_path",
                             default="archaeon.toml", show_default=True)


@click.group()
def main():
    """Archaeon evidence lake."""


@main.command("ingest-git")
@config_option
def cli_ingest_git(config_path):
    cfg, conn = _load(config_path)
    git_cfg = cfg.get("git", {})
    n = ingest_git(conn, Path(cfg["component"]["repo_path"]),
                   cfg["component"]["path_prefixes"],
                   exclude_authors=git_cfg.get("exclude_authors"),
                   exclude_message_patterns=git_cfg.get(
                       "exclude_message_patterns"))
    click.echo(f"commits: {n}")


@main.command("ingest-jira")
@config_option
def cli_ingest_jira(config_path):
    """Fetch only the tickets referenced by the component's commits/PRs.
    Run ingest-git and ingest-prs first so those keys are discoverable.
    """
    cfg, conn = _load(config_path)
    keys = discover_ticket_keys(conn, cfg["jira"]["project_keys"])
    n = ingest_jira_by_keys(conn, cfg["jira"]["base_url"], keys,
                            os.environ["ARCHAEON_JIRA_TOKEN"],
                            email=os.environ.get("ARCHAEON_JIRA_EMAIL"))
    click.echo(f"tickets: {n} (from {len(keys)} discovered keys)")


@main.command("ingest-prs")
@config_option
def cli_ingest_prs(config_path):
    cfg, conn = _load(config_path)

    def progress(sha, i, total, inserted):
        if i == 1 or i == total or i % 25 == 0:
            click.echo(f"  [{i}/{total}] {sha[:12]}  ({inserted} PRs so far)",
                      err=True)

    n = ingest_prs(conn, cfg["prs"]["repo"],
                   hostname=cfg["prs"].get("hostname"), on_progress=progress)
    click.echo(f"prs: {n}")


@main.command("ingest-wiki")
@config_option
def cli_ingest_wiki(config_path):
    cfg, conn = _load(config_path)
    n = ingest_wiki_export(conn, Path(cfg["wiki"]["export_dir"]))
    click.echo(f"pages: {n}")


@main.command("link")
@config_option
def cli_link(config_path):
    cfg, conn = _load(config_path)
    n = extract_heuristic_links(conn, cfg["jira"]["project_keys"])
    click.echo(f"links: {n}")


@main.command("link-llm")
@config_option
def cli_link_llm(config_path):
    from archaeon.llm import AgentClassifier
    cfg, conn = _load(config_path)
    classifier = AgentClassifier(cfg["llm"]["cheap_model"], max_turns=3)
    n = recover_links(conn, classifier.ask,
                      cfg["llm"].get("max_commits", 200))
    click.echo(f"llm links: {n}")


@main.command("coupling")
@config_option
def cli_coupling(config_path):
    cfg, conn = _load(config_path)
    n = compute_coupling(conn)
    click.echo(f"pairs: {n}")


@main.command("scan")
@config_option
def cli_scan(config_path):
    cfg, conn = _load(config_path)
    compile_db = cfg["component"].get("compile_db_dir")
    stats = scan_component(conn, Path(cfg["component"]["repo_path"]),
                           cfg["component"]["path_prefixes"],
                           Path(compile_db) if compile_db else None,
                           include=cfg["component"].get("include"),
                           exclude=cfg["component"].get("exclude"))
    click.echo(f"clang: {stats['clang']}  tree-sitter: "
               f"{stats['tree_sitter']}  gaps: {stats['gaps']}")


@main.command("embed")
@config_option
def cli_embed(config_path):
    """Build the local embedding index for scanned symbols (idempotent)."""
    from archaeon.retrieval.embed import build_embedding_index
    cfg, conn = _load(config_path)
    retr = config_mod.retrieval(cfg)
    r = build_embedding_index(conn, Path(cfg["component"]["repo_path"]),
                              retr["embed_model"], retr["embed_endpoint"],
                              retr["embed_dims"],
                              max_tokens=retr["embed_max_tokens"])
    if r["ollama_available"]:
        click.echo(f"embedded: {r['embedded']}  skipped: {r['skipped']}  "
                   f"unembeddable: {r['unembeddable']}  "
                   f"model: {retr['embed_model']} dims: {retr['embed_dims']}")
    else:
        click.echo(f"ollama unavailable ({r.get('error', 'unknown')}); "
                   f"embedded {r['embedded']} before failing — clustering "
                   f"will fall back to graph-only")


@main.command("cluster")
@config_option
def cli_cluster(config_path):
    """Cluster scanned symbols into feature areas (embeds first if possible)."""
    from archaeon.cost import CostMeter
    from archaeon.llm import AgentClassifier
    from archaeon.retrieval.cluster import (
        LABEL_SYSTEM, cluster_symbols, label_cluster)
    from archaeon.retrieval.embed import build_embedding_index
    cfg, conn = _load(config_path)
    retr = config_mod.retrieval(cfg)
    e = build_embedding_index(conn, Path(cfg["component"]["repo_path"]),
                              retr["embed_model"], retr["embed_endpoint"],
                              retr["embed_dims"],
                              max_tokens=retr["embed_max_tokens"])
    if not e["ollama_available"]:
        click.echo("ollama unavailable; clustering on graph signal only",
                   err=True)
    meter = CostMeter()
    labeller = AgentClassifier(cfg["llm"]["cheap_model"], LABEL_SYSTEM,
                               max_turns=1, meter=meter,
                               stage="cluster-label")
    clusters = cluster_symbols(
        conn, Path(cfg["component"]["repo_path"]),
        cfg["component"]["name"], retr,
        label_fn=lambda rows: label_cluster(rows, labeller.ask))
    click.echo(f"clusters: {len(clusters)}  "
               f"(ollama: {'yes' if e['ollama_available'] else 'no'})")
    for c in clusters:
        click.echo(f"  [{c['id']}] {c['label'] or '(unlabelled)'}  "
                   f"({len(c['members'])} symbols)")
    click.echo(meter.format_summary("cluster"))


@main.command("eval")
@config_option
@click.option("--gold", "gold_path", required=True)
@click.option("--method", "methods", multiple=True)
def cli_eval(config_path, gold_path, methods):
    cfg, conn = _load(config_path)
    gold, sampled = load_gold(Path(gold_path))
    m = evaluate(conn, gold, sampled, list(methods) or None)
    click.echo(f"precision: {m['precision']:.3f}  recall: "
               f"{m['recall']:.3f}  predicted: {m['predicted']}  "
               f"gold: {m['gold']}")


@main.command("synthesize")
@config_option
@click.option("--feature", "feature", default=None,
              help="path prefix of the feature area to synthesize claims for")
@click.option("--cluster", "cluster_id", type=int, default=None,
              help="synthesize a single cluster id (from `cluster`)")
@click.option("--all-clusters", "all_clusters", is_flag=True,
              help="synthesize every cluster in the component")
@click.option("--out", "out_dir", default="claims", show_default=True)
def cli_synthesize(config_path, feature, cluster_id, all_clusters, out_dir):
    """Recover + adversarially verify what-layer claims from code.

    Bundles are built from clusters (token-bounded, relevance-ranked) when
    --cluster/--all-clusters is given; --feature bundles exactly the symbols
    under that path prefix (ranked by the prefix symbols' own centroid),
    never expanding to whole clusters that merely overlap the prefix.
    """
    from archaeon.claims.recover import (
        SYNTH_SYSTEM, VERIFY_SYSTEM, synthesize_claims, verify_claims)
    from archaeon.claims.pin import pin_claims
    from archaeon.claims.schema import save_claims
    from archaeon.cost import CostMeter
    from archaeon.llm import AgentClassifier
    from archaeon.retrieval.bundle import bundle_for_cluster, bundle_for_prefix

    if sum(bool(x) for x in (feature, cluster_id is not None,
                             all_clusters)) != 1:
        raise click.UsageError(
            "give exactly one of --feature, --cluster, --all-clusters")
    cfg, conn = _load(config_path)
    retr = config_mod.retrieval(cfg)
    repo = Path(cfg["component"]["repo_path"])

    # Resolve the list of (feature_label, cluster_id | None) targets.
    targets: list[tuple[str, int | None]] = []
    if all_clusters:
        targets = [(r["label"] or f"cluster-{r['id']}", r["id"])
                   for r in conn.execute(
                       "SELECT id, label FROM clusters ORDER BY id")]
        if not targets:
            raise click.ClickException("no clusters; run `cluster` first")
    elif cluster_id is not None:
        row = conn.execute("SELECT id, label FROM clusters WHERE id=?",
                           (cluster_id,)).fetchone()
        if row is None:
            raise click.ClickException(f"no cluster {cluster_id}")
        targets = [(row["label"] or f"cluster-{row['id']}", row["id"])]
    else:
        targets = [(feature, None)]

    model = cfg["llm"].get("expensive_model", cfg["llm"]["cheap_model"])
    # One meter for the whole run: every classifier below records into it.
    meter = CostMeter()
    all_claims = []
    for label, cid in targets:
        if cid is not None:
            bundle, _ = bundle_for_cluster(conn, repo, cid, retr)
        else:
            bundle, _ = bundle_for_prefix(conn, repo, feature, retr)
            if not bundle:
                raise click.ClickException(
                    "no parsed files under that prefix; run scan first")
        claims = synthesize_claims(
            label, bundle,
            AgentClassifier(model, SYNTH_SYSTEM, max_turns=4,
                            meter=meter, stage="synthesize").ask)
        verify_claims(claims, bundle,
                      AgentClassifier(model, VERIFY_SYSTEM, max_turns=4,
                                      meter=meter, stage="verify").ask)
        all_claims.extend(claims)

    # Re-id claims uniquely across clusters before saving.
    for i, c in enumerate(all_claims, 1):
        c.id = f"CLM-{i:04d}"
    # Commit-pin each code evidence to a content hash + HEAD sha before
    # persisting, so staleness is decidable later (Spec B). Degrades
    # per-evidence; never aborts the run. The scoped symbol paths let pinning
    # resolve a basename-only ref (which git can't locate from the repo root)
    # back to its unique full repo-relative path instead of degrading it.
    known_paths = [r["path"] for r in
                   conn.execute("SELECT DISTINCT path FROM symbols")]
    pin_claims(all_claims, repo, known_paths=known_paths)
    save_claims(all_claims, Path(out_dir))
    verified = sum(1 for c in all_claims if c.status == "machine_verified")
    click.echo(f"claims: {len(all_claims)}  machine_verified: {verified}  "
               f"contested: {len(all_claims) - verified}  -> {out_dir}/")
    # One summary_dict() call feeds both the printed line and the JSON
    # file, so the two surfaces share a single billing-route probe and
    # cannot disagree about it.
    cost_summary = meter.summary_dict("synthesize")
    click.echo(meter.format_summary("synthesize", cost_summary))
    (Path(out_dir) / "run_cost.json").write_text(
        json.dumps(cost_summary, indent=2),
        encoding="utf-8")


@main.command("why")
@config_option
@click.option("--claims", "claims_dir", default="claims", show_default=True,
              help="directory of what-layer claim YAML to enrich")
@click.option("--feature", "feature", default=None,
              help="only enrich claims whose feature label matches")
def cli_why(config_path, claims_dir, feature):
    """Recover why-layer claims (Pass 2) from tickets and PRs.

    Walks each what-claim's commit-pinned span back through git history to
    the commits that shaped it, resolves those to tickets and PRs, then
    synthesizes, mechanically grounds, and adversarially verifies the
    rationale. Run `synthesize` and the ingest commands first.
    """
    from archaeon.claims.pin import pin_claims
    from archaeon.claims.schema import load_claims, save_claims
    from archaeon.claims.why import (
        WHY_SYNTH_SYSTEM, WHY_VERIFY_SYSTEM, ground_citations,
        synthesize_why_claims, verify_why_claims)
    from archaeon.claims.why_corpus import build_corpus, collect_artifacts
    from archaeon.cost import CostMeter
    from archaeon.llm import AgentClassifier

    cfg, conn = _load(config_path)
    why_cfg = config_mod.why(cfg)
    repo = Path(cfg["component"]["repo_path"])
    out = Path(claims_dir)

    # Both preconditions are checked before any classifier is built, so a
    # misconfigured run cannot spend anything. The claims-dir check comes
    # first: it is the one nearest a user's next action ("run synthesize"),
    # and a run pointed at a bad claims path has nothing to enrich regardless
    # of what the evidence lake holds.
    if not out.is_dir() or not any(out.glob("*.yaml")):
        raise click.ClickException(
            f"no claim YAML in {out}/; run `synthesize` first")
    tickets = conn.execute("SELECT COUNT(*) AS n FROM tickets").fetchone()["n"]
    prs = conn.execute("SELECT COUNT(*) AS n FROM prs").fetchone()["n"]
    if not tickets and not prs:
        raise click.ClickException(
            "evidence lake has no tickets or PRs; the why-layer has nothing "
            "to corroborate against -- run `ingest-git`, `ingest-prs` and "
            "`ingest-jira` first")

    existing = load_claims(out)
    # B1: only a claim that survived adversarial verification in Pass 1, or
    # a human review, may be enriched. `synthesize` routinely leaves
    # `contested` what-claims on disk; enriching those would tell the
    # why-synthesizer they were "verified in Pass 1" (see _code_hypothesis)
    # when they were not, and would copy an unverified code hypothesis onto
    # a why-claim. `expert_accepted` is a human review outcome -- strictly
    # stronger evidence than `machine_verified` -- so it is included too.
    what = [c for c in existing if c.layer == "what" and
            c.status in ("machine_verified", "expert_accepted")]
    if feature:
        what = [c for c in what if c.feature == feature]
    if not what:
        raise click.ClickException(
            "no machine_verified or expert_accepted what-layer claims to "
            "enrich -- only machine_verified and expert_accepted "
            "what-claims are enriched (contested claims are excluded); "
            "check whether every what-claim in this directory is still "
            "contested, or re-run `synthesize`")

    model = why_cfg["model"] or cfg["llm"].get("expensive_model",
                                               cfg["llm"]["cheap_model"])
    meter = CostMeter()
    groups: dict = {}
    for c in what:
        groups.setdefault(c.feature or "(unlabelled)", []).append(c)

    all_why, unknown_shas, uncorroborated = [], set(), 0
    # N2: ground_citations' return value is the pipeline's only measurement
    # of citation fabrication/loss. Accumulated across every group so the
    # summary can report it instead of discarding it.
    grounding = {"grounded": 0, "dropped": 0, "code_inferred": 0}
    # Issue 1: track which groups actually finished (whether they produced
    # claims or legitimately found no artifacts) versus which raised. Only
    # a *completed* group's prior WHY-*.yaml may be replaced below -- an
    # *errored* group's prior output (which may carry a human's
    # expert_accepted review) must survive, since this run has nothing
    # trustworthy to replace it with.
    completed_labels: set = set()
    errored_labels: set = set()
    for label, members in sorted(groups.items()):
        refs = collect_artifacts(conn, repo, members, why_cfg)
        unknown_shas |= refs.unknown
        corpus, manifest = build_corpus(conn, refs, why_cfg["token_budget"])
        if not manifest:
            uncorroborated += 1
            completed_labels.add(label)
            continue        # no artifacts: never invent a rationale
        try:
            claims = synthesize_why_claims(
                label, members, corpus,
                AgentClassifier(model, WHY_SYNTH_SYSTEM, max_turns=4,
                                meter=meter, stage="why-synth").ask)
        except Exception as e:              # noqa: BLE001
            click.echo(f"warning: why-synthesis failed for {label}: {e}",
                       err=True)
            errored_labels.add(label)
            continue        # keep every other group's results
        completed_labels.add(label)
        stats = ground_citations(claims, conn)
        for k in grounding:
            grounding[k] += stats[k]
        verify_why_claims(
            claims, corpus,
            AgentClassifier(model, WHY_VERIFY_SYSTEM, max_turns=4,
                            meter=meter, stage="why-verify").ask)
        all_why.extend(claims)

    # B3: re-running `why` must not silently invalidate a hand-labelled
    # why_labels.csv or leave orphan WHY-*.yaml behind. A full run (no
    # --feature) replaces the directory's entire WHY-* output, so ids stay
    # WHY-0001..N. A --feature run only replaces that feature's own
    # why-claims, leaving other features' ids (and their labels) untouched,
    # and numbers its new claims starting after the highest WHY- id already
    # in the directory. CLM-*.yaml (what-claims) is never touched.
    #
    # Issue 1 (data loss): the deletion below must never destroy prior
    # WHY-*.yaml for a scope this run produced NOTHING for. If the whole
    # run produced zero why-claims -- every group errored, every group
    # legitimately found no artifacts, or a mix -- every existing WHY-*.yaml
    # in scope is left untouched entirely (no unlink calls at all). Only
    # when the run produced at least one why-claim do we replace prior
    # output, and even then only for groups that completed (so an errored
    # group's prior claims, in a run where some other group succeeded,
    # still survive).
    def _why_num(claim_id) -> int | None:
        m = re.fullmatch(r"WHY-(\d+)", str(claim_id or ""))
        return int(m.group(1)) if m else None

    existing_why = [c for c in existing if c.layer == "why"]
    start = 1
    if all_why:
        if feature:
            # The sole group in a --feature run only reaches here (all_why
            # non-empty) if it completed, so it is always safe to replace.
            max_id = max((n for n in (_why_num(c.id) for c in existing_why)
                         if n is not None), default=0)
            for c in existing_why:
                if c.feature == feature:
                    p = out / f"{c.id}.yaml"
                    if p.is_file():
                        p.unlink()
            start = max_id + 1
        elif errored_labels:
            # Partial failure: replace only the groups that completed this
            # run; leave an errored group's prior WHY-*.yaml alone. Ids are
            # numbered past every existing id (not reset to 1) so the new
            # claims cannot collide with the preserved ones.
            for c in existing_why:
                if (c.feature or "(unlabelled)") in completed_labels:
                    p = out / f"{c.id}.yaml"
                    if p.is_file():
                        p.unlink()
            max_id = max((n for n in (_why_num(c.id) for c in existing_why)
                         if n is not None), default=0)
            start = max_id + 1
        else:
            # No group errored: a full, clean re-run replaces the entire
            # directory's WHY-* output, same as before this fix.
            for p in out.glob("WHY-*.yaml"):
                p.unlink()
            start = 1

    # Re-id uniquely across groups, then pin the copied code hypotheses.
    for i, c in enumerate(all_why, start):
        c.id = f"WHY-{i:04d}"
    known_paths = [r["path"] for r in
                   conn.execute("SELECT DISTINCT path FROM symbols")]
    pin_claims(all_why, repo, known_paths=known_paths)
    save_claims(all_why, out)

    verified = sum(1 for c in all_why if c.status == "machine_verified")
    inferred = sum(1 for c in all_why if c.corroboration == "code_inferred")
    click.echo(f"why-claims: {len(all_why)}  machine_verified: {verified}  "
               f"code_inferred: {inferred}  -> {out}/")
    click.echo(f"  groups without artifacts: {uncorroborated}  "
               f"commits outside the lake: {len(unknown_shas)}")
    click.echo(f"  citations grounded: {grounding['grounded']}  "
               f"dropped: {grounding['dropped']}")
    if not all_why:
        click.echo("  no why-claims produced this run -- prior WHY-*.yaml "
                   "left untouched")
    if errored_labels:
        click.echo(
            "  run incomplete: why-synthesis failed for feature(s) "
            f"{', '.join(sorted(errored_labels))} -- their prior "
            "WHY-*.yaml (if any) was left untouched", err=True)
    # One summary_dict feeds both surfaces so they share a single
    # billing-route probe. NOT run_cost.json: synthesize owns that name here.
    cost_summary = meter.summary_dict("why")
    click.echo(meter.format_summary("why", cost_summary))
    (out / "why_cost.json").write_text(json.dumps(cost_summary, indent=2),
                                       encoding="utf-8")

    attempted = set(groups)
    if attempted and errored_labels == attempted:
        # Every group raised: this is not a legitimate "no artifacts"
        # outcome, it is a failed run that produced nothing. Exit non-zero
        # so the failure is not mistaken for a clean, empty result.
        raise click.ClickException(
            "why-synthesis failed for every group; no why-claims were "
            "produced and existing WHY-*.yaml (if any) were left untouched")


@main.command("review")
@click.option("--claims", "claims_dir", default="claims", show_default=True,
              help="directory of claim YAML files to review")
@click.option("--config", "config_path", default=None,
              help="optional archaeon.toml; enables cluster + fan-in metadata")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
def cli_review(claims_dir, config_path, host, port):
    """Local review UI: browse + accept/edit/reject claims back into YAML."""
    import uvicorn

    from archaeon.review.server import create_app
    db = None
    if config_path:
        cfg = config_mod.load(Path(config_path))
        db = cfg["component"]["db"]
    app = create_app(claims_dir, db=db)
    click.echo(f"review UI on http://{host}:{port}  (claims: {claims_dir})")
    uvicorn.run(app, host=host, port=port)


@main.command("claims-eval")
@click.option("--claims", "claims_dir", default="claims", show_default=True)
@click.option("--labels", "labels_path", required=True,
              help="CSV: claim_id,correct (yes/no) from expert review")
def cli_claims_eval(claims_dir, labels_path):
    from archaeon.claims.claim_eval import evaluate_claims, load_labels
    from archaeon.claims.schema import load_claims
    claims = load_claims(Path(claims_dir))
    result = evaluate_claims(claims, load_labels(Path(labels_path)))
    if not result:
        click.echo("no labeled claims found")
        return
    # Pre-verification gate (all recovered claims, including contested ones)
    # vs. post-verification gate (only claims that reached machine_verified,
    # i.e. what would actually surface to a user or the guardrail — per the
    # design's "contested claims stay silent" rule). See the P1 spike
    # checklist: a synthesis batch can have real defects the verifier
    # reliably catches, so the two numbers measure different things.
    PRE_GATE, POST_GATE, CORROBORATED_GATE = 0.85, 0.95, 0.80
    for layer, s in sorted(result.items()):
        pre_mark = "PASS" if s["precision"] >= PRE_GATE else "FAIL"
        click.echo(f"{layer}-layer precision (pre-verification, all claims): "
                   f"{s['precision']:.3f} ({s['correct']}/{s['n']}) "
                   f"[gate {PRE_GATE:.2f}: {pre_mark}]")
        if s["verified_n"]:
            post_mark = "PASS" if s["verified_precision"] >= POST_GATE else "FAIL"
            click.echo(f"{layer}-layer precision (post-verification, "
                       f"machine_verified only): {s['verified_precision']:.3f} "
                       f"({s['verified_correct']}/{s['verified_n']}) "
                       f"[gate {POST_GATE:.2f}: {post_mark}]")
        # The why-layer's own gate: corroborated claims only, so a large
        # code-inferred tail can neither dilute nor inflate it. Printed
        # separately because the design bans a blended what+why number.
        if s["corroborated_n"]:
            corr_mark = "PASS" if \
                s["corroborated_precision"] >= CORROBORATED_GATE else "FAIL"
            click.echo(f"{layer}-layer precision (corroborated only): "
                       f"{s['corroborated_precision']:.3f} "
                       f"({s['corroborated_correct']}/{s['corroborated_n']}) "
                       f"[gate {CORROBORATED_GATE:.2f}: {corr_mark}]")


@main.command("check-staleness")
@config_option
@click.option("--claims", "claims_dir", default="claims", show_default=True)
def cli_check_staleness(config_path, claims_dir):
    """Report claims whose commit-pinned evidence has drifted (stale) or was
    never anchorable (unpinnable) — input to re-verification."""
    from archaeon.claims.pin import is_stale
    from archaeon.claims.schema import load_claims
    cfg = config_mod.load(Path(config_path))
    repo = Path(cfg["component"]["repo_path"])
    claims = load_claims(Path(claims_dir))
    flagged = 0
    for c in claims:
        for e in c.evidence:
            if e.role != "primary" or e.kind != "code":
                continue
            if is_stale(e, repo):
                click.echo(f"STALE       {c.id}  {e.ref}  "
                           f"@{(e.commit_sha or '')[:12]}")
                flagged += 1
            elif e.pin_status in (None, "unpinnable"):
                click.echo(f"UNPINNABLE  {c.id}  {e.ref}")
                flagged += 1
    click.echo(f"flagged: {flagged}  (claims scanned: {len(claims)})")


@main.command("stats")
@config_option
def cli_stats(config_path):
    cfg, conn = _load(config_path)
    for table in ("commits", "tickets", "prs", "pr_comments", "wiki_pages",
                  "symbols", "scan_gaps", "links", "coupling"):
        count = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        click.echo(f"{table}: {count}")
    top = strongest_pairs(conn, 5)
    if top:
        click.echo("top coupling:")
        for r in top:
            click.echo(f"  {r['path_a']} <-> {r['path_b']} "
                       f"({r['co_changes']} co-changes)")


if __name__ == "__main__":
    main()
