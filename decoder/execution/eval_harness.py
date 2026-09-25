"""Model bake-off harness: pick worker models by EVIDENCE, quality first.

The routing defaults must be earned, not guessed. For a given worker we run each
candidate model, then rank them:

1. QUALITY GATE (primary): an Opus judge scores each output 0-100, grounded in
   the source material and against the Opus reference output (accuracy,
   completeness, insight; hallucinations penalised). Opus itself is scored too,
   as the calibration ceiling.
2. DETERMINISTIC FLOOR (sanity): symbol coverage, line citations, required
   sections (cheap objective checks the LLM judge can't fake).
3. COST / TIME (tiebreaker): only among models within `epsilon` of the Opus
   quality ceiling do we prefer the cheaper / faster one.

Nothing here calls an LLM at import; executors are injected, so the whole module
is unit-testable with MockExecutor + a stub judge.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from decoder.execution.base import ModelTier, WorkerExecutor, WorkerSpec
from decoder.execution.runner import strip_json_fences

_CITATION_RE = re.compile(r"L\d+(?:-L?\d+)?")


# --------------------------------------------------------------------------- #
# Deterministic floor metrics (no LLM)
# --------------------------------------------------------------------------- #
def symbol_coverage(output_text: str, symbol_names: list[str]) -> float:
    """Fraction of scope symbols whose name is mentioned in the output."""
    if not symbol_names:
        return 1.0
    hits = sum(1 for name in symbol_names if name and name in output_text)
    return hits / len(symbol_names)


def count_citations(output_text: str) -> int:
    """Count `Lxx` / `Lxx-Lyy` line citations."""
    return len(_CITATION_RE.findall(output_text))


def has_sections(output_text: str, required: list[str]) -> bool:
    return all(section in output_text for section in required)


# --------------------------------------------------------------------------- #
# Quality judge (LLM)
# --------------------------------------------------------------------------- #
class JudgeVerdict(BaseModel):
    overall: int = 0  # 0-100, closeness to Opus-grade quality
    accuracy: int = 0  # 0-100, grounded in the source
    completeness: int = 0
    insight: int = 0
    hallucinations: int = 0  # count of unsupported claims (lower better)
    notes: str = ""


JUDGE_PROMPT = """\
You are a STRICT evaluator of code-analysis quality. Score the CANDIDATE
analysis against the SOURCE MATERIAL and the REFERENCE (an Opus-grade analysis
of the same thing).

Worker kind: {kind}

=== SOURCE MATERIAL (ground truth: the code/data being analysed) ===
{reference_material}

=== REFERENCE ANALYSIS (Opus-grade gold standard) ===
{gold}

=== CANDIDATE ANALYSIS (to score) ===
{candidate}

Score 0-100 on each axis. `overall` = how close the candidate is to the
gold-standard's usefulness. Penalise any claim not supported by the source
(count them in `hallucinations`). Be harsh; reserve 90+ for genuinely
Opus-grade work.

Output ONLY a JSON object: {{"overall": int, "accuracy": int,
"completeness": int, "insight": int, "hallucinations": int, "notes": str}}.
"""


class QualityJudge:
    """Scores a candidate output via a strong executor (default Opus)."""

    def __init__(self, executor: WorkerExecutor, tier: ModelTier = ModelTier.STRONG):
        self.executor = executor
        self.tier = tier

    def score(
        self,
        *,
        kind: str,
        reference_material: str,
        gold: str,
        candidate: str,
        source_dir,
        timeout_s: int = 300,
    ) -> JudgeVerdict:
        prompt = JUDGE_PROMPT.format(
            kind=kind,
            reference_material=reference_material[:20000],
            gold=gold[:12000],
            candidate=candidate[:12000],
        )
        spec = WorkerSpec(
            id=f"judge-{kind}",
            prompt=prompt,
            tier=self.tier,
            source_dir=source_dir,
            output_file=source_dir / "_judge_tmp.json",
        )
        result = self.executor.execute(spec)
        if not result.ok:
            return JudgeVerdict(notes=f"judge failed: {result.error}")
        return _parse_verdict(result.output_text)


def _parse_verdict(text: str) -> JudgeVerdict:
    import json

    raw = strip_json_fences(text)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return JudgeVerdict(notes="unparseable judge output")
    if not isinstance(data, dict):
        return JudgeVerdict(notes="judge output not an object")
    return JudgeVerdict(
        overall=int(data.get("overall", 0)),
        accuracy=int(data.get("accuracy", 0)),
        completeness=int(data.get("completeness", 0)),
        insight=int(data.get("insight", 0)),
        hallucinations=int(data.get("hallucinations", 0)),
        notes=str(data.get("notes", "")),
    )


# --------------------------------------------------------------------------- #
# Bake-off
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    label: str  # e.g. "claude:haiku"
    executor: WorkerExecutor
    tier: ModelTier


class WorkerEval(BaseModel):
    worker_id: str
    candidate: str
    status: str = "ok"
    quality: int = 0
    accuracy: int = 0
    completeness: int = 0
    insight: int = 0
    hallucinations: int = 0
    symbol_coverage: float = 0.0
    citations: int = 0
    sections_ok: bool = False
    cost_usd: float | None = None
    duration_s: float = 0.0
    judge_notes: str = ""

    @property
    def model_name(self) -> str:
        return self.candidate


@dataclass
class BakeoffResult:
    evals: list[WorkerEval] = field(default_factory=list)

    def for_worker(self, worker_id: str) -> list[WorkerEval]:
        return [e for e in self.evals if e.worker_id == worker_id]


def evaluate_candidate(
    *,
    spec: WorkerSpec,
    candidate: Candidate,
    judge: QualityJudge,
    kind: str,
    reference_material: str,
    gold: str,
    symbol_names: list[str],
    required_sections: list[str],
) -> WorkerEval:
    """Run one candidate on one worker and score it (quality + floor + cost)."""
    spec = spec.model_copy(update={"tier": candidate.tier})
    result = candidate.executor.execute(spec)
    if not result.ok:
        return WorkerEval(
            worker_id=spec.id, candidate=candidate.label, status=result.status,
            judge_notes=result.error or "",
        )

    out = result.output_text
    verdict = judge.score(
        kind=kind, reference_material=reference_material, gold=gold,
        candidate=out, source_dir=spec.source_dir,
    )
    return WorkerEval(
        worker_id=spec.id,
        candidate=candidate.label,
        status="ok",
        quality=verdict.overall,
        accuracy=verdict.accuracy,
        completeness=verdict.completeness,
        insight=verdict.insight,
        hallucinations=verdict.hallucinations,
        symbol_coverage=symbol_coverage(out, symbol_names),
        citations=count_citations(out),
        sections_ok=has_sections(out, required_sections),
        cost_usd=result.cost_usd,
        duration_s=result.duration_s,
        judge_notes=verdict.notes,
    )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def aggregate_evals(runs: list[WorkerEval]) -> WorkerEval:
    """Average N repeated evals of the SAME candidate to beat judge variance.

    Quality/sub-scores are averaged; the spread (min-max quality) is recorded in
    judge_notes so we can see how noisy the judge was for that candidate.
    """
    ok = [e for e in runs if e.status == "ok"]
    if not ok:
        return runs[0]
    base = ok[0]
    qualities = [e.quality for e in ok]
    costs = [e.cost_usd for e in ok if e.cost_usd is not None]
    return WorkerEval(
        worker_id=base.worker_id,
        candidate=base.candidate,
        status="ok",
        quality=round(_mean(qualities)),
        accuracy=round(_mean([e.accuracy for e in ok])),
        completeness=round(_mean([e.completeness for e in ok])),
        insight=round(_mean([e.insight for e in ok])),
        hallucinations=round(_mean([e.hallucinations for e in ok])),
        symbol_coverage=base.symbol_coverage,
        citations=base.citations,
        sections_ok=base.sections_ok,
        cost_usd=_mean(costs) if costs else None,
        duration_s=_mean([e.duration_s for e in ok]),
        judge_notes=f"n={len(ok)} quality {min(qualities)}-{max(qualities)} (spread {max(qualities) - min(qualities)})",
    )


def pick_winner(evals: list[WorkerEval], *, epsilon: int = 5) -> WorkerEval | None:
    """Quality first: take the top quality score, then among those within
    `epsilon` of it, pick the cheapest (then fastest)."""
    ok = [e for e in evals if e.status == "ok"]
    if not ok:
        return None
    ceiling = max(e.quality for e in ok)
    contenders = [e for e in ok if e.quality >= ceiling - epsilon]
    return min(
        contenders,
        key=lambda e: (e.cost_usd if e.cost_usd is not None else float("inf"), e.duration_s),
    )


def format_matrix(result: BakeoffResult) -> str:
    """Markdown matrix, grouped by worker, with the winner flagged."""
    lines: list[str] = ["# Model bake-off: quality first", ""]
    worker_ids: list[str] = []
    for e in result.evals:
        if e.worker_id not in worker_ids:
            worker_ids.append(e.worker_id)
    for wid in worker_ids:
        evals = result.for_worker(wid)
        winner = pick_winner(evals)
        lines.append(f"## Worker `{wid}`")
        lines.append("")
        lines.append("| model | quality | acc | compl | insight | halluc | sym cov | cites | $ | time | |")
        lines.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|:--|")
        for e in sorted(evals, key=lambda e: -e.quality):
            mark = "🏆" if winner and e.candidate == winner.candidate else ""
            cost = f"{e.cost_usd:.4f}" if e.cost_usd is not None else "-"
            lines.append(
                f"| {e.candidate} | {e.quality} | {e.accuracy} | {e.completeness} | "
                f"{e.insight} | {e.hallucinations} | {e.symbol_coverage:.0%} | "
                f"{e.citations} | {cost} | {e.duration_s:.0f}s | {mark} |"
            )
        if winner:
            lines.append("")
            lines.append(
                f"**Winner: `{winner.candidate}`** "
                f"(quality {winner.quality}, ${winner.cost_usd if winner.cost_usd is not None else 'n/a'})"
            )
        lines.append("")
    return "\n".join(lines)
