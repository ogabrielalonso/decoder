"""Deterministic security pre-scan: high-signal regex over the source tree.

Delta (audit) currently re-discovers secrets/dangerous calls by sampling files,
so anything outside its sample is missed. A full-repo regex pass surfaces the
obvious cases exactly (with file:line evidence) so the LLM verifies instead of
hunting. Kept to high-precision patterns to avoid false-positive noise.
"""

from __future__ import annotations

import re
from pathlib import Path

from decoder.config import settings
from decoder.static_analysis.languages import is_binary, is_probably_text

_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "hardcoded-secret",
        re.compile(
            r"""(?i)\b(api[_-]?key|secret|password|passwd|token|access[_-]?key)\b['"]?\s*[=:]\s*['"][^'"\s]{8,}['"]"""
        ),
    ),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("shell-injection-risk", re.compile(r"shell\s*=\s*True")),
    ("dynamic-exec", re.compile(r"\b(?:eval|exec)\s*\(")),
]


def scan_security(root: Path, *, max_hits: int = 200) -> list[dict]:
    """Walk text files and flag high-signal secret / dangerous-call patterns."""
    ignore = set(settings.ignore_patterns)
    hits: list[dict] = []
    for path in root.rglob("*"):
        if len(hits) >= max_hits:
            break
        if not path.is_file():
            continue
        if set(path.relative_to(root).parts) & ignore:
            continue
        if is_binary(path.name) or not is_probably_text(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(root))
        for i, line in enumerate(text.splitlines(), 1):
            for label, pat in _PATTERNS:
                if pat.search(line):
                    hits.append(
                        {"file": rel, "line": i, "pattern": label, "snippet": line.strip()[:80]}
                    )
                    break
            if len(hits) >= max_hits:
                break
    return hits


def render_security_hints(hits: list[dict]) -> str:
    """Markdown injected into Delta (verify: regex may have false positives)."""
    lines = [
        "## Precomputed security pre-scan (regex over the whole repo: VERIFY each, "
        "may include false positives)",
        "",
    ]
    if not hits:
        lines.append("_(no obvious secret / dangerous-call patterns matched)_")
        return "\n".join(lines)
    for h in hits:
        lines.append(
            f"- **{h['pattern']}** `{h['file']}`:L{h['line']}: `{h['snippet']}`"
        )
    return "\n".join(lines)
