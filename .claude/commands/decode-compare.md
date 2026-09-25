---
name: decode-compare
description: Fully decodes 2 to 4 repositories and produces an N-way comparison (feature matrix, structure matrix and a Consolidated Insights report). Use when the user asks to compare several repositories in depth (patterns, domain, audit), not just a structural diff.
---

# /decode-compare: decode N repositories and compare them

Binary: `decoder` (or `.venv/bin/decoder` if installed in this checkout).

## Input

**2 to 4 arguments**: GitHub URLs or local paths. Order matters (it defines the matrix columns).

## Contract

- Each repository goes through the **full decode** of `/decode` (four teams, QA, red
  team, synthesis, executive summary).
- At the end, the CLI builds the N-way matrices and one agent consolidates the insights.

## Flow

### Step 0: validate the input and confirm
- With only one source, point the user to `/decode` instead.
- With more than four, say the cap is four and ask which to keep.
- For three or more repositories, or an expected `large`/`huge` tier, show the
  estimated size and ask for confirmation before the deep decodes.

### Step 1: static side and initial matrices
```bash
decoder decode-compare <sourceA> <sourceB> [<sourceC>] [<sourceD>] --no-history
```
This runs only the deterministic side (clone, metrics, tier and static analysis of
each repository) and writes `docs/compare/<joined_slug>/`: `summary.md`,
`feature_matrix.md`, `structure_matrix.md` and `insights_prompt.md`.

Read `summary.md` and tell the user the repositories, sizes, tiers and the alignment
headline.

### Step 2: full decode of each repository
Run the `/decode` flow for each repository, **one at a time** (workflows must not run
concurrently; they compete for the same pool). Use the slugs shown in `summary.md`.

### Step 3: link each decode's index

For each repository, append links to `architecture.md`, `domain.md`, `audit.md`,
`qa/qa_report.md`, `red_team.md`, `executive_summary.md`, `api_catalog.md` and
`final_report.md` in `docs/decode/<slug>/index.md` (the CLI writes `index.md` in the
first step, before these files exist).

### Step 4: Consolidated Insights
```bash
decoder decode-compare --insights-prompt <sourceA> <sourceB> [<sourceC>] [<sourceD>]
```
Pass it as the `prompt` of a single Task call (`subagent_type: "general-purpose"`).
The agent reads every
decode's output plus the matrices and writes the insights; save them to
`docs/compare/<joined_slug>/insights.md`.

### Step 5: report to the user
In the user's language: repositories compared and their metrics, the paths of
`summary.md` and `insights.md`, the top three axes of divergence, and any repository
where QA or the red team flagged serious problems.

## Rules
- **Hard cap of 4 repositories.**
- **Every repository gets the full decode.** Do not skip teams or QA; the comparison
  needs complete outputs.
- **Zero tokens when avoidable:** use the static side (step 1) to validate the plan
  before spending tokens on agents.
