from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class ValidationIssue:
    file: str
    section: str | None
    severity: str
    message: str


@dataclass(slots=True)
class ValidationReport:
    documents_checked: int
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def passes(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)


_MIN_WORDS = {"Purpose": 5, "Key symbols": 6, "Notes": 3}
_REQUIRED_SECTIONS = {"Purpose", "Key symbols", "External dependencies"}
_CITATION_PATTERN = re.compile(r"L\d+-L\d+|:L\d+|:\d+\b")
_BACKTICK_PATTERN = re.compile(r"`[^`\n]+`")


def validate_module_docs(output_root: Path) -> ValidationReport:
    report = ValidationReport(documents_checked=0)
    modules_dir = output_root / "modules"
    if not modules_dir.is_dir():
        return report

    for md in sorted(modules_dir.glob("*.md")):
        report.documents_checked += 1
        content = md.read_text(encoding="utf-8", errors="replace")
        _check_module_doc(md.name, content, report.issues)
    return report


def validate_team_outputs(output_root: Path) -> ValidationReport:
    report = ValidationReport(documents_checked=0)
    for name in ("architecture.md", "domain.md", "audit.md"):
        path = output_root / name
        if not path.exists():
            report.issues.append(
                ValidationIssue(
                    file=name,
                    section=None,
                    severity="error",
                    message="team output missing",
                )
            )
            continue
        report.documents_checked += 1
        content = path.read_text(encoding="utf-8", errors="replace")
        _check_citations(path.name, content, report.issues)
    return report


def _check_module_doc(name: str, content: str, out: list[ValidationIssue]) -> None:
    file_sections = _split_by_file_header(content)
    if not file_sections:
        out.append(
            ValidationIssue(
                file=name,
                section=None,
                severity="error",
                message="no file sections detected (expected `## `...`` headers)",
            )
        )
        return

    for file_path, body in file_sections:
        sections = _split_by_h3(body)
        present = {s.lower() for s in sections}
        missing = {s for s in _REQUIRED_SECTIONS if s.lower() not in present}
        for m in missing:
            out.append(
                ValidationIssue(
                    file=name,
                    section=f"{file_path} / {m}",
                    severity="error",
                    message="required section missing",
                )
            )
        for section_name, section_body in sections.items():
            words = len(section_body.split())
            needed = _MIN_WORDS.get(section_name.title(), 0)
            if needed and words < needed:
                out.append(
                    ValidationIssue(
                        file=name,
                        section=f"{file_path} / {section_name}",
                        severity="warning",
                        message=f"section has {words} words, expected >= {needed}",
                    )
                )
        # Files with no symbols (README/config) legitimately have no identifiers
        # to quote: only require backticks where the doc actually lists symbols.
        if "none detected" not in body.lower() and not _BACKTICK_PATTERN.search(body):
            out.append(
                ValidationIssue(
                    file=name,
                    section=file_path,
                    severity="warning",
                    message="no backtick-quoted identifiers in this file section",
                )
            )


def _check_citations(name: str, content: str, out: list[ValidationIssue]) -> None:
    if not _CITATION_PATTERN.search(content):
        out.append(
            ValidationIssue(
                file=name,
                section=None,
                severity="warning",
                message="no line-range citations (e.g. L10-L20) found",
            )
        )
    if not _BACKTICK_PATTERN.search(content):
        out.append(
            ValidationIssue(
                file=name,
                section=None,
                severity="warning",
                message="no backtick-quoted identifiers or paths",
            )
        )


def _split_by_file_header(content: str) -> list[tuple[str, str]]:
    """Parse `## `path`` style file headers."""
    pattern = re.compile(r"^##\s+`([^`]+)`\s*$", re.MULTILINE)
    out: list[tuple[str, str]] = []
    matches = list(pattern.finditer(content))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        out.append((m.group(1), content[start:end]))
    return out


def _split_by_h3(body: str) -> dict[str, str]:
    pattern = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(body))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[m.group(1).strip()] = body[start:end]
    return sections
