"""Core contracts for the worker execution layer."""

from __future__ import annotations

import abc
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

# Read-only toolset every worker is allowed. Workers analyse code; they never
# mutate it. The spike showed claude's --allowedTools does NOT block Write, so
# executors must also explicitly disallow write tools (see DENIED_TOOLS).
READ_ONLY_TOOLS = ("Read", "Grep", "Glob")
DENIED_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


class ModelTier(StrEnum):
    """Abstract capability tier, mapped to a concrete model per executor.

    Routing is decided by evidence (the eval harness), not hard-coded here:
    callers pick a tier per worker and each executor resolves it to its own
    model id (e.g. CHEAP -> claude 'haiku', BALANCED -> claude 'sonnet').
    """

    CHEAP = "cheap"
    BALANCED = "balanced"
    STRONG = "strong"


WorkerStatus = Literal["ok", "error", "timeout"]


class WorkerSpec(BaseModel):
    """A single generative unit of work to run on some model."""

    id: str
    prompt: str
    tier: ModelTier = ModelTier.BALANCED
    source_dir: Path
    output_file: Path
    scope: list[str] = Field(default_factory=list)
    extra_read_dirs: list[Path] = Field(default_factory=list)
    timeout_s: int = 600

    def all_read_dirs(self) -> list[Path]:
        """source_dir plus any extra dirs, de-duplicated, order-preserving."""
        seen: dict[Path, None] = {}
        for d in (self.source_dir, *self.extra_read_dirs):
            seen.setdefault(Path(d), None)
        return list(seen)


class WorkerResult(BaseModel):
    """Outcome of running a WorkerSpec."""

    id: str
    status: WorkerStatus
    output_text: str = ""
    executor: str = ""
    model: str = ""
    duration_s: float = 0.0
    cost_usd: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class WorkerExecutor(abc.ABC):
    """Maps a WorkerSpec to a concrete CLI run and returns a WorkerResult."""

    #: stable identifier for logging / routing tables
    name: str = "base"

    #: tier -> concrete model id for this executor
    model_map: ClassVar[dict[ModelTier, str]] = {}

    def model_for(self, tier: ModelTier) -> str:
        # An explicit per-instance model override wins over the tier map: this
        # is how the eval harness pins a specific model (e.g. codex gpt-5.5).
        override = getattr(self, "model_override", None)
        if override:
            return override
        try:
            return self.model_map[tier]
        except KeyError as exc:  # pragma: no cover - defensive
            raise ValueError(
                f"{self.name} has no model mapped for tier {tier!r}"
            ) from exc

    @abc.abstractmethod
    def build_command(self, spec: WorkerSpec) -> list[str]:
        """Build the argv for this spec. Pure: no side effects (testable)."""

    @abc.abstractmethod
    def execute(self, spec: WorkerSpec) -> WorkerResult:
        """Run the worker and return its result."""
