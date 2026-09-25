"""Deterministic executor for tests: never spawns a subprocess."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from decoder.execution.base import (
    ModelTier,
    WorkerExecutor,
    WorkerResult,
    WorkerSpec,
)


class MockExecutor(WorkerExecutor):
    """Returns canned output without touching any CLI.

    Pass `responder` to compute output from the spec; otherwise echoes a stub.
    """

    name = "mock"
    model_map: ClassVar[dict[ModelTier, str]] = {}

    def __init__(self, responder: Callable[[WorkerSpec], str] | None = None) -> None:
        self.responder = responder
        self.calls: list[WorkerSpec] = []

    def model_for(self, tier: ModelTier) -> str:
        return "mock-model"

    def build_command(self, spec: WorkerSpec) -> list[str]:
        return ["mock", spec.id]

    def execute(self, spec: WorkerSpec) -> WorkerResult:
        self.calls.append(spec)
        text = self.responder(spec) if self.responder else f"# output for {spec.id}"
        return WorkerResult(
            id=spec.id,
            status="ok",
            output_text=text,
            executor=self.name,
            model="mock-model",
            duration_s=0.0,
            cost_usd=0.0,
        )
