"""Deterministic rendering of per-file module structure.

The Alpha worker template has four sections per file: Purpose, Key symbols,
External dependencies, Notes. Two of them: Key symbols and External
dependencies: are pure restatements of data the static analysis already
produced (symbols, imports). Rendering them in code removes that work from the
LLM (cheaper, faster, exactly consistent with api_catalog.md) and leaves the
model only the genuinely generative parts: Purpose and Notes.

See docs/design/2026-05-31-execucao-custo-otimizado.md (Alavanca A / Fase 1).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from pydantic import BaseModel, Field

from decoder.schemas import ImportEdge, SymbolRecord

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


class FileNarrative(BaseModel):
    """The generative half of a module doc, produced by the LLM per file."""

    purpose: str = ""
    notes: list[str] = []


def group_symbols_by_file(
    symbols: list[SymbolRecord], scope: set[str]
) -> dict[str, list[SymbolRecord]]:
    by_file: dict[str, list[SymbolRecord]] = defaultdict(list)
    for sym in symbols:
        if sym.file in scope:
            by_file[sym.file].append(sym)
    return by_file


def group_imports_by_file(
    imports: list[ImportEdge], scope: set[str]
) -> dict[str, list[ImportEdge]]:
    by_file: dict[str, list[ImportEdge]] = defaultdict(list)
    for edge in imports:
        if edge.source_file in scope:
            by_file[edge.source_file].append(edge)
    return by_file


def render_key_symbols(symbols: list[SymbolRecord]) -> str:
    """Render the body of '### Key symbols' from static data alone."""
    if not symbols:
        return "_(none detected)_"
    lines: list[str] = []
    for s in sorted(symbols, key=lambda s: s.start_line):
        name = s.qualified_name or s.name
        lines.append(f"- `{name}` ({s.kind}, L{s.start_line}-L{s.end_line})")
    return "\n".join(lines)


def render_external_deps(imports: list[ImportEdge]) -> str:
    """Render the body of '### External dependencies' from static data alone."""
    if not imports:
        return "_(none detected)_"
    # de-dupe (target, kind) preserving first-seen order
    seen: dict[tuple[str, str], None] = {}
    for e in imports:
        seen.setdefault((e.target, e.kind), None)
    return "\n".join(f"- `{target}` ({kind})" for target, kind in seen)


def render_file_block(
    rel_path: str,
    symbols: list[SymbolRecord],
    imports: list[ImportEdge],
    narrative: FileNarrative | None = None,
) -> str:
    """Render one file's full markdown block.

    Key symbols / External dependencies are always deterministic. Purpose and
    Notes come from `narrative` when available, otherwise emit placeholders so
    the skeleton is still valid markdown (useful for tests / dry runs).
    """
    purpose = (narrative.purpose if narrative else "").strip() or "_(pending)_"
    notes = narrative.notes if narrative else []

    parts = [
        f"## `{rel_path}`",
        "",
        "### Purpose",
        purpose,
        "",
        "### Key symbols",
        render_key_symbols(symbols),
        "",
        "### External dependencies",
        render_external_deps(imports),
    ]
    if notes:
        parts += ["", "### Notes", *(f"- {n}" for n in notes)]
    return "\n".join(parts)


def assemble_module_doc(
    scope: list[str],
    symbols: list[SymbolRecord],
    imports: list[ImportEdge],
    narratives: dict[str, FileNarrative] | None = None,
) -> str:
    """Assemble a full module doc for a worker scope, in scope order."""
    scope_set = set(scope)
    sym_by_file = group_symbols_by_file(symbols, scope_set)
    imp_by_file = group_imports_by_file(imports, scope_set)
    narratives = narratives or {}

    blocks = [
        render_file_block(
            rel,
            sym_by_file.get(rel, []),
            imp_by_file.get(rel, []),
            narratives.get(rel),
        )
        for rel in scope
    ]
    return "\n\n---\n\n".join(blocks) + "\n"


# --------------------------------------------------------------------------- #
# Decomposed Alpha task: close the steps so cheaper models match Opus.
# (Validated on a real-world fixture repo: vs the open task, codex gpt-5.5 +13, sonnet +4.)
# --------------------------------------------------------------------------- #
# tight-coupling and complexity are NOT here on purpose: they are computed
# deterministically from the import graph and symbol spans and injected at
# assembly time (see _deterministic_risks), so the LLM never re-judges a fact
# the code already owns. The LLM only flags risks that need reading judgment.
_RISK_TYPES = (
    "no-error-handling",
    "unvalidated-input",
    "hardcoded-config",
    "blocking-io",
    "security",
)

_LONG_FN_MIN = 50
_COUPLING_RISK_MIN = 10
_CALLABLE_KINDS = {"function", "method"}


def _deterministic_risks(
    scope: list[str],
    symbols: list[SymbolRecord],
    imports: list[ImportEdge],
) -> dict[str, list[dict]]:
    """Code-owned risks (tight-coupling, complexity) merged in at assembly time.

    Computed from the resolved import graph (fan-in/out) and symbol spans, so they
    are exact and consistent across every worker regardless of model: instead of
    the LLM re-deriving what static analysis already measured.
    """
    from collections import Counter, defaultdict

    scope_set = set(scope)
    fan_out: Counter[str] = Counter()
    fan_in: Counter[str] = Counter()
    for e in imports:
        fan_out[e.source_file] += 1
        if e.resolved_file:
            fan_in[e.resolved_file] += 1

    out: dict[str, list[dict]] = defaultdict(list)
    for f in scope_set:
        total = fan_in[f] + fan_out[f]
        if total >= _COUPLING_RISK_MIN:
            out[f].append(
                {
                    "type": "tight-coupling",
                    "evidence": f"fan_in={fan_in[f]} fan_out={fan_out[f]}",
                    "note": "high coupling per static analysis",
                }
            )
    for s in symbols:
        if s.file in scope_set and s.kind in _CALLABLE_KINDS:
            span = s.end_line - s.start_line + 1
            if span >= _LONG_FN_MIN:
                out[s.file].append(
                    {
                        "type": "complexity",
                        "evidence": f"{s.qualified_name or s.name} L{s.start_line}-L{s.end_line} ({span} lines)",
                        "note": "exceeds 50-line size threshold",
                    }
                )
    return out

_DECOMPOSED_ALPHA_TEMPLATE = """\
You document code files by filling CLOSED slots. The static analyzer already
extracted every symbol (authoritative: do not re-derive or invent symbols).

Repository root (read-only): {source_path}

For each file below, read it and answer ONLY these closed questions:
- purpose: <=25 words, what the file is for.
- symbols: for EACH listed symbol, <=15 words on what it does, grounded in the
  code at its line range. Do not add or rename symbols.
- risks: zero or more items, each with a `type` from this FIXED set:
  [{risk_types}] plus `evidence` (a Lxx line) and a <=15-word `note`. [] if none.

Files and their symbols:
{files_block}

Output ONLY a JSON object mapping each relative path to
{{"purpose": str, "symbols": {{"<name>": str, ...}}, "risks": [{{"type": str, "evidence": str, "note": str}}]}}.
Include EVERY file listed above: do NOT skip any, including docs, configs, and
data files (give those a 1-line purpose; their "symbols" is just {{}}). No prose,
no markdown fences.
"""


class FileAnalysis(BaseModel):
    """The generative half of a decomposed Alpha output, per file."""

    purpose: str = ""
    symbols: dict[str, str] = Field(default_factory=dict)  # name -> behaviour
    risks: list[dict] = Field(default_factory=list)  # {type, evidence, note}


def _alpha_symbol_line(s: SymbolRecord) -> str:
    """One pre-listed symbol for the Alpha prompt: structure + (code-extracted)
    signature and docstring, so the LLM describes behavior without re-deriving them."""
    sig = f" {s.signature}" if s.signature else ""
    line = f"  - {s.kind} `{s.qualified_name or s.name}`{sig} (L{s.start_line}-L{s.end_line})"
    if s.docstring:
        line += f": doc: {s.docstring}"
    return line


def build_decomposed_alpha_prompt(
    scope: list[str],
    symbols: list[SymbolRecord],
    source_path: str,
) -> str:
    """Build a fully-closed Alpha prompt: symbols pre-listed, slots to fill."""
    by_file = group_symbols_by_file(symbols, set(scope))
    blocks: list[str] = []
    for rel in scope:
        syms = by_file.get(rel, [])
        if syms:
            sym_lines = "\n".join(_alpha_symbol_line(s) for s in sorted(syms, key=lambda s: s.start_line))
        else:
            sym_lines = "  (no symbols: config/doc file)"
        blocks.append(f"### `{rel}`\n{sym_lines}")
    return _DECOMPOSED_ALPHA_TEMPLATE.format(
        source_path=source_path,
        risk_types=", ".join(_RISK_TYPES),
        files_block="\n\n".join(blocks),
    )


def _extract_json_object(s: str) -> str | None:
    """Best-effort extraction of a parseable JSON object from messy LLM output.

    Handles two real failure modes seen on large repos:
    - prose/markdown before or after the object ("Extra data" errors): returns
      the first brace-balanced ``{...}``.
    - truncated output (worker hit an output/token limit mid-stream): salvages
      the prefix up to the last *complete* top-level entry and closes it, so we
      keep the files that did serialize instead of losing the whole worker.

    Returns ``None`` only when nothing usable can be recovered.
    """
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = esc = False
    last_entry_end = -1  # index of the comma separating completed top-level entries
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]  # balanced object complete
        elif c == "," and depth == 1:
            last_entry_end = i
    # truncated: rebuild from the complete top-level entries we did capture
    if last_entry_end > start:
        return s[start:last_entry_end] + "}"
    return None


def _loads_lenient(raw: str) -> object:
    """Load JSON tolerantly; never raises (returns ``{}`` when unrecoverable)."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    obj = _extract_json_object(raw)
    if obj is None:
        return {}
    try:
        return json.loads(obj)
    except json.JSONDecodeError:
        return {}


def parse_decomposed_alpha(text: str) -> dict[str, FileAnalysis]:
    """Parse the decomposed Alpha JSON into per-file analyses.

    Tolerant by design: strips fences, salvages the JSON object out of any
    surrounding prose, recovers truncated output, and NEVER raises. On
    unparseable output it returns whatever it could salvage (possibly empty) so
    the caller still assembles the code-owned skeleton instead of losing the
    whole worker.
    """
    raw = _FENCE_RE.sub("", text.strip())
    raw = _FENCE_RE.sub("", raw).strip()
    data = _loads_lenient(raw)
    if not isinstance(data, dict):
        return {}
    out: dict[str, FileAnalysis] = {}
    for path, val in data.items():
        if not isinstance(val, dict):
            continue
        syms = val.get("symbols") or {}
        risks = val.get("risks") or []
        out[path] = FileAnalysis(
            purpose=str(val.get("purpose", "")),
            symbols=_normalize_symbol_keys(syms) if isinstance(syms, dict) else {},
            risks=[r for r in risks if isinstance(r, dict)],
        )
    return out


_KIND_PREFIX = re.compile(
    r"^(?:async\s+|export\s+|public\s+|private\s+|static\s+)*"
    r"(?:type|class|function|func|fn|def|method|interface|enum|struct|const|let|var)\s+",
    re.IGNORECASE,
)
_LEADING_IDENT = re.compile(r"([A-Za-z_$][\w$.]*)")


def _extract_symbol_name(key: str) -> str:
    """Recover the bare symbol name from however an agent decorated the key.

    Handles: a backticked token ("interface `Foo` (L1-L2)" -> Foo), a kind prefix
    ("type RequestInterceptor" -> RequestInterceptor), and trailing signatures /
    line-ranges ("function bar ({a,b}) (L5-L9)" -> bar). Falls back to the raw key.
    """
    k = key.strip()
    m = re.search(r"`([^`]+)`", k)
    if m:
        k = m.group(1).strip()
    k = _KIND_PREFIX.sub("", k)
    m = _LEADING_IDENT.match(k)
    return m.group(1) if m else key.strip()


def _normalize_symbol_keys(syms: dict) -> dict[str, str]:
    """Index each symbol description under BOTH its raw key and its bare symbol name.

    Agents key descriptions inconsistently: by the whole decorated prompt line
    ("interface `Foo` (L15-L20)"), by a kind prefix ("type RequestInterceptor"), or
    with a multi-line signature (TS destructuring). Assembly matches by the static
    name/qualified_name, so we also index by the recovered bare name so those
    descriptions attach instead of rendering symbols bare (the silent depth hole).
    """
    norm: dict[str, str] = {}
    for k, v in syms.items():
        key, desc = str(k), str(v)
        norm[key] = desc
        name = _extract_symbol_name(key)
        if name and name != key:
            norm.setdefault(name, desc)
    return norm


def render_decomposed_file_block(
    rel_path: str,
    symbols: list[SymbolRecord],
    imports: list[ImportEdge],
    analysis: FileAnalysis | None,
    extra_risks: list[dict] | None = None,
) -> str:
    """Render one file: code-owned structure + LLM per-symbol descriptions + risks.

    `extra_risks` are the deterministic, code-owned risks (tight-coupling /
    complexity) injected ahead of the LLM-judged ones.
    """
    purpose = (analysis.purpose if analysis else "").strip() or "_(pending)_"
    parts = [f"## `{rel_path}`", "", "### Purpose", purpose, "", "### Key symbols"]
    if symbols:
        # Locally-scoped helpers (e.g. a `FakeProcess` class or `fake_fetch` closure
        # redefined inside many test functions) share one bare name across many
        # distinct occurrences. The static analyzer rarely computes a qualified_name
        # for these (tree-sitter scope tracking gap), so a plain bare-name lookup can
        # only ever surface ONE description and silently blanks every other instance.
        # Agents frequently DO disambiguate their own JSON keys ("test_x.FakeProcess"),
        # so consume those qualified variants one-per-occurrence (file order) instead
        # of only ever matching the bare name.
        used_desc_keys: set[str] = set()

        def _pick_description(sym_name: str, qualified: str | None) -> str:
            if not analysis:
                return ""
            suffix = f".{sym_name}"
            candidates = [k for k in analysis.symbols if k == sym_name or k.endswith(suffix)]
            if qualified and qualified in analysis.symbols and qualified not in candidates:
                candidates.insert(0, qualified)
            if not candidates:
                return ""
            unused = [k for k in candidates if k not in used_desc_keys]
            # Prefer an unused (distinct) match so repeated locally-scoped helpers
            # (e.g. ten different `FakeProcess` closures) each get their own
            # description when the agent disambiguated its JSON keys. Once every
            # distinct variant is spent, fall back to reusing the first candidate
            # (old behavior) rather than rendering blank: an imprecise repeated
            # description beats a silent depth hole.
            key = unused[0] if unused else candidates[0]
            used_desc_keys.add(key)
            return analysis.symbols[key]

        for s in sorted(symbols, key=lambda s: s.start_line):
            name = s.qualified_name or s.name
            desc = _pick_description(s.name, s.qualified_name)
            line = f"- `{name}` ({s.kind}, L{s.start_line}-L{s.end_line})"
            if desc:
                line += f": {desc}"
            parts.append(line)
    else:
        parts.append("_(none detected)_")
    parts += ["", "### External dependencies", render_external_deps(imports)]
    risks = list(extra_risks or []) + (analysis.risks if analysis else [])
    if risks:
        parts += ["", "### Risks"]
        for r in risks:
            parts.append(
                f"- **{r.get('type', '?')}** ({r.get('evidence', '')}): {r.get('note', '')}"
            )
    return "\n".join(parts)


def assemble_decomposed_doc(
    scope: list[str],
    symbols: list[SymbolRecord],
    imports: list[ImportEdge],
    analyses: dict[str, FileAnalysis] | None = None,
) -> str:
    """Assemble a module doc from the decomposed Alpha output, in scope order."""
    scope_set = set(scope)
    sym_by_file = group_symbols_by_file(symbols, scope_set)
    imp_by_file = group_imports_by_file(imports, scope_set)
    det_risks = _deterministic_risks(scope, symbols, imports)
    analyses = analyses or {}
    blocks = [
        render_decomposed_file_block(
            rel,
            sym_by_file.get(rel, []),
            imp_by_file.get(rel, []),
            analyses.get(rel),
            det_risks.get(rel, []),
        )
        for rel in scope
    ]
    return "\n\n---\n\n".join(blocks) + "\n"
