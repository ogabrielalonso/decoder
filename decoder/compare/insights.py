from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CONSOLIDATED_INSIGHTS_PROMPT = """\
You are the CONSOLIDATED INSIGHTS AGENT of the decoder system.

Your job: read the outputs of the per-repo decodes and the multi-repo
matrix artifacts, then produce a unified narrative about the set of
repositories as a whole. You are not re-analysing source code; you are
distilling what the individual decodes and the matrices already say.

Repositories analysed (slug list): {repos_list}
Compare output root: {compare_root}

What to read before writing anything
  1. {compare_root}/summary.md
  2. {compare_root}/feature_matrix.md
  3. {compare_root}/structure_matrix.md
  4. For each slug in the list: `{decode_root}/<slug>/architecture.md`,
     `{decode_root}/<slug>/domain.md`, `{decode_root}/<slug>/audit.md`,
     `{decode_root}/<slug>/executive_summary.md` when present.

Produce markdown at `{compare_root}/insights.md` with these exact sections:

```
# Consolidated Insights

## Overview
<2-4 sentences. What kind of set of repositories this is, what they have in
common, and the main axis they differ on.>

## Convergence
<patterns, entities, dependencies, or architectural decisions that are
present across all (or a majority of) the repos. Cite the concrete
occurrences from the feature / structure matrices.>

## Divergence axes
<clear table of the axes where the repos split. One row per axis (e.g.,
"auth strategy", "domain richness", "test coverage", "stack bias"), one
column per repo, one-cell summaries.>

| axis | {columns_header} |
|---|{columns_sep}|
| ... | ... |

## Outliers
<for each repo, one short paragraph: what makes it stand out in this set.
Ground every claim in a matrix row or a per-repo architecture/domain/audit
document. No speculation.>

## Cross-pollination opportunities
<pairs of directions: "repo X has <capability>; repo Y could adopt it
because <reason>". Ranked by likely impact.>

## Portfolio-level recommendations
<ordered list. Each item applies to the set as a whole or to 2+ repos.
Say which repos each recommendation targets.>

## Unknowns
<what the data cannot answer across the set. Be concrete about which
documents would have to exist to close the gap.>
```

Rules
- Every claim must cite a matrix row or a per-repo document path.
- Prefer "cannot determine from the current decode" over guessing.
- Keep total length under ~1200 words.
- Output ONLY the markdown, no preamble.
"""


@dataclass(slots=True)
class InsightsAssignment:
    prompt: str
    output_file: str


def build_insights_assignment(
    *,
    slugs: list[str],
    compare_root: Path,
    decode_root: Path,
) -> InsightsAssignment:
    repos_list = ", ".join(f"`{s}`" for s in slugs)
    columns_header = " | ".join(slugs)
    columns_sep = "|".join(["---"] * len(slugs))
    prompt = CONSOLIDATED_INSIGHTS_PROMPT.format(
        repos_list=repos_list,
        compare_root=str(compare_root),
        decode_root=str(decode_root),
        columns_header=columns_header,
        columns_sep=columns_sep,
    )
    return InsightsAssignment(
        prompt=prompt, output_file=str(compare_root / "insights.md")
    )
