from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

EXECUTIVE_SUMMARY_PROMPT = """\
You are the EXECUTIVE SYNTHESIZER of the decoder system.

Your job: produce the one-page executive summary that sits at the front of
the final decode report. You are synthesising what the four teams and the
red-team agent have already produced; you do not re-analyse source code.

Repository root (read-only): {source_path}
Decode output root: {output_root}

Read, in order
  1. {output_root}/index.md
  2. {output_root}/architecture.md
  3. {output_root}/domain.md
  4. {output_root}/audit.md
  5. {output_root}/qa/qa_report.md (if present)
  6. {output_root}/red_team.md (if present)

{scale}

Produce markdown with EXACTLY these sections:

```
# Executive Summary

## What this repository is
<3-5 sentences. Plain language. No marketing words. Mention purpose,
primary tech stack, rough size. Use data from the index + architecture.>

## Shape at a glance
- **Architecture**: <one sentence distilled from architecture.md>
- **Domain**: <one sentence distilled from domain.md>
- **Scale**: <use the PRECOMPUTED scale facts above verbatim: do not recompute>

## What works well
- <bullet list of genuine strengths observed by the teams, with citations>

## Risks and hotspots
- <bullet list of the top 3-5 risks. Prefer concrete over generic.>

## What to fix first
<ordered list, most impactful first. Anchored in the audit + red-team
verdicts. No more than 5 items.>

## Unknowns
<what the decode could not answer and why. Anchored in red_team.md or QA
gaps if available. Omit if nothing applies.>
```

Rules
- Never invent findings; every claim must be traceable to one of the inputs.
- Keep the whole document under ~700 words.
- Cite files and line ranges when quoting specific findings.
- Do NOT write any file. Your entire response IS the document (it is saved for
  you). Begin immediately with `# Executive Summary`: no preamble, no closing.
"""


@dataclass(slots=True)
class ExecutiveAssignment:
    prompt: str
    output_file: str


def build_executive_assignment(
    source_path: Path, output_root: Path, scale: str = ""
) -> ExecutiveAssignment:
    prompt = EXECUTIVE_SUMMARY_PROMPT.format(
        source_path=str(source_path),
        output_root=str(output_root),
        scale=scale,
    )
    return ExecutiveAssignment(
        prompt=prompt,
        output_file=str(output_root / "executive_summary.md"),
    )
