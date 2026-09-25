from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from decoder.config import Tier

SymbolKind = Literal[
    "function",
    "method",
    "class",
    "struct",
    "enum",
    "interface",
    "trait",
    "type",
    "variable",
    "constant",
]

ImportKind = Literal["import", "from_import", "require", "use", "include"]


class LanguageStats(BaseModel):
    language: str
    files: int = 0
    lines: int = 0
    bytes: int = 0


class RepoMetrics(BaseModel):
    root: Path
    origin_url: str | None = None
    commit: str | None = None
    branch: str | None = None
    total_files: int = 0
    ignored_files: int = 0
    analyzed_files: int = 0
    total_lines: int = 0
    total_bytes: int = 0
    languages: dict[str, LanguageStats] = Field(default_factory=dict)
    estimated_tokens: int = 0


class TierDecision(BaseModel):
    tier: Tier
    reasoning: str
    teams: int
    workers_per_team: int


class SymbolRecord(BaseModel):
    file: str
    language: str
    kind: SymbolKind
    name: str
    qualified_name: str | None = None
    start_line: int
    end_line: int
    parent: str | None = None
    signature: str | None = None
    docstring: str | None = None


class ImportEdge(BaseModel):
    source_file: str
    target: str
    kind: ImportKind
    resolved_file: str | None = None
    line: int | None = None


class FileHistory(BaseModel):
    file: str
    commits: int = 0
    authors: list[str] = Field(default_factory=list)
    first_commit: datetime | None = None
    last_commit: datetime | None = None
    insertions: int = 0
    deletions: int = 0


class StaticAnalysisReport(BaseModel):
    metrics: RepoMetrics
    tier: TierDecision
    symbols: list[SymbolRecord] = Field(default_factory=list)
    imports: list[ImportEdge] = Field(default_factory=list)
    history: list[FileHistory] = Field(default_factory=list)
    symbols_by_file_count: dict[str, int] = Field(default_factory=dict)
    dep_graph_stats: dict[str, int] = Field(default_factory=dict)
