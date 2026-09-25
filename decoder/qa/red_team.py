from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

RED_TEAM_PROMPT = """\
You are the RED TEAM agent of the decoder system.

Your job: read every output produced by Teams Alpha, Bravo, Charlie, and
Delta for a decoded repository, then actively try to falsify their claims.
You are adversarial by design: if a pattern was declared, prove it with the
code; if a claim cannot be supported, flag it.

Repository root (read-only): {source_path}
Decode output root: {output_root}
Static report: {static_report_path}

What to read before writing anything
  1. {output_root}/index.md
  2. Every markdown in {output_root}/modules/
  3. {output_root}/architecture.md
  4. {output_root}/domain.md
  5. {output_root}/audit.md
  6. {static_report_path} (use it to verify cited symbols / line ranges
     actually exist)

{coverage}

For each claim you evaluate, classify it as:
  - VERIFIED: claim is backed by direct code evidence you re-checked.
  - WEAK:     claim is plausible but under-cited; say what evidence is
              missing.
  - UNSUPPORTED: claim is wrong or cannot be substantiated; give the
                 contradicting evidence.

Produce a markdown document with this structure:

```
# Red Team Review

## Summary
<counts: verified / weak / unsupported. 1-3 sentences of overall judgment.>

## Findings
### <Team>: <short title of claim under review>
- **status**: verified | weak | unsupported
- **original claim**: <quote or paraphrase>
- **your check**: <what you did to verify>
- **evidence**: `<path>:L<start>-L<end>` (or "no evidence found")
- **verdict**: <one sentence>

(repeat for each notable claim; do not review every claim: focus on the
ones that carry the most weight or that you could falsify.)

## Gaps
<The PRECOMPUTED coverage gaps above already list files/symbols that went
uncommented: do NOT recompute them. Pick the few that genuinely should have
been covered and say why they matter.>

## Recommendations
<ordered list of what subsequent runs should tighten. Be concrete.>
```

Rules
- Never accept a claim on faith. Re-open the cited file and check.
- Prefer "UNSUPPORTED" over silence when you cannot verify.
- Quote short code snippets (<= 4 lines) as evidence when useful.
- Do NOT write any file. Your entire response IS the document (it is saved for
  you). Begin immediately with `# Red Team Review`: no preamble, no closing.
"""


@dataclass(slots=True)
class RedTeamAssignment:
    prompt: str
    output_file: str


def build_red_team_assignment(
    source_path: Path,
    output_root: Path,
    static_report_path: Path,
    coverage: str = "",
) -> RedTeamAssignment:
    prompt = RED_TEAM_PROMPT.format(
        source_path=str(source_path),
        output_root=str(output_root),
        static_report_path=str(static_report_path),
        coverage=coverage,
    )
    return RedTeamAssignment(
        prompt=prompt,
        output_file=str(output_root / "red_team.md"),
    )
