from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from decoder.compare.insights import InsightsAssignment, build_insights_assignment
from decoder.compare.matrix import MatrixArtifacts, write_matrix_artifacts
from decoder.config import settings
from decoder.ingestion.cloner import _slug_from_url, resolve_source
from decoder.ingestion.metrics import compute_metrics
from decoder.ingestion.tier import decide_tier
from decoder.knowledge.graph_store import GraphStore
from decoder.knowledge.ingest import ingest_static_report
from decoder.schemas import StaticAnalysisReport
from decoder.static_analysis.pipeline import run_static_analysis
from decoder.utils.logging import get_logger

logger = get_logger(__name__)

MAX_REPOS = 4


@dataclass(slots=True)
class RepoSide:
    slug: str
    source_path: Path
    out_dir: Path
    report: StaticAnalysisReport


@dataclass(slots=True)
class DecodeCompareArtifacts:
    slugs: list[str]
    compare_root: Path
    sides: list[RepoSide]
    matrix: MatrixArtifacts
    insights_prompt_file: Path
    insights_output_file: str


def _decode_side_static(source: str, *, include_history: bool) -> RepoSide:
    src = resolve_source(source)
    slug = _slug_from_url(source) if src.kind == "git" else src.path.name
    out_dir = (settings.output_dir / "decode" / slug).resolve()
    (out_dir / "static").mkdir(parents=True, exist_ok=True)

    metrics = compute_metrics(src)
    tier = decide_tier(metrics)
    report = run_static_analysis(src.path, metrics, tier, include_history=include_history)
    (out_dir / "static" / "report.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8"
    )
    return RepoSide(slug=slug, source_path=src.path, out_dir=out_dir, report=report)


def _compare_pair_slug(slugs: list[str]) -> str:
    if len(slugs) == 2:
        return f"{slugs[0]}__vs__{slugs[1]}"
    return "__".join(slugs)


def run_decode_compare(
    sources: list[str], *, include_history: bool = False
) -> DecodeCompareArtifacts:
    if not (2 <= len(sources) <= MAX_REPOS):
        raise ValueError(
            f"decode-compare accepts 2 to {MAX_REPOS} repositories; got {len(sources)}"
        )

    sides = [_decode_side_static(s, include_history=include_history) for s in sources]
    slugs = [s.slug for s in sides]
    reports = {s.slug: s.report for s in sides}

    compare_root = (settings.output_dir / "compare" / _compare_pair_slug(slugs)).resolve()
    compare_root.mkdir(parents=True, exist_ok=True)

    matrix = write_matrix_artifacts(slugs, reports, compare_root)

    # Persist each side in the shared graph so cross-repo queries work.
    graph_store = GraphStore(settings.cache_dir / "graph.sqlite")
    for side in sides:
        ingest_static_report(side.slug, side.report, graph_store)

    insights: InsightsAssignment = build_insights_assignment(
        slugs=slugs,
        compare_root=compare_root,
        decode_root=(settings.output_dir / "decode").resolve(),
    )
    insights_prompt_file = compare_root / "insights_prompt.md"
    insights_prompt_file.write_text(insights.prompt, encoding="utf-8")

    logger.info(
        "decode-compare: N=%d slugs=%s compare_root=%s",
        len(slugs),
        slugs,
        compare_root,
    )

    return DecodeCompareArtifacts(
        slugs=slugs,
        compare_root=compare_root,
        sides=sides,
        matrix=matrix,
        insights_prompt_file=insights_prompt_file,
        insights_output_file=insights.output_file,
    )
