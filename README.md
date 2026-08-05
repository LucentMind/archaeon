# Archeon

Evidence lake (P0): recovers requirements evidence from a codebase and its
artifacts. See docs/superpowers/specs/2026-07-23-archeon-design.md.

## Scope of this repository

This repo holds the domain-neutral implementation, specs, and plans. The
validation runs that measured the gates were performed against a private
codebase; their recovered claims, evidence databases, gold label sets, and
run write-ups are not published here. Where a spec or plan cites one, it is
marked *(internal validation run — not in this repo)*. Documentation uses a
fictional `motor-ctrl` component throughout, matching
[archeon.example.toml](archeon.example.toml).

## Quickstart

    uv sync
    cp archeon.example.toml archeon.toml   # edit paths and Jira/PR settings
    set ARCHEON_JIRA_TOKEN=...             # or $env:ARCHEON_JIRA_TOKEN
    gh auth login                          # PRs use the GitHub CLI's own auth
    uv run archeon ingest-git              # scoped to component paths, bots excluded
    uv run archeon ingest-prs              # PRs that contain the component's commits
    uv run archeon ingest-jira             # only tickets referenced by those commits/PRs
    # uv run archeon ingest-wiki           # optional: local Confluence HTML export
    uv run archeon scan
    uv run archeon coupling
    uv run archeon link
    uv run archeon link-llm                # Claude Agent SDK (see Authentication)
    uv run archeon stats

## Authentication (link-llm)

`link-llm` runs the Claude Agent SDK using the **Claude CLI's own login**
(subscription/OAuth), not an API key. Set it up once:

    claude login                           # interactive, or:
    set CLAUDE_CODE_OAUTH_TOKEN=...         # from `claude setup-token` (1-year token)

`link-llm` deliberately hides `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` from
the SDK so it always uses the CLI login rather than API billing. The other
commands are unaffected — `ARCHEON_JIRA_TOKEN` and `ARCHEON_GIT_TOKEN` are
still plain API tokens for Jira and the git host.

Because of that login, the cost figures `synthesize` and `cluster` report are
**API-equivalent** (what the run would have cost on an API key), not money
charged. One honest exception: if `CLAUDE_CODE_USE_BEDROCK`,
`CLAUDE_CODE_USE_VERTEX` or `ANTHROPIC_BASE_URL` is set, the CLI may be routed
somewhere that really does bill, which archeon cannot see from the outside — so
it reports the billing route as unknown instead of claiming the run was free:
`synthesize`'s `run_cost.json` carries `"billed": null`, and `cluster` (which
writes no JSON, only the printed summary) prints the same "unknown" wording.

## Measuring link-recovery quality (P0 exit metric)

Hand-label a random sample of commits into `gold.csv`:

    sha,ticket_key
    abc123,EMB-42
    def456,

(empty ticket_key = commit genuinely has no ticket). Then compare methods —
this is the commit-level vs PR-level story:

    uv run archeon eval --gold gold.csv --method key_regex                    # commit messages only
    uv run archeon eval --gold gold.csv --method key_regex --method pr_inherited  # + PR-level (title/body/branch inherited to member commits)
    uv run archeon eval --gold gold.csv                                       # all methods (adds the llm tail)

Commit-message keys alone cover only the commits that name a ticket; most
tickets live on the PR (title, body, or branch) and are inherited to the PR's
commits, which is the large lift. Run `ingest-prs` and `link` before eval.

## Known limitations (P0)

- The git connector runs `git log --no-merges`, so a `pr->commit` link whose
  `dst_ref` is a true merge commit (the PR's `merge_commit_sha`) will not
  join to `commits.sha` and can dangle. This is harmless for the P0 exit
  metric, since `eval` only reads `commit->ticket` links.

## P1 spike: recover what-layer claims from code

The what-layer (behavior, structure, constraint values) stands on code alone, so
this needs only `scan` to have run — not the artifact links. Synthesize and
adversarially verify claims for one feature area:

    uv run archeon synthesize --feature src/motor_ctrl/thermal/ --out claims
    # writes one YAML claim per finding to claims/, each machine_verified or contested
    # plus claims/run_cost.json: the run's actual LLM cost, per stage and model

It also prints that cost block at the end (`cluster` prints one too, but has no
output directory, so it writes no file). Errored model calls — turn exhaustion,
overload — are counted there as well, and flagged, since they spend quota
without producing an answer.

Review the YAML, label each claim in a CSV (`claim_id,correct` with yes/no), then:

    uv run archeon claims-eval --claims claims --labels claim_labels.csv
    # prints what-layer precision (gate: >= 0.95)

`synthesize` uses the Claude Agent SDK (same CLI auth as `link-llm`); set
`[llm] expensive_model` for a stronger synthesis model. Full validation runbook:
[docs/p1-spike-exit-checklist.md](docs/p1-spike-exit-checklist.md) (gate:
what-layer precision ≥ 0.95).

## P1: recover why-layer claims from artifacts (Pass 2)

The why-layer — intent and rationale — cannot be settled by code. `why` walks
each what-claim's commit-pinned span back through git history to the commits
that shaped it, resolves those to their PRs and tickets, and uses those
artifacts as corroborating evidence. It needs the artifact ingest commands to
have run, not just `scan`:

    uv run archeon ingest-git
    uv run archeon ingest-prs
    uv run archeon ingest-jira
    uv run archeon synthesize --all-clusters --out claims
    uv run archeon why --claims claims
    # writes WHY-*.yaml beside the CLM-*.yaml, plus why_cost.json

Every artifact excerpt a why-claim quotes is checked **mechanically** against
the stored artifact text before any model judges it — a fabricated citation is
dropped without an LLM call. A claim whose citations all survive is marked
`corroboration: corroborated`. A claim left with no surviving artifact keeps
its code hypothesis, is marked `corroboration: code_inferred`, capped at
confidence 0.4, and is never auto-verified.

Measure the gate (corroborated why-layer precision >= 0.80) by labeling the
why-claims in a CSV (`claim_id,correct`) and reusing `claims-eval`:

    uv run archeon claims-eval --claims claims --labels why_labels.csv

Label the *rationale*, not whether the cited artifact exists — grounding
already guarantees existence. A labels file containing only `WHY-` ids reports
the why layer alone, even though the directory holds both layers.

Optional `[why]` config: `max_commits_per_span`, `token_budget`, `model`.

## Tests

    uv run pytest
