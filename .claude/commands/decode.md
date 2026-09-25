---
name: decode
description: Fully decodes a repository (GitHub URL or local path) of any size, autonomously. The `decoder` CLI does everything deterministic (static analysis, worker auto-sizing, facts injected into prompts, module assembly, QA, synthesis); the generative workers (Alpha, Bravo, Charlie, Delta) run in parallel through the `decode-execute` workflow, each writing its output to disk. The red-team review and the executive summary run in the main loop. Use when the user asks to analyze, decode or deeply understand a repository.
---

# /decode: decode a repository (autonomous, any size)

**Design principle:** the user only passes the link. Do not ask how many workers,
which model or how to run it. That intelligence lives in `decoder` (auto-sizing) and
in this flow.

- **`decoder`** does everything deterministic, at zero generation cost: static
  analysis, **worker auto-sizing** (each worker gets at most ~120k tokens of content
  and at most 100 files, so it neither truncates its output nor overflows its
  context), facts injected into the prompts, module assembly, QA and synthesis. If
  `decode` warns that a worker is above ~70% of its window, that is one giant file;
  continue.
- **Generative workers** run through the **`decode-execute`** workflow
  (`.claude/workflows/decode-execute.js`): parallel, with bounded concurrency and a
  retry. Each worker reads its prompt from disk and **writes its output to disk**, so
  the main context does not grow.
- In the workflow, keep infrastructure backoff (rate limit, timeout, `is_error`)
  separate from escalating to a stronger model for quality: different causes,
  different handling.
- **Huge repositories (~1M+ lines):** one decode can exhaust a usage cap midway. The
  missing workers can be re-run after the cap resets (the Alpha workflow is
  idempotent), then `assemble-alpha` and continue. Do not run several large decodes
  in parallel; they compete for the same cap.
- Only **two strong generative calls** happen in the main loop: the red team and the
  executive summary.

Artifacts go to `docs/decode/<slug>/`, relative to the current directory. Binary:
`decoder` (or `.venv/bin/decoder` if installed in this checkout). **Run every step
from the same directory.**

## Input

A GitHub URL (browser URLs with `/tree`, `/blob`, `/pull` are normalized) or a local path.

**Pre-flight:** `/decode` is for a codebase (symbols, imports, dependency graph). If
the target is a single prose or markdown file, do not run the decoder; just read it.

**Workspace with several cloned repos:** clone the target locally and pass the local
path instead of the GitHub URL. A URL can silently resolve to the wrong slug.

## Flow

### Step 1: static analysis and an auto-sized plan (deterministic, no LLM, no flags)
```bash
decoder decode <source> --no-history
```
Clones (if a URL), runs static analysis and writes `static/report.json`, `plan.json`,
`index.md`, `assets.md`. **Do not pass `--target-tokens`, `--workers` or
`--alpha-max-workers`**; the defaults size correctly from nano to huge. The output
prints the tier and the `<slug>`: **capture the `<slug>`**. If the tier is `huge`,
tell the user it will take a while and continue without asking.

### Step 2: dump the prompts and the manifest (deterministic)
```bash
decoder dump-prompts <slug>
```
Writes `prompts/<id>.txt` (each prompt with its facts injected) and
`prompts/manifest.json` (id, team, prompt, output, kind per worker). Keep the
**absolute** path of the manifest: `<CWD>/docs/decode/<slug>/prompts/manifest.json`.

### Step 3: Team Alpha through the workflow (parallel, writes `.raw.json`)

> **Concurrency rule:** never launch several workflows at once (neither partitions of
> the same decode nor decodes of different repos); they compete for the same pool and
> starve each other. One workflow at a time; before launching the next, check real
> progress on disk (count the written outputs against the manifest), not the return text.

```
Workflow({ name: "decode-execute",
           args: { manifestPath: "<ABS>/prompts/manifest.json", kind: "alpha" } })
```
If the workflow cannot be resolved by name, pass
`scriptPath: "<ABS path of this checkout>/.claude/workflows/decode-execute.js"`.
If the Workflow tool is not available in your Claude Code, read the manifest and
dispatch each `kind: "alpha"` entry with the Task tool in parallel batches, giving
each agent the same instruction the workflow uses (read the prompt file, do the work,
write the output verbatim to the `output` path).

Each Alpha agent reads its prompt and its files and writes the decomposed JSON to its
`.raw.json`. Then assemble:

### Step 4: assemble the modules and the context pack (deterministic)
```bash
decoder assemble-alpha <slug>
decoder build-context-pack <slug>
```
`assemble-alpha` reports `assembled N` and `Y skeleton-only`. **If `Y > 0`** (a worker
wrote empty or invalid JSON), re-run step 3 (idempotent) and `assemble-alpha` again.
At most two attempts, then continue. `build-context-pack` condenses the modules for
synthesis.

### Step 5: Bravo, Charlie and Delta through the workflow (parallel, writes `.md`)
```
Workflow({ name: "decode-execute",
           args: { manifestPath: "<ABS>/prompts/manifest.json", kind: "synth" } })
```
The three agents read `context_pack.md`, `static/report.json` and the repository, and
write `architecture.md`, `domain.md` and `audit.md` verbatim.

**Test fixtures:** an incomplete entity in a fixture reflects minimal test intent,
not a real omission; mark it `unknown` instead of reporting a failure.
**Multi-language repos:** make parallels between equivalent entities explicit (the
same entity implemented in different languages).

### Step 6: QA (deterministic)
```bash
decoder qa run <slug>
```
Shows three metrics: **file coverage**, **symbol coverage (listed)** and **symbol
description (depth)**. The last one is the real measure: a symbol described, not
just listed.

**Self-healing weak modules:** if QA lists **weak modules** (symbols listed but under
60% described), re-dispatch **only those workers**:
1. For each weak `alpha-worker-NN`, the worker id is `alpha-N` (drop the leading
   zero: `alpha-worker-03` becomes `alpha-3`).
2. `Workflow({ name: "decode-execute", args: { manifestPath: "<ABS>/prompts/manifest.json", kind: "alpha", ids: ["alpha-17", ...] } })`.
3. `decoder assemble-alpha <slug>`, then `decoder qa run <slug>`. Repeat at most
   twice. If a worker stays weak, its files are genuinely dense: split them finer.

Also re-dispatch (step 3, weak ids) if files are under 90%, listed symbols under 70%,
or there are `error` issues. Symbols in test files do not need descriptions.

### Step 7: red team (strong model = you, main loop)
```bash
decoder qa red-team-prompt <slug>
```
Do not dispatch a subagent for this. **You** run the prompt from stdout: open the
files it asks for, check the claims adversarially against the code, and write your
answer **verbatim** to `docs/decode/<slug>/red_team.md`; then
`decoder doc-clean docs/decode/<slug>/red_team.md`.

### Step 8: synthesis (deterministic) and the executive summary (you, main loop)
```bash
decoder synthesize <slug>
decoder synthesize <slug> --executive-prompt
```
For `--executive-prompt`: **you** read the prompt from stdout and the documents it
cites, and write `docs/decode/<slug>/executive_summary.md` verbatim; then
`decoder doc-clean docs/decode/<slug>/executive_summary.md`.

**Synthesis caveat:** synthesis is lossy and can drop medium-severity findings and
legal or license constraints. For serious decisions (build vs buy, architecture),
read `modules/*.md` directly.

### Step 9: report and clean up
Summarize for the user, in their language: slug, tier, number of workers, generated
files, path to `index.md` (and `final_report.md`), QA coverage, and any worker that needed a
re-dispatch. Then:
```bash
decoder cleanup <slug>
```
Deletes the clone under `workspace/<slug>/` (the output stays). Safe: it only removes
a clone the decoder downloaded; a local path of yours is never touched.

## Rules
- **Do not ask the user** about sizing, models or execution. **Do not pass tuning
  flags** to `decoder decode`.
- **Generative workers go through the `decode-execute` workflow** (parallel, retry,
  write to disk). Do not dispatch dozens of Alpha workers by hand one by one.
- **Fan-out resilience:** if the parallel workflow is rejected, fall back to a direct
  sequential pass instead of stalling.
- **Deterministic work stays in `decoder`**: static analysis, auto-sizing,
  dump-prompts, assemble-alpha, build-context-pack, qa, synthesize. Dispatch prompts
  **verbatim**.
- **Same directory for every step.** The slug resolves `docs/decode/<slug>/`
  relative to it.
- **Cover 100% of the repository**: every text file becomes a module; binaries and
  images go to `assets.md`. If QA reports a gap, re-run the Alpha workflow.
- Before re-decoding a repository: delete `.decoder_cache/graph.sqlite` in the
  current directory and the old output `docs/decode/<slug>/` (exact slug path, never
  a glob).
