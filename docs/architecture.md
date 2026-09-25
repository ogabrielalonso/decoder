# Decoder - Internal architecture

This document describes the system design. For usage, see `README.md`.

## Principles

1. **The lib is deterministic and testable.** No lib module calls Claude. Every artifact that requires an agent is generated as a prompt, consumed either by a Claude Code skill via the Task tool, or by the lib's own CLI-subprocess executors.
2. **Static analysis before agents.** The plan the lib produces already carries symbols, imports, git history. Workers start "warm" and spend tokens on synthesis, not discovery.
3. **Sequential teams, parallel workers.** Alpha fires N workers in parallel; Bravo/Charlie/Delta/QA are gates that read the previous stage's output.
4. **Zero required paid external dependency.** Local embeddings, graph in SQLite, no API key is mandatory.

## End-to-end flow

1. **Ingestion** ([decoder.ingestion](../decoder/ingestion)):
   - `cloner.resolve_source`: normalizes a GitHub URL or local path. Shallow clone (`--filter=blob:none`), cached by slug under `workspace/`.
   - `metrics.compute_metrics`: walker with pattern-ignore (`.venv`, `node_modules`, ...), per-language line counts, token estimate.
   - `tier.decide_tier`: maps LOC -> nano/small/medium/large/huge.

2. **Static analysis** ([decoder.static_analysis](../decoder/static_analysis)):
   - `parser`: tree-sitter wrapper with a `functools.cache` parser/language cache; `QueryCursor` API (0.23+).
   - `queries`: pattern-based queries per language (python, javascript, typescript, tsx, go, rust, java, kotlin) for symbols and imports.
   - `symbol_index.extract_symbols`: applies the queries and promotes `function -> method` when the body sits inside a class.
   - `dep_graph.extract_imports` + `build_graph`: a networkx `DiGraph` of dependencies.
   - `git_history.collect_history`: churn/authors/timestamps per file.
   - `graph_metrics`: deterministic facts fed to synthesis prompts: coupling table, god-modules, dependency health (parsed from `pyproject.toml`/`requirements.txt`/`package.json`), long-function detection, churn hotspots, candidate domain entities.
   - `security_scan.scan_security`: deterministic pre-scan for common security-relevant patterns, rendered as hints for the Delta (audit) worker instead of asking the model to find them unaided.
   - `pipeline.run_static_analysis`: orchestrates all of the above and produces a `StaticAnalysisReport`.

3. **Orchestration** ([decoder.orchestration](../decoder/orchestration)):
   - `chunker`: `build_file_infos` + `chunk_files` to distribute files across workers, favoring directory locality.
   - `budget`: `adapt_worker_count` expands worker count when each one would exceed `target_tokens_per_worker` (default 120k), up to `MAX_WORKERS_PER_TEAM` (300); `estimate_plan_budget` projects the cost of the full run.
   - `master.plan_from_static`: produces an `OrchestrationPlan` with 4 teams. Alpha has N workers; Bravo/Charlie/Delta each have 1 synthesis worker.
   - `prompts`: per-role templates (ALPHA_LEAD, ALPHA_WORKER, BRAVO_WORKER, CHARLIE_WORKER, DELTA_WORKER).
   - `contracts`: pydantic models `WorkerAssignment`, `TeamPlan`, `OrchestrationPlan`, versioned (`ORCHESTRATION_VERSION`).
   - `event_bus`: append-only JSONL at `<output_root>/events.jsonl`. Workers share no memory; the bus is the only async channel between passes.

4. **Execution layer** ([decoder.execution](../decoder/execution)):
   - `WorkerExecutor` (ABC): `execute(spec: WorkerSpec) -> WorkerResult`, the abstraction that lets a worker run either inside the conversation (via the skill's Task tool) or as a headless subprocess.
   - `ClaudeCliExecutor`: spawns `claude -p --model <tier> --output-format json`, authenticating through the caller's existing subscription rather than an API key; read-only tools, Write/Edit disallowed.
   - `CodexCliExecutor`: spawns `codex exec --sandbox read-only`, same read-only contract, output captured via `--output-last-message`.
   - `MockExecutor`: deterministic, network-free, used in tests.
   - `runner`: drives a full team through whichever executor is configured, with bounded parallelism and retry on malformed output.
   - `eval_harness`: runs the same worker prompt across multiple model tiers on a fixture repo and scores coverage/validation/cost/time, so a worker's default model is chosen from measurement rather than guessed.
   - This layer is what lets `decoder decode --execute` run a full decode end-to-end without spending any tokens in the calling conversation: the binary itself becomes the orchestrator, workers run on the machine's own Claude Code / Codex subscription.

5. **Knowledge layer** ([decoder.knowledge](../decoder/knowledge)):
   - `embeddings`: an `Embedder` protocol with two implementations: `LocalEmbedder` (lazy-loaded sentence-transformers) and `HashEmbedder` (deterministic bag-of-buckets, used in tests).
   - `vector_store`: persistent ChromaDB with a single collection, namespaced via the `kind` metadata field (`symbol`, `module_doc`, `file_summary`).
   - `graph_store`: SQLite with `nodes` and `edges` tables; IDs namespaced by slug (`<slug>::file::<path>`, `<slug>::sym::<path>::<name>::<line>`); exports to networkx; `neighbors(node, depth)` via bidirectional BFS.
   - `ingest.ingest_static_report`: idempotent per slug (`clear_slug` first), always populates the graph, optionally the vector store.
   - `markdown_writer.write_index`: renders the navigable `index.md`.

6. **QA** ([decoder.qa](../decoder/qa)):
   - `coverage.compute_coverage`: word-boundary regex to avoid false positives ("add" matching inside "address"); reports `file_coverage` and `symbol_coverage`.
   - `validator.validate_module_docs`: a light parser for `## \`<path>\`` plus h3 subheaders; validates required sections, minimum word counts, presence of backtick-quoted identifiers.
   - `validator.validate_team_outputs`: checks that architecture/domain/audit exist and carry citations.
   - `red_team.RED_TEAM_PROMPT`: adversarial prompt; the agent classifies each claim as verified/weak/unsupported.
   - `runner.run_qa` + `write_qa_artifacts`: produces `qa/qa_report.{json,md}`.

7. **Synthesis** ([decoder.synthesis](../decoder/synthesis)):
   - `module_structure`: deterministic renderer for the "Key symbols" and "External dependencies" sections of a module doc (signatures, docstrings, complexity/coupling risk), so the model only has to write Purpose + Notes instead of re-deriving structure it can't see reliably.
   - `api_catalog.write_api_catalog`: deterministic; groups public symbols (no `_` prefix) by file, sorted by line.
   - `executive.EXECUTIVE_SUMMARY_PROMPT`: the executive-summary prompt; under 700 words, fixed sections, every claim cited.
   - `assembler.synthesize`: writes `final_report.md` (document index + snapshot + deep-dives + how-to-read) and `api_catalog.md`; returns the executive prompt for dispatch.

8. **Decode-Compare (N-way)** ([decoder.compare](../decoder/compare)):
   - `alignment`: `align_files_multi` groups files into `FileCluster`s by coverage (how many repos contain them); `align_symbols_multi` groups symbols into `SymbolCluster`s by `(kind, name)` with a `slug -> SymbolRecord` map; `language_breakdown_multi` and `import_divergence_multi` helpers (`universal`/`majority`/`unique` buckets).
   - `matrix`: `write_matrix_artifacts` writes `summary.md` + `feature_matrix.md` (symbols × repos matrix, `file:L<line>` per cell) + `structure_matrix.md` (grouped by descending coverage).
   - `insights`: `build_insights_assignment` builds the Consolidated Insights Agent prompt, which reads the matrices plus each individual decode's output and writes `insights.md` with convergences, divergence axes, outliers, cross-repo opportunities, portfolio recommendations.
   - `pipeline.run_decode_compare(sources)`: accepts 2 to 4 sources, statically decodes each (clone + metrics + tier + symbol index), generates the matrix artifacts, ingests everything into the shared graph, returns the insights-agent prompt for dispatch via the skill.

## Contracts between the lib and the Skill

The skill (`/decode` or `/decode-compare`) never touches the lib's logic; it only:

1. Runs the CLI with `--plan-only` or `--*-prompt` to obtain text artifacts.
2. Reads `plan.json` and the other markdown files via Read.
3. Dispatches Task tool calls with the ready-made prompts (without rewriting them).
4. Writes worker outputs to the exact `output_file` paths from the plan.
5. Updates `index.md` with links to the final outputs.

This separation lets the lib be iterated on via `pytest` without running any agent, and lets the skill evolve without breaking the lib's contracts.

## Adaptive scaling

The lib implements the "Adaptive Route" by combining:

- **Pass 0**: static metrics decide the baseline tier (no tokens spent).
- **Pass 1**: `adapt_worker_count` checks whether the tier's baseline would fit the per-worker budget. If not, it expands (up to `MAX_WORKERS_PER_TEAM = 300`). If it fits, it keeps the baseline. The nano tier always stays at 1 worker.
- **Pass 2**: actual execution, whether by the skill's Task tool or by the execution layer's subprocess executors. `compute_budget` projects and shows the cost beforehand.

Override flags (`--workers`, `--target-tokens`) allow manual adjustment.

## Event bus

Since workers sharing no memory (whether via Task tool or subprocess), the bus is file-based (`events.jsonl`). The lib publishes structural events (`plan.ready`); workers can be instructed to publish their own events in future phase prompts. Consumers (later phases, QA, red team) read the log.

## Known limits

- Only 8 languages have full semantic extraction (Python, JavaScript, TypeScript, TSX, Go, Rust, Java, Kotlin). Others are counted as structure but do not enter the symbol index.
- Fuzzy file matching in compare is filename-only (not path-semantic).
- Token counting is heuristic (4 bytes/token); it does not use tiktoken.
- Red team and the executive summary depend on skill dispatch for the Task-tool path; there is no deterministic fallback if the user runs only the lib without any executor configured.
