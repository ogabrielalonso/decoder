from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from decoder.config import settings
from decoder.schemas import SymbolRecord
from decoder.static_analysis.languages import detect_language, is_binary, is_probably_text


@dataclass(slots=True)
class FileInfo:
    relative: str
    bytes: int
    language: str
    symbols: int


@dataclass(slots=True)
class AssetInfo:
    """A file captured for 100% coverage but NOT LLM-analyzed (binary/media, or
    a vendored/VCS file when --analyze-vendored is off). Inventoried, never dropped."""

    relative: str
    bytes: int
    kind: str  # 'vendored' | 'binary' | 'binary:<ext>'


@dataclass(slots=True)
class Chunk:
    files: list[FileInfo]
    total_bytes: int


def build_file_infos(
    root: Path,
    symbols: list[SymbolRecord],
    *,
    analyze_vendored: bool = False,
) -> tuple[list[FileInfo], list[AssetInfo]]:
    """Partition EVERY file under root into (text-to-analyze, assets-to-inventory).

    Nothing is silently dropped (100% coverage):
    - vendored/VCS files (ignore_patterns) -> asset inventory, unless analyze_vendored.
    - binary/media (known ext or NUL-byte sniff) -> asset inventory (no text to read).
    - everything else textual (ANY extension, even unknown/no-ext) -> analyzed,
      tagged with its detected language or 'other'.
    """
    symbols_per_file: dict[str, int] = defaultdict(int)
    for sym in symbols:
        symbols_per_file[sym.file] += 1

    ignore = set(settings.ignore_patterns)
    infos: list[FileInfo] = []
    assets: list[AssetInfo] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        try:
            size = path.stat().st_size
        except OSError:
            continue

        if (set(path.relative_to(root).parts) & ignore) and not analyze_vendored:
            assets.append(AssetInfo(relative=rel, bytes=size, kind="vendored"))
            continue
        if is_binary(path.name) or not is_probably_text(path):
            kind = f"binary:{path.suffix.lower()}" if path.suffix else "binary"
            assets.append(AssetInfo(relative=rel, bytes=size, kind=kind))
            continue

        infos.append(
            FileInfo(
                relative=rel,
                bytes=size,
                language=detect_language(path.name) or "other",
                symbols=symbols_per_file.get(rel, 0),
            )
        )
    return infos, assets


def render_asset_inventory(assets: list[AssetInfo]) -> str:
    """Markdown inventory of every captured-but-not-analyzed file (100% coverage)."""
    from collections import Counter

    lines = [
        "# Asset Inventory",
        "",
        f"_{len(assets)} file(s) captured for completeness but NOT LLM-analyzed "
        "(binary/media, or vendored/VCS files). Listed so coverage is 100%: their "
        "contents are not summarized because there is no source text to read._",
        "",
    ]
    if not assets:
        lines.append("_(none: every file was analyzed)_")
        return "\n".join(lines) + "\n"

    by_kind = Counter(a.kind for a in assets)
    lines.append("## By type")
    for kind, n in sorted(by_kind.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"- `{kind}`: {n}")
    lines += ["", "## Files", "", "| file | kind | bytes |", "|---|---|--:|"]
    for a in sorted(assets, key=lambda a: a.relative):
        lines.append(f"| `{a.relative}` | {a.kind} | {a.bytes:,} |")
    return "\n".join(lines) + "\n"


def chunk_files(
    files: list[FileInfo],
    *,
    worker_count: int,
    max_files_per_chunk: int = 0,
    max_symbols_per_chunk: int = 0,
) -> list[Chunk]:
    """Greedy chunking that keeps directory-adjacent files together.

    Fills each chunk until it reaches the per-worker byte target OR the
    ``max_files_per_chunk`` cap OR the ``max_symbols_per_chunk`` cap (0 = no cap),
    then starts the next one. The byte target balances load; the file cap and the
    symbol cap are HARD guarantees on OUTPUT size: no worker holds more files (one
    JSON entry each) or more symbols (one description each) than the agent can emit
    without skimping/truncating. The resulting chunk count is emergent: close to
    ``worker_count`` but may be higher when dense files would otherwise pile up.
    """
    if worker_count <= 0 or not files:
        return []

    files_sorted = sorted(files, key=lambda f: (Path(f.relative).parent.as_posix(), f.relative))
    total_bytes = sum(f.bytes for f in files_sorted)
    target_per_worker = max(1, total_bytes // worker_count)

    chunks: list[Chunk] = []
    current = Chunk(files=[], total_bytes=0)
    current_symbols = 0
    for info in files_sorted:
        # Close the current chunk BEFORE adding a file that would push it past the
        # byte target, the file cap, or the symbol cap: appending first would let one
        # large/dense boundary file overshoot. A single file bigger than a cap
        # unavoidably forms its own (oversized) chunk; nothing else exceeds it.
        would_exceed_bytes = current.files and current.total_bytes + info.bytes > target_per_worker
        would_exceed_files = max_files_per_chunk > 0 and len(current.files) >= max_files_per_chunk
        would_exceed_symbols = (
            current.files
            and max_symbols_per_chunk > 0
            and current_symbols + info.symbols > max_symbols_per_chunk
        )
        if would_exceed_bytes or would_exceed_files or would_exceed_symbols:
            chunks.append(current)
            current = Chunk(files=[], total_bytes=0)
            current_symbols = 0
        current.files.append(info)
        current.total_bytes += info.bytes
        current_symbols += info.symbols
    if current.files:
        chunks.append(current)

    return chunks
