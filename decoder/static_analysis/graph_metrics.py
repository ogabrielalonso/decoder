"""Deterministic synthesis metrics: move work out of the LLM.

The synthesis workers (Bravo/Delta) currently ask the model to *infer* coupling
hotspots, god modules, and dependency health. Those are computable from data we
already have (import edges, per-file symbol counts, manifests). Computing them
in code makes them exact (no hallucination) and shrinks the generative task to
pure judgement ("why does this matter?"), which is what closes the model gap.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import Counter
from pathlib import Path

from decoder.schemas import StaticAnalysisReport


# --------------------------------------------------------------------------- #
# Coupling (fan-in / fan-out from import edges)
# --------------------------------------------------------------------------- #
def coupling_table(report: StaticAnalysisReport) -> list[dict]:
    """Per-file fan-in (incoming imports) and fan-out (outgoing), most-coupled first."""
    fan_out: Counter[str] = Counter()
    fan_in: Counter[str] = Counter()
    for edge in report.imports:
        fan_out[edge.source_file] += 1
        if edge.resolved_file:
            fan_in[edge.resolved_file] += 1
    files = set(fan_out) | set(fan_in)
    rows = [
        {"file": f, "fan_in": fan_in[f], "fan_out": fan_out[f], "total": fan_in[f] + fan_out[f]}
        for f in files
    ]
    return sorted(rows, key=lambda r: (-r["total"], r["file"]))


def god_modules(
    report: StaticAnalysisReport,
    *,
    symbol_threshold: int = 25,
    coupling_threshold: int = 15,
) -> list[dict]:
    """Files that are large (many symbols) AND/OR highly coupled."""
    coupling = {r["file"]: r["total"] for r in coupling_table(report)}
    out: list[dict] = []
    for file, n_syms in report.symbols_by_file_count.items():
        total_coupling = coupling.get(file, 0)
        if n_syms >= symbol_threshold or total_coupling >= coupling_threshold:
            out.append({"file": file, "symbols": n_syms, "coupling": total_coupling})
    return sorted(out, key=lambda r: -(r["symbols"] + r["coupling"]))


# --------------------------------------------------------------------------- #
# Dependency health (parse manifests)
# --------------------------------------------------------------------------- #
_PIN_RE = re.compile(r"[=<>~!^]")


def _pinned(spec: str) -> bool:
    return bool(_PIN_RE.search(spec or ""))


def dependency_health(source_root: Path) -> list[dict]:
    """Direct dependencies from common manifests, flagged pinned/unpinned."""
    deps: list[dict] = []
    deps += _deps_pyproject(source_root / "pyproject.toml")
    deps += _deps_requirements(source_root)
    deps += _deps_package_json(source_root / "package.json")
    return deps


def _deps_pyproject(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return []
    out: list[dict] = []
    for dep in data.get("project", {}).get("dependencies", []) or []:
        name = re.split(r"[=<>~!^\s\[]", dep, maxsplit=1)[0]
        out.append({"manifest": "pyproject.toml", "name": name, "spec": dep, "pinned": _pinned(dep)})
    return out


def _deps_requirements(root: Path) -> list[dict]:
    out: list[dict] = []
    for req in sorted(root.glob("requirements*.txt")):
        for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            name = re.split(r"[=<>~!^\s\[]", line, maxsplit=1)[0]
            out.append({"manifest": req.name, "name": name, "spec": line, "pinned": _pinned(line)})
    return out


def _deps_package_json(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out: list[dict] = []
    for section in ("dependencies", "devDependencies"):
        for name, spec in (data.get(section) or {}).items():
            # npm ranges (^, ~, *) are "unpinned"; an exact x.y.z is pinned.
            pinned = bool(re.fullmatch(r"\d+\.\d+\.\d+", str(spec).strip()))
            out.append({"manifest": "package.json", "name": name, "spec": spec, "pinned": pinned})
    return out


# --------------------------------------------------------------------------- #
# Render a precomputed-metrics block to inject into synthesis prompts
# --------------------------------------------------------------------------- #
_ENTITY_KINDS = {"class", "struct", "enum", "interface", "trait"}


def render_entity_candidates(report: StaticAnalysisReport, *, top: int = 40) -> str:
    """Candidate domain entities (classes/structs/enums/...) for Team Charlie.

    Hands the LLM the entity list from static analysis so it describes their
    domain role instead of hunting for them.
    """
    ents = [s for s in report.symbols if s.kind in _ENTITY_KINDS]
    lines = [
        "## Candidate entities (from static analysis: describe these, don't hunt)",
        "",
    ]
    if ents:
        for s in sorted(ents, key=lambda s: (s.file, s.start_line))[:top]:
            name = s.qualified_name or s.name
            # full line range so Charlie can cite `origin: file:Lx-Ly` verbatim
            lines.append(f"- `{name}` ({s.kind}) at `{s.file}`:L{s.start_line}-L{s.end_line}")
        if len(ents) > top:
            lines.append(f"- … and {len(ents) - top} more")
    else:
        lines.append(
            "_(no class/struct/enum/interface symbols detected: infer entities from code)_"
        )
    return "\n".join(lines)


_CALLABLE_KINDS = {"function", "method"}


def long_functions(report: StaticAnalysisReport, *, min_lines: int = 50) -> list[dict]:
    """Functions/methods whose line span exceeds min_lines (a size smell)."""
    out: list[dict] = []
    for s in report.symbols:
        if s.kind in _CALLABLE_KINDS:
            span = s.end_line - s.start_line + 1
            if span >= min_lines:
                out.append(
                    {
                        "name": s.qualified_name or s.name,
                        "file": s.file,
                        "start": s.start_line,
                        "end": s.end_line,
                        "lines": span,
                    }
                )
    return sorted(out, key=lambda r: -r["lines"])


def churn_hotspots(report: StaticAnalysisReport, *, top: int = 12) -> list[dict]:
    """Most-changed files from git history (commits + insertions/deletions).

    Churn is the strongest empirical predictor of defect density and is a signal
    static structure cannot reveal (it was collected but never surfaced before).
    """
    ranked = sorted(
        report.history, key=lambda h: (-h.commits, -(h.insertions + h.deletions))
    )[:top]
    return [
        {
            "file": h.file,
            "commits": h.commits,
            "churn": h.insertions + h.deletions,
            "authors": len(h.authors),
        }
        for h in ranked
    ]


def circular_deps(report: StaticAnalysisReport, *, top: int = 8) -> list[list[str]]:
    """Import cycles as strongly-connected components (>1 file). Linear-time/safe."""
    import networkx as nx

    graph: nx.DiGraph = nx.DiGraph()
    for edge in report.imports:
        if edge.resolved_file:
            graph.add_edge(edge.source_file, edge.resolved_file)
    sccs = [sorted(c) for c in nx.strongly_connected_components(graph) if len(c) > 1]
    return sorted(sccs, key=len, reverse=True)[:top]


def top_hubs(report: StaticAnalysisReport, *, top: int = 10) -> list[dict]:
    """Files most imported by others (highest fan_in): the densest hubs."""
    rows = [r for r in coupling_table(report) if r["fan_in"] > 0]
    return sorted(rows, key=lambda r: -r["fan_in"])[:top]


_TEST_MARKERS = ("test", "spec", "__tests__")


def test_file_ratio(report: StaticAnalysisReport) -> tuple[int, int]:
    """(test files, files-with-symbols): a testing-intent signal for Delta."""
    files = set(report.symbols_by_file_count) or {s.file for s in report.symbols}
    n_test = sum(1 for f in files if any(m in f.lower() for m in _TEST_MARKERS))
    return n_test, len(files)


_PATTERN_VOCAB: dict[str, set[str]] = {
    "MVC / layered": {"controller", "controllers", "router", "routes", "handler", "handlers", "view", "views"},
    "event-driven": {"event", "events", "bus", "emitter", "subscriber", "publisher", "queue", "consumer"},
    "DDD": {"repository", "repositories", "entity", "entities", "aggregate", "valueobject", "domain"},
    "hexagonal / ports-adapters": {"port", "ports", "adapter", "adapters", "usecase", "usecases", "interactor"},
    "service layer": {"service", "services", "dao", "dto", "provider", "providers"},
}


def pattern_vocabulary(report: StaticAnalysisReport) -> list[dict]:
    """Architectural-vocabulary matches in paths + import targets (candidate labels)."""
    tokens: set[str] = set()
    files = set(report.symbols_by_file_count) or {s.file for s in report.symbols}
    for blob in list(files) + [e.target for e in report.imports]:
        for part in re.split(r"[/\\._\-]", blob.lower()):
            if part:
                tokens.add(part)
    out: list[dict] = []
    for label, vocab in _PATTERN_VOCAB.items():
        hits = sorted(vocab & tokens)
        if hits:
            out.append({"pattern": label, "evidence": hits})
    return out


def render_glossary_candidates(report: StaticAnalysisReport, *, top: int = 40) -> str:
    """Domain-term skeleton for Charlie: entity names + their method names."""
    ents = [s for s in report.symbols if s.kind in _ENTITY_KINDS]
    ent_names = {e.name for e in ents}
    terms: dict[str, str] = {}
    for e in sorted(ents, key=lambda s: s.name):
        terms.setdefault(e.name, e.kind)
    for s in report.symbols:
        if s.kind == "method" and s.parent in ent_names:
            terms.setdefault(f"{s.parent}.{s.name}", "method")

    lines = ["## Candidate glossary terms (define these; drop non-domain ones)", ""]
    if not terms:
        lines.append("_(no domain entities detected: derive terms from code)_")
        return "\n".join(lines)
    for name, kind in list(terms.items())[:top]:
        lines.append(f"- `{name}` ({kind}):")
    if len(terms) > top:
        lines.append(f"- … and {len(terms) - top} more")
    return "\n".join(lines)


def render_metrics_block(report: StaticAnalysisReport, source_root: Path, *, top: int = 12) -> str:
    """Markdown the synthesis worker is GIVEN (don't recompute: just explain)."""
    coupling = coupling_table(report)[:top]
    gods = god_modules(report)[:top]
    longs = long_functions(report)
    deps = dependency_health(source_root)
    unpinned = [d for d in deps if not d["pinned"]]
    cycles = circular_deps(report)
    churn = churn_hotspots(report)

    lines = ["## Precomputed metrics (authoritative: do not recompute)", ""]

    lines.append("### Coupling (fan-in / fan-out)")
    if coupling:
        lines.append("| file | fan_in | fan_out |")
        lines.append("|---|--:|--:|")
        for r in coupling:
            lines.append(f"| `{r['file']}` | {r['fan_in']} | {r['fan_out']} |")
    else:
        lines.append("_(no import edges detected)_")
    lines.append("")

    lines.append("### Likely god modules (large and/or highly coupled)")
    if gods:
        for g in gods:
            lines.append(f"- `{g['file']}`: {g['symbols']} symbols, coupling {g['coupling']}")
    else:
        lines.append("_(none flagged)_")
    lines.append("")

    lines.append("### Dependency health")
    if deps:
        lines.append(f"- {len(deps)} direct dependencies; {len(unpinned)} unpinned.")
        for d in unpinned[:top]:
            lines.append(f"  - UNPINNED `{d['name']}` ({d['spec']}) in {d['manifest']}")
    else:
        lines.append("_(no manifests parsed)_")
    lines.append("")

    lines.append("### Long functions (>= 50 lines: size smell)")
    if longs:
        for f in longs[:top]:
            lines.append(
                f"- `{f['name']}`: {f['lines']} lines at `{f['file']}`:L{f['start']}-L{f['end']}"
            )
    else:
        lines.append("_(none over threshold)_")
    lines.append("")

    lines.append("### Circular dependencies (import cycles)")
    if cycles:
        for cyc in cycles:
            lines.append(f"- {' → '.join(f'`{c}`' for c in cyc)} → (back)")
    else:
        lines.append("_(none detected)_")
    lines.append("")

    lines.append("### Churn hotspots (most-changed files: git history)")
    if churn:
        lines.append("| file | commits | churn (ins+del) | authors |")
        lines.append("|---|--:|--:|--:|")
        for c in churn:
            lines.append(
                f"| `{c['file']}` | {c['commits']} | {c['churn']} | {c['authors']} |"
            )
    else:
        lines.append("_(no git history available)_")
    lines.append("")

    hubs = top_hubs(report)
    lines.append("### Top import targets (densest hubs by fan-in)")
    if hubs:
        for h in hubs:
            lines.append(f"- `{h['file']}`: imported by {h['fan_in']}")
    else:
        lines.append("_(none: fan-in unresolved or no intra-repo imports)_")
    lines.append("")

    n_test, n_total = test_file_ratio(report)
    pct = f"{(n_test / n_total * 100):.0f}%" if n_total else "n/a"
    lines.append(f"### Testing intent: {n_test}/{n_total} files look like tests ({pct})")
    lines.append("")

    vocab = pattern_vocabulary(report)
    lines.append("### Architectural vocabulary matches (candidate patterns: confirm/refute)")
    if vocab:
        for v in vocab:
            lines.append(f"- candidate **{v['pattern']}**: vocab: {', '.join(v['evidence'])}")
    else:
        lines.append("_(no strong vocabulary signal)_")

    return "\n".join(lines)
