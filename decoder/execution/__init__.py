"""Worker execution layer.

Runs generative workers as subprocesses of local AI CLIs (claude, codex),
keeping cost on flat-fee subscriptions instead of per-token API billing. Each
executor maps a `WorkerSpec` to a CLI invocation and returns a `WorkerResult`.

See docs/design/2026-05-31-execucao-custo-otimizado.md for the rationale and the
spike findings that shaped this layer (Write must be blocked for claude; codex
needs read-only sandbox + stdin redirect).
"""

from __future__ import annotations

from decoder.execution.base import (
    ModelTier,
    WorkerExecutor,
    WorkerResult,
    WorkerSpec,
)
from decoder.execution.claude_cli import ClaudeCliExecutor
from decoder.execution.codex_cli import CodexCliExecutor
from decoder.execution.mock import MockExecutor

__all__ = [
    "ClaudeCliExecutor",
    "CodexCliExecutor",
    "MockExecutor",
    "ModelTier",
    "WorkerExecutor",
    "WorkerResult",
    "WorkerSpec",
]
