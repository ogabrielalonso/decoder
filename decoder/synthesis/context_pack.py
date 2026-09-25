"""Condense all Alpha module docs into one compact overview for synthesis teams.

Bravo/Charlie/Delta each otherwise re-read the entire modules/ directory. The
context pack distills every file to: its Purpose, its symbol *names* (line ranges
kept, verbose per-symbol prose dropped), and its risks: dropping the external-
dependencies section and the long descriptions. Synthesis reads this single file
for the overview and drills into modules/<file>.md only when it needs a specific
file's full detail. Pure text transform, deterministic.
"""

from __future__ import annotations

from pathlib import Path

_HEADER = (
    "# Context Pack: condensed overview of every module\n\n"
    "_Read this for the whole-repo picture. For a specific file's full per-symbol "
    "detail, open `modules/<file>.md`._\n"
)


def _condense_module(text: str) -> str:
    out: list[str] = []
    skip = False
    for line in text.splitlines():
        if line.startswith("## "):  # per-file header
            out.append("")
            out.append(line)
            skip = False
        elif line.startswith("### "):
            skip = line[4:].strip().lower() == "external dependencies"
            if not skip:
                out.append(line)
        elif skip:
            continue
        elif line.startswith("- `") and "):" in line:
            # symbol bullet: keep "- `name` (kind, Lx-Ly)", drop the description
            out.append(line.split("):", 1)[0] + ")")
        else:
            out.append(line)
    return "\n".join(out).strip()


def build_context_pack(modules_dir: Path) -> str:
    """Build the condensed pack from every module doc under modules_dir."""
    blocks = [_HEADER]
    for md in sorted(modules_dir.glob("*.md")):
        condensed = _condense_module(md.read_text(encoding="utf-8", errors="replace"))
        if condensed:
            blocks.append(condensed)
    return "\n\n".join(blocks) + "\n"
