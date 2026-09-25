"""Execute an OrchestrationPlan via worker executors.

This is the binary-side orchestrator: instead of the /decode skill dispatching
workers on the expensive session model, the decoder runs each worker as a CLI
subprocess on a per-team-routed model, and assembles outputs itself.

Team order matters (Bravo/Charlie/Delta read Alpha's module docs), so teams run
sequentially; workers within a team run concurrently up to `max_parallel`.

Alpha is special: its workers return JSON (purpose+notes per file), which we
merge with the deterministic structure (symbols/imports) from the static report
to produce the final module docs. Other teams write their markdown directly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from decoder.execution.base import ModelTier, WorkerExecutor, WorkerResult, WorkerSpec
from decoder.orchestration.contracts import OrchestrationPlan, TeamPlan, WorkerAssignment
from decoder.schemas import StaticAnalysisReport
from decoder.synthesis.module_structure import (
    FileNarrative,
    assemble_decomposed_doc,
    parse_decomposed_alpha,
)

_ALPHA = "alpha"
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S", re.MULTILINE)


def _clean_synthesis_output(text: str) -> str:
    """Strip code fences and any preamble before the first markdown heading.

    Synthesis subprocesses sometimes wrap the doc in a ```fence``` or prepend a
    sentence like 'Now I will produce the markdown.' The artifact must be the
    pure document, so drop everything before the first heading and any enclosing
    fence.
    """
    s = text.strip()
    # 1) drop any preamble before the first markdown heading
    m = _HEADING_RE.search(s)
    if m and m.start() > 0:
        s = s[m.start() :].strip()
    # 2) drop a leading enclosing-fence line (```/```markdown)
    if s.startswith("```"):
        nl = s.find("\n")
        s = (s[nl + 1 :] if nl != -1 else "").strip()
    # 3) drop an ORPHANED closing fence: when the opening ``` lived in the
    #    preamble (stripped in step 1), the trailing ``` is left behind. An odd
    #    number of fence markers means one is unclosed -> it's that orphan, not a
    #    legitimate fenced code block (which contributes a balanced pair).
    if s.rstrip().endswith("```") and len(re.findall(r"(?m)^```", s)) % 2 == 1:
        s = s.rstrip()[:-3].rstrip()
    return s.strip()


def _looks_like_doc(text: str) -> bool:
    """A real synthesis doc has at least one markdown heading and some body."""
    return bool(_HEADING_RE.search(text)) and len(text.split()) >= 20


def clean_doc(text: str) -> tuple[str, bool]:
    """Public gate for session-dispatched docs (red_team/executive).

    The /decode skill saves a Task agent's *return* verbatim; agents add
    preambles ("Now I'll produce…") or ```fences``` despite the prompt. This
    strips them deterministically and reports whether a real document remains: so the same code gate protects the session path, not just the subprocess one.

    The "looks like a doc" signal here is heading-presence only (NOT the synthesis
    contract's word-count floor): a short-but-valid red_team/executive must not
    trip a false "agent narrated" warning. Narration has no `#` heading and is
    still caught.
    """
    cleaned = _clean_synthesis_output(text)
    return cleaned, bool(_HEADING_RE.search(cleaned))


# --------------------------------------------------------------------------- #
# Deterministic output contracts (the code gate; not prompt-dependent)
# --------------------------------------------------------------------------- #
_SUBSECTION_RE = re.compile(r"^\s{0,3}##\s+\S", re.MULTILINE)
_CITATION_RE = re.compile(r"L\d+-L\d+|:L\d+|`[^`\n]+`")
_ALPHA_MIN_COVERAGE = 0.85  # nearly every file needs a purpose: the WHOLE repo matters


def _alpha_complaints(scope: list[str], analyses: dict) -> list[str]:
    """Require a non-empty purpose for (almost) EVERY file in the chunk.

    The decode's value is the WHOLE repo: docs, configs, data, code: not just
    code. A file left without a purpose is lost coverage. The complaint NAMES the
    files still missing one, so the retry fills exactly those (cheap on Sonnet)
    instead of re-doing the whole chunk or escalating pointlessly.
    """
    if not scope:
        return []
    missing = [f for f in scope if not ((a := analyses.get(f)) and a.purpose.strip())]
    if (len(scope) - len(missing)) / len(scope) < _ALPHA_MIN_COVERAGE:
        shown = ", ".join(missing[:15])
        if len(missing) > 15:
            shown += f", +{len(missing) - 15} more"
        return [
            f"{len(missing)}/{len(scope)} files have NO purpose: write a 1-line "
            f"purpose for EVERY file (docs/configs/data count too): {shown}"
        ]
    return []


def _synthesis_complaints(doc: str) -> list[str]:
    """Reject narration, structureless, or ungrounded synthesis docs."""
    if not _looks_like_doc(doc):
        return ["no markdown body (you narrated instead of producing the document)"]
    complaints: list[str] = []
    if len(_SUBSECTION_RE.findall(doc)) < 2:
        complaints.append("fewer than 2 `##` sections; produce the full required structure")
    if not _CITATION_RE.search(doc):
        complaints.append(
            "no code citations (e.g. `path`:L10-L20 or a backticked symbol); "
            "ground every claim in the code"
        )
    return complaints


def _evaluate_worker(
    team: TeamPlan,
    worker: WorkerAssignment,
    result: WorkerResult,
    report: StaticAnalysisReport,
) -> tuple[str, list[str]]:
    """Build the doc a worker's output implies AND check it against its contract.

    Returns ``(doc, complaints)``; an empty complaint list means the doc passes
    and may be written. This is the deterministic gate: it does not trust the
    model to have obeyed the prompt.
    """
    if team.id == _ALPHA:
        analyses = parse_decomposed_alpha(result.output_text)
        doc = assemble_decomposed_doc(
            worker.scope, report.symbols, report.imports, analyses
        )
        return doc, _alpha_complaints(worker.scope, analyses)
    doc = _clean_synthesis_output(result.output_text)
    return doc, _synthesis_complaints(doc)


def _feedback_block(complaints: list[str]) -> str:
    """Append the validator's complaints to the prompt for the next attempt."""
    items = "\n".join(f"- {c}" for c in complaints)
    return (
        "\n\n## Automated validator REJECTED your previous attempt\n"
        "Your last output failed these deterministic checks. Fix every item, "
        "then output ONLY the corrected document (no apology, no preamble):\n"
        f"{items}\n"
    )


def _tier_ladder(base: ModelTier, max_retries: int) -> list[ModelTier]:
    """Attempt tiers: base (1 + retries times), then escalate to STRONG once."""
    ladder = [base] * (1 + max(0, max_retries))
    if base != ModelTier.STRONG:
        ladder.append(ModelTier.STRONG)
    return ladder


@dataclass(frozen=True)
class Route:
    """Which executor + capability tier handles a given team."""

    executor: WorkerExecutor
    tier: ModelTier


@dataclass
class ExecutionReport:
    """Outcome of executing a full plan."""

    results: list[WorkerResult] = field(default_factory=list)
    written: list[Path] = field(default_factory=list)
    retry_cost_usd: float = 0.0  # cost of superseded attempts (retries/escalations)

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd or 0.0 for r in self.results) + self.retry_cost_usd

    @property
    def failures(self) -> list[WorkerResult]:
        return [r for r in self.results if not r.ok]


def default_routing(
    cheap: WorkerExecutor,
    balanced: WorkerExecutor | None = None,
) -> dict[str, Route]:
    """Evidence-based default (RFC §14 bake-off): on a code-dense Alpha chunk
    haiku stays ~19 below Opus even with the decomposed task, but sonnet/codex
    reach Opus-grade: so every team runs at the BALANCED tier (sonnet-grade).

    Executor split honours the `--synth-executor` flag's NAME: Alpha runs on the
    primary (`cheap`) executor while synthesis (Bravo/Charlie/Delta) runs on
    `balanced`. So `--synth-executor codex` offloads ONLY synthesis to codex and
    Alpha stays on the primary (claude). When no second executor is given,
    `balanced` falls back to `cheap` and all four share one executor.
    """
    balanced = balanced or cheap
    return {
        "alpha": Route(cheap, ModelTier.BALANCED),
        "bravo": Route(balanced, ModelTier.BALANCED),
        "charlie": Route(balanced, ModelTier.BALANCED),
        "delta": Route(balanced, ModelTier.BALANCED),
    }


def strip_json_fences(text: str) -> str:
    """Remove a leading/trailing ```json fence if the model wrapped its output."""
    out = text.strip()
    out = _FENCE_RE.sub("", out)
    return _FENCE_RE.sub("", out).strip()


def parse_alpha_narratives(text: str) -> dict[str, FileNarrative]:
    """Parse an Alpha worker's JSON into per-file narratives.

    Tolerant: strips fences, and skips entries that aren't well-formed instead
    of failing the whole worker.
    """
    raw = strip_json_fences(text)
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Alpha output is not a JSON object")
    narratives: dict[str, FileNarrative] = {}
    for path, val in data.items():
        if not isinstance(val, dict):
            continue
        notes = val.get("notes") or []
        if not isinstance(notes, list):
            notes = [str(notes)]
        narratives[path] = FileNarrative(
            purpose=str(val.get("purpose", "")),
            notes=[str(n) for n in notes],
        )
    return narratives


def _spec_for(
    worker: WorkerAssignment,
    team: TeamPlan,
    plan: OrchestrationPlan,
    route: Route,
    *,
    tier: ModelTier | None = None,
    feedback: str = "",
) -> WorkerSpec:
    source_dir = Path(plan.source_path)
    extra: list[Path] = []
    # Synthesis teams read the output root (modules/ + static report).
    if team.id != _ALPHA:
        extra.append(Path(plan.output_root))
    # Alpha workers get large decomposed prompts; give them more headroom,
    # especially under quota contention with parallel decode runs.
    timeout_s = 1200 if team.id == _ALPHA else 900
    return WorkerSpec(
        id=worker.id,
        prompt=worker.prompt + feedback,
        tier=tier or route.tier,
        source_dir=source_dir,
        output_file=Path(worker.output_file),
        scope=list(worker.scope),
        extra_read_dirs=extra,
        timeout_s=timeout_s,
    )


def _write_doc(worker: WorkerAssignment, doc: str) -> Path:
    out = Path(worker.output_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    return out


@dataclass
class _WorkerOutcome:
    final: WorkerResult
    written: Path | None
    superseded_cost: float  # cost of failed attempts before the final one


def _make_outcome(
    final: WorkerResult, written: Path | None, attempts: list[WorkerResult]
) -> _WorkerOutcome:
    """Bundle a worker's outcome; superseded_cost = every attempt except `final`."""
    total = sum(a.cost_usd or 0.0 for a in attempts)
    superseded = max(0.0, total - (final.cost_usd or 0.0))
    return _WorkerOutcome(final, written, superseded)


_MAX_INFRA_RETRIES = 1  # same-tier retries for infra/throttle errors: NEVER escalate on these


def _run_worker_with_contract(
    worker: WorkerAssignment,
    team: TeamPlan,
    plan: OrchestrationPlan,
    route: Route,
    report: StaticAnalysisReport,
    *,
    max_retries: int,
    emit: Callable[[str], None],
) -> _WorkerOutcome:
    """Generate → deterministically verify → (retry|escalate) → accept or fail.

    Correctness is enforced by code (the contract in `_evaluate_worker`). Two
    failure kinds are handled DIFFERENTLY, which is what keeps a rate-limited
    subscription from spiralling:

    - CONTENT failure (the call returned, but its body fails the contract: truncated JSON, narration): feed the complaint back and escalate the model
      along the ladder (… → STRONG).
    - INFRA failure (the call itself errored/timed out: almost always a throttle
      the CLI already backed off on): do NOT escalate. Piling opus calls onto a
      throttled subscription is exactly what caused the meltdown. Retry the SAME
      tier a bounded number of times, then stop.
    """
    content_ladder = _tier_ladder(route.tier, max_retries)  # escalate on CONTENT failures only
    tier_idx = 0
    infra_retries = 0
    feedback = ""
    attempts: list[WorkerResult] = []
    best_doc: str | None = None  # best-effort doc from an OK attempt
    best_result: WorkerResult | None = None
    last_complaints: list[str] = []

    while True:
        tier = content_ladder[tier_idx]
        spec = _spec_for(worker, team, plan, route, tier=tier, feedback=feedback)
        result = route.executor.execute(spec)
        attempts.append(result)

        if not result.ok:
            # INFRA failure: retry SAME tier (bounded); never escalate the model.
            last_complaints = [result.error or "executor error"]
            if infra_retries < _MAX_INFRA_RETRIES:
                infra_retries += 1
                emit(
                    f"  worker {worker.id} infra error "
                    f"({(result.error or '')[:70]}): retry same tier ({tier}), NOT escalating"
                )
                continue
            break

        # OK result: judge its body against the deterministic contract.
        doc, complaints = _evaluate_worker(team, worker, result, report)
        best_doc, best_result = doc, result
        if not complaints:
            written = _write_doc(worker, doc)
            if tier_idx > 0 or infra_retries:
                emit(f"  worker {worker.id} recovered (tier {tier})")
            return _make_outcome(result, written, attempts)

        # CONTENT failure: feed the complaint back and escalate along the ladder.
        last_complaints = complaints
        if tier_idx < len(content_ladder) - 1:
            nxt = content_ladder[tier_idx + 1]
            note = "escalating to STRONG" if nxt != tier else "retrying"
            emit(
                f"  worker {worker.id} rejected "
                f"({'; '.join(complaints)[:100]}): {note}"
            )
            feedback = _feedback_block(complaints)
            tier_idx += 1
            continue
        break

    # Exhausted. Alpha ALWAYS writes the code-owned skeleton: assembled from
    # static data even if no model output survived (the structure stands alone);
    # the low coverage shows up as a QA warning. Synthesis narration is worthless,
    # so it fails loudly and downstream handles the gap.
    if team.id == _ALPHA:
        doc = best_doc if best_doc is not None else assemble_decomposed_doc(
            worker.scope, report.symbols, report.imports, {}
        )
        written = _write_doc(worker, doc)
        emit(
            f"  worker {worker.id} -> {written.name} "
            f"(DEGRADED after {len(attempts)} attempt(s): {'; '.join(last_complaints)[:70]})"
        )
        final = best_result or WorkerResult(id=worker.id, status="ok")
        return _make_outcome(final, written, attempts)

    final = attempts[-1] if attempts else WorkerResult(id=worker.id, status="error")
    final.status = "error"
    final.error = "contract not met: " + "; ".join(last_complaints)[:200]
    return _make_outcome(final, None, attempts)


def execute_plan(
    plan: OrchestrationPlan,
    report: StaticAnalysisReport,
    routing: dict[str, Route],
    *,
    max_parallel: int = 3,
    max_retries: int = 1,
    log: Callable[[str], None] | None = None,
) -> ExecutionReport:
    """Run every team in declaration order; workers within a team in parallel.

    Each worker runs under a deterministic output contract with bounded retries
    and a final escalation to the STRONG model (see _run_worker_with_contract).
    """
    exec_report = ExecutionReport()

    def _emit(msg: str) -> None:
        if log:
            log(msg)

    for team in plan.teams:
        route = routing.get(team.id)
        if route is None:
            _emit(f"skip team {team.id}: no route")
            continue
        _emit(f"team {team.id}: {len(team.workers)} worker(s) on {route.executor.name}/{route.tier}")

        def _run(
            worker: WorkerAssignment, team: TeamPlan = team, route: Route = route
        ) -> _WorkerOutcome:
            return _run_worker_with_contract(
                worker, team, plan, route, report, max_retries=max_retries, emit=_emit
            )

        workers = team.workers
        if max_parallel > 1 and len(workers) > 1:
            with ThreadPoolExecutor(max_workers=max_parallel) as pool:
                outcomes = list(pool.map(_run, workers))
        else:
            outcomes = [_run(w) for w in workers]

        for worker, outcome in zip(workers, outcomes, strict=True):
            exec_report.results.append(outcome.final)
            exec_report.retry_cost_usd += outcome.superseded_cost
            if outcome.written:
                exec_report.written.append(outcome.written)
                _emit(
                    f"  worker {worker.id} -> {outcome.written.name} "
                    f"({outcome.final.duration_s:.0f}s)"
                )
            else:
                _emit(f"  worker {worker.id} FAILED: {outcome.final.error}")

        # After Alpha writes all module docs, condense them into one context pack
        # so the synthesis teams read the compact overview instead of re-reading
        # every module (their prompts prefer context_pack.md).
        if team.id == _ALPHA:
            modules_dir = Path(plan.output_root) / "modules"
            if modules_dir.is_dir():
                from decoder.synthesis.context_pack import build_context_pack

                (Path(plan.output_root) / "context_pack.md").write_text(
                    build_context_pack(modules_dir), encoding="utf-8"
                )
                _emit("  built context_pack.md for synthesis teams")

    return exec_report
