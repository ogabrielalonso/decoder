from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from decoder.schemas import StaticAnalysisReport, SymbolRecord


@dataclass(slots=True)
class FileCluster:
    """A file path and the set of repos (slugs) where it exists."""
    path: str
    slugs: list[str]

    @property
    def coverage(self) -> int:
        return len(self.slugs)


@dataclass(slots=True)
class SymbolCluster:
    """A (kind, name) pair and per-repo SymbolRecords that match."""
    kind: str
    name: str
    occurrences: dict[str, SymbolRecord]  # slug -> record

    @property
    def slugs(self) -> list[str]:
        return sorted(self.occurrences)

    @property
    def coverage(self) -> int:
        return len(self.occurrences)


@dataclass(slots=True)
class MultiFileAlignment:
    clusters: list[FileCluster] = field(default_factory=list)
    fuzzy_pairs: list[tuple[str, str, str, str, float]] = field(default_factory=list)
    # fuzzy_pair row: (slug_a, path_a, slug_b, path_b, similarity)


@dataclass(slots=True)
class MultiSymbolAlignment:
    clusters: list[SymbolCluster] = field(default_factory=list)


def align_files_multi(
    reports: dict[str, StaticAnalysisReport],
    *,
    fuzzy_threshold: float = 0.75,
) -> MultiFileAlignment:
    """Cluster files by path across N repos and detect fuzzy matches."""
    path_to_slugs: dict[str, list[str]] = defaultdict(list)
    for slug, report in reports.items():
        seen = {s.file for s in report.symbols}
        seen.update(e.source_file for e in report.imports)
        for path in sorted(seen):
            path_to_slugs[path].append(slug)

    clusters = [
        FileCluster(path=path, slugs=sorted(slugs))
        for path, slugs in path_to_slugs.items()
    ]
    clusters.sort(key=lambda c: (-c.coverage, c.path))

    # Fuzzy: for files appearing in only 1 repo, try to find basename matches
    # in files also appearing in only 1 (different) repo.
    singletons: list[tuple[str, str]] = [
        (c.slugs[0], c.path) for c in clusters if c.coverage == 1
    ]
    fuzzy: list[tuple[str, str, str, str, float]] = []
    used: set[tuple[str, str]] = set()
    for i, (slug_a, path_a) in enumerate(singletons):
        if (slug_a, path_a) in used:
            continue
        base_a = path_a.rsplit("/", 1)[-1]
        best: tuple[str, str, float] | None = None
        for j, (slug_b, path_b) in enumerate(singletons):
            if j == i or slug_a == slug_b:
                continue
            if (slug_b, path_b) in used:
                continue
            base_b = path_b.rsplit("/", 1)[-1]
            ratio = SequenceMatcher(None, base_a, base_b).ratio()
            if ratio >= fuzzy_threshold and (best is None or ratio > best[2]):
                best = (slug_b, path_b, ratio)
        if best is not None:
            fuzzy.append((slug_a, path_a, best[0], best[1], best[2]))
            used.add((slug_a, path_a))
            used.add((best[0], best[1]))

    return MultiFileAlignment(clusters=clusters, fuzzy_pairs=fuzzy)


def align_symbols_multi(
    reports: dict[str, StaticAnalysisReport],
) -> MultiSymbolAlignment:
    """Cluster symbols by (kind, name) across N repos.

    A symbol appearing in multiple files within the same repo collapses into
    one slot per slug (kept as the first occurrence for stability).
    """
    clusters: dict[tuple[str, str], dict[str, SymbolRecord]] = defaultdict(dict)
    for slug, report in reports.items():
        for sym in report.symbols:
            key = (sym.kind, sym.name)
            if slug not in clusters[key]:
                clusters[key][slug] = sym

    result = [
        SymbolCluster(kind=kind, name=name, occurrences=dict(occ))
        for (kind, name), occ in clusters.items()
    ]
    result.sort(key=lambda c: (-c.coverage, c.kind, c.name))
    return MultiSymbolAlignment(clusters=result)


def language_breakdown_multi(
    reports: dict[str, StaticAnalysisReport]
) -> list[tuple[str, dict[str, int]]]:
    """Return rows of (language, {slug: lines}) across all repos."""
    langs: set[str] = set()
    for report in reports.values():
        langs.update(report.metrics.languages)

    rows: list[tuple[str, dict[str, int]]] = []
    for lang in sorted(langs):
        counts: dict[str, int] = {}
        for slug, report in reports.items():
            stats = report.metrics.languages.get(lang)
            counts[slug] = stats.lines if stats else 0
        rows.append((lang, counts))
    return rows


def import_divergence_multi(
    reports: dict[str, StaticAnalysisReport]
) -> dict[str, list[str]]:
    """For each target, list which repos import it. Return divergence buckets."""
    target_to_slugs: dict[str, list[str]] = defaultdict(list)
    for slug, report in reports.items():
        for edge in report.imports:
            if slug not in target_to_slugs[edge.target]:
                target_to_slugs[edge.target].append(slug)

    out: dict[str, list[str]] = {}
    total = len(reports)
    out["universal"] = sorted(
        t for t, slugs in target_to_slugs.items() if len(slugs) == total
    )
    out["majority"] = sorted(
        t for t, slugs in target_to_slugs.items() if 1 < len(slugs) < total
    )
    out["unique"] = sorted(
        t for t, slugs in target_to_slugs.items() if len(slugs) == 1
    )
    return out
