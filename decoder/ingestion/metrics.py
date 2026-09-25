from __future__ import annotations

from pathlib import Path

from decoder.config import settings
from decoder.ingestion.cloner import Source
from decoder.schemas import LanguageStats, RepoMetrics
from decoder.static_analysis.languages import detect_language, is_binary
from decoder.utils.logging import get_logger

logger = get_logger(__name__)

_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB cap per file for analysis


def _should_skip(rel: Path) -> bool:
    parts = set(rel.parts)
    return bool(parts & set(settings.ignore_patterns))


def _count_lines(path: Path, byte_budget: int = _MAX_FILE_BYTES) -> tuple[int, int]:
    size = path.stat().st_size
    if size > byte_budget:
        return 0, size
    try:
        data = path.read_bytes()
    except OSError:
        return 0, size
    if b"\x00" in data[:4096]:
        return 0, size
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("latin-1")
        except UnicodeDecodeError:
            return 0, size
    return text.count("\n") + (0 if text.endswith("\n") else 1 if text else 0), size


def compute_metrics(source: Source) -> RepoMetrics:
    root = source.path
    metrics = RepoMetrics(
        root=root,
        origin_url=source.origin_url,
        commit=source.commit,
        branch=source.branch,
    )

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if _should_skip(rel):
            metrics.ignored_files += 1
            continue
        metrics.total_files += 1

        if is_binary(path.name):
            continue

        lang = detect_language(path.name) or "other"
        lines, byte_size = _count_lines(path)
        if lines == 0 and byte_size == 0:
            continue

        metrics.analyzed_files += 1
        metrics.total_lines += lines
        metrics.total_bytes += byte_size
        stats = metrics.languages.setdefault(lang, LanguageStats(language=lang))
        stats.files += 1
        stats.lines += lines
        stats.bytes += byte_size

    # Rough token estimate: 1 token ~ 4 bytes for source code.
    metrics.estimated_tokens = metrics.total_bytes // 4

    logger.info(
        "metrics: files=%d analyzed=%d lines=%d tokens~%d",
        metrics.total_files,
        metrics.analyzed_files,
        metrics.total_lines,
        metrics.estimated_tokens,
    )
    return metrics
