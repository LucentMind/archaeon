# Archeon P0 — Exit Checklist (Runbook)

The goal of this checklist is to produce **one number**: the *precision* of recovered
issue↔commit links on a golden component. That number gates P1 — if the recovered evidence
graph isn't trustworthy, the P1 synthesis pipeline would be building on wrong facts, which is
the exact trust-killer the design guards against.

- **Gate:** link precision **≥ 0.80** → green-light P1. Below → improve link recovery (or
  narrow the component) and re-measure first.
- Recall is reported but **not** gated: missing links are a growth path; wrong links poison
  everything downstream.

Related docs: [design spec](superpowers/specs/2026-07-23-archeon-design.md) ·
[P0 plan](superpowers/plans/2026-07-23-p0-evidence-lake.md) ·
[market research](research/2026-07-23-market-research.md)

---

## Step 0 — Pick the golden component and line up an expert

Choose **one** medium component from the monorepo (not the whole 3M LOC), ideally one where:

- a domain expert can tell you, for a given commit, which Jira ticket it really belongs to, and
- the code sits under a clear path prefix such as `src/motor_ctrl/`.

You need that expert (possibly yourself) for Step 6 — the only labor-intensive part.

## Step 1 — Write the config

Copy the example and edit it for your component:

```bash
cp archeon.example.toml archeon.toml
```

Edit `archeon.toml` to point at your real paths and project:

```toml
[component]
name = "motor_ctrl"
db = "evidence.db"
repo_path = "C:/work/monorepo"             # the monorepo checkout root
path_prefixes = ["src/motor_ctrl/"]         # component path(s), trailing slash required
compile_db_dir = "C:/work/monorepo/build"   # dir with compile_commands.json; omit line if none

[jira]
base_url = "https://jira.yourcompany.com"
jql = "project = EMB AND component = motor_ctrl"
project_keys = ["EMB"]                       # ticket-key prefixes, e.g. EMB-123

[prs]
api_base = "https://api.github.com"          # or your GitHub Enterprise API base
repo = "org/monorepo"

[wiki]
export_dir = "C:/work/confluence_export"     # a Confluence HTML space export

[llm]
cheap_model = "claude-haiku-4-5-20251001"
max_commits = 200
```

Notes:

- `path_prefixes` entries **must** end with a trailing slash.
- If the component has no `compile_commands.json`, delete the `compile_db_dir` line — the
  scanner falls back to tree-sitter automatically.

## Step 2 — Set credentials (environment only, never in the config)

Jira Cloud API tokens (the kind you generate at id.atlassian.com) authenticate
via HTTP Basic auth as `(email, token)` — set both:

```bash
export ARCHEON_JIRA_TOKEN=...
export ARCHEON_JIRA_EMAIL=...     # the Atlassian account the token belongs to
```

Omitting `ARCHEON_JIRA_EMAIL` falls back to a bare `Authorization: Bearer`
header, which Jira Cloud rejects with a 403 (not a 401 — it doesn't look like
an auth problem at first glance). Only omit it for a Jira Data Center/Server
setup that genuinely authenticates via bearer/OAuth token.

PRs are fetched through the **GitHub CLI**, which uses its own stored auth —
no token to manage. Set it up once:

```bash
gh auth login
```

The `link-llm` step (Step 7) authenticates through the **Claude CLI's own
login** (subscription/OAuth), not an API key. Do this once:

```bash
claude login
```

Or, for a non-interactive / CI-style setup, generate a long-lived token and
export it:

```bash
export CLAUDE_CODE_OAUTH_TOKEN=$(claude setup-token)
```

Note: `link-llm` intentionally hides `ANTHROPIC_API_KEY` from the SDK, so
having that variable set will **not** switch it to API billing — it always
uses the CLI login. On PowerShell use `$env:ARCHEON_JIRA_TOKEN = "..."` etc.

## Step 3 — Ingest the artifacts

Run **in this order** — each is deterministic and re-runnable. The order matters:
PRs are discovered from the component's commits, and Jira tickets are fetched by the keys
found in those commits and PRs, so git must run first, then PRs, then Jira.

```bash
uv run archeon ingest-git
```

git is scoped to the component's `path_prefixes` and excludes bot commits (Dependabot/Renovate/
CI) by default.

```bash
uv run archeon ingest-prs
```

Fetches only the PRs that contain the component's commits (via `commits/{sha}/pulls`) — it does
not enumerate every PR in the monorepo.

```bash
uv run archeon ingest-jira
```

Discovers the ticket keys referenced by those commits/PRs/branches and fetches **only** those
tickets — so the 11 contributing Jira projects never get bulk-pulled. It prints how many keys
it discovered.

Confluence is optional and not needed for the code-first / link-recovery numbers this checklist
produces; skip it, or run `uv run archeon ingest-wiki` against a local HTML export if you have
one.

## Step 4 — Build the code graph, coupling, and heuristic links

```bash
uv run archeon scan
```

```bash
uv run archeon coupling
```

```bash
uv run archeon link
```

- `scan` prints clang vs tree-sitter file counts and a **gap count** (files it couldn't parse)
  — note it; it tells you how much of the component the parser missed.
- `link` is deterministic link recovery. It extracts ticket keys from commit messages
  (`key_regex`, confidence 1.0), from PR title/body and branch names (`key_regex` /
  `branch_regex`), and then **inherits each PR's ticket to the PR's member commits**
  (`pr_inherited`, confidence 0.9). Commit-message keys alone typically cover a minority of
  commits; the PR-level inheritance is what lifts coverage substantially (`ingest-prs` must run
  first so PR commit membership is available).

## Step 5 — Sanity-check what landed

```bash
uv run archeon stats
```

Expect non-zero `commits`, `tickets`, `prs`, `symbols`, and `links`, plus the top
change-coupled file pairs. If `tickets` or `commits` is zero, a connector didn't connect — fix
that before continuing, because the eval depends on both.

## Step 6 — Build the gold labels (the manual step)

This is the unbiased ground truth you measure against: a random sample of commits, each
labeled with the ticket it *truly* belongs to (or blank if it genuinely has none).

Get a candidate list of ~100 commits from the component's history:

```bash
git -C "C:/work/monorepo" log --no-merges --pretty=format:%H -- src/motor_ctrl/ | shuf | head -100 > sample_shas.txt
```

Create `gold.csv` with this exact header, one row per commit:

```csv
sha,ticket_key
a3f9c21b...,EMB-1042
def456...,
7c2e9a1...,EMB-1051
```

Rules:

- For each sha, you (or the expert) decide the correct ticket from the commit and the tracker
  — **not** from what Archeon guessed (that would bias the measurement).
- An **empty `ticket_key` means "this commit legitimately has no ticket."** Keep those rows —
  they are what make precision meaningful. Expect roughly half your commits to have no ticket;
  that is normal.
- ~100 commits is enough for a first read; 50 if the expert's time is tight.

## Step 7 — Run the evaluation, in arms

Commit-message keys only (the low-coverage baseline):

```bash
uv run archeon eval --gold gold.csv --method key_regex
```

Add PR-level inheritance (PR title/body/branch ticket propagated to member commits) — this is
the large lift:

```bash
uv run archeon eval --gold gold.csv --method key_regex --method pr_inherited
```

Optionally add the LLM tail (commits with no key that no PR covered):

```bash
uv run archeon link-llm
```

```bash
uv run archeon eval --gold gold.csv
```

Each `eval` prints `precision`, `recall`, `predicted`, and `gold`. (`branch_regex` is a
PR→ticket method, so it shows up in coverage via the `pr_inherited` commit links it feeds, not
as its own commit→ticket arm.)

## Step 8 — Read the number and make the call

- **Precision** = of the links Archeon asserted, the fraction that were correct.
  **This is the gate: ≥ 0.80.**
- **Recall** = of the true links, the fraction Archeon found. Report it; it does **not** gate.

Expect the jump between the first two arms to be large (commit-message keys reach only the
commits that name a ticket; PR inheritance reaches whole PRs). If the LLM arm barely moves
recall, drop it — it isn't earning its cost. Watch precision on `pr_inherited`: a mega-PR that
references a ticket but bundles unrelated commits can mis-attribute; if precision dips below
0.80, that's the likely cause.

## Step 9 — Record it and note the gaps

Write the precision/recall for each arm (`key_regex`, `+pr_inherited`, all) plus the `scan` gap
count into a dated note under `docs/research/`. That is the P0 deliverable and the baseline P1
is compared against. Note: this whole metric is a *why-layer / traceability* number — a ceiling
like ~58% is expected and fine; it does not cap the what-layer, which stands on code.

---

## Decision

| Outcome | Meaning | Next |
|---|---|---|
| precision ≥ 0.80 | evidence graph is trustworthy | green-light P1 (recovery pipeline + review UI) |
| precision < 0.80 | too many wrong links to synthesize on | improve link recovery or narrow the component, re-measure |

## Known P0 debt (won't affect this run, but be aware)

- Connectors and `link-llm` commit once at the end of their loop; a mid-run failure discards
  that run's progress. Re-running is safe (writes are idempotent).
- `scan` records unparsed files in `scan_gaps` rather than failing — check the gap count.
