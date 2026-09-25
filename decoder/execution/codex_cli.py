"""Executor that runs workers via the `codex` CLI (ChatGPT subscription)."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import ClassVar

from decoder.execution.base import (
    ModelTier,
    WorkerExecutor,
    WorkerResult,
    WorkerSpec,
)


class CodexCliExecutor(WorkerExecutor):
    """Spawns `codex exec` headless on the ChatGPT subscription.

    Findings baked in (see RFC §13):
    - --sandbox read-only correctly blocks all writes (safer than claude here).
    - Prompt is fed on stdin; stdin is otherwise a non-TTY which makes codex
      hang on "Reading additional input from stdin": feeding the prompt there
      (rather than as argv) both avoids the hang and skips shell escaping.
    - Final answer is captured via --output-last-message <file>, since stdout
      carries the agent transcript, not just the answer.
    - --skip-git-repo-check lets it run on sources that aren't git repos.
    """

    name = "codex"
    # A ChatGPT-account Codex exposes a SINGLE model: gpt-5.5. The gpt-5.1-codex*
    # names are API-only and 400 ("not supported with a ChatGPT account").
    # Reasoning effort (-c model_reasoning_effort) could differentiate tiers
    # later; for now every tier uses gpt-5.5.
    model_map: ClassVar[dict[ModelTier, str]] = {
        ModelTier.CHEAP: "gpt-5.5",
        ModelTier.BALANCED: "gpt-5.5",
        ModelTier.STRONG: "gpt-5.5",
    }

    def __init__(self, binary: str = "codex", model: str | None = None) -> None:
        self.binary = binary
        self.model_override = model

    @staticmethod
    def _msg_file_for(spec: WorkerSpec) -> Path:
        return spec.output_file.with_suffix(spec.output_file.suffix + ".codexmsg")

    def build_command(self, spec: WorkerSpec) -> list[str]:
        cmd = [
            self.binary,
            "exec",
            "--model",
            self.model_for(spec.tier),
            "--cd",
            str(spec.source_dir),
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--color",
            "never",
            # skip user config/MCP servers/hooks (less startup noise & overhead;
            # auth still resolves via CODEX_HOME)
            "--ignore-user-config",
            "--output-last-message",
            str(self._msg_file_for(spec)),
        ]
        for d in spec.extra_read_dirs:
            cmd += ["--add-dir", str(d)]
        return cmd

    def execute(self, spec: WorkerSpec) -> WorkerResult:
        model = self.model_for(spec.tier)
        msg_file = self._msg_file_for(spec)
        cmd = self.build_command(spec)
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                input=spec.prompt,
                capture_output=True,
                text=True,
                timeout=spec.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return WorkerResult(
                id=spec.id,
                status="timeout",
                executor=self.name,
                model=model,
                duration_s=time.monotonic() - start,
                error=f"timed out after {spec.timeout_s}s",
            )
        duration = time.monotonic() - start

        text = ""
        if msg_file.exists():
            text = msg_file.read_text(encoding="utf-8", errors="replace")
            msg_file.unlink(missing_ok=True)

        if proc.returncode != 0:
            return WorkerResult(
                id=spec.id,
                status="error",
                executor=self.name,
                model=model,
                duration_s=duration,
                error=(proc.stderr or proc.stdout or "").strip()[:2000],
            )

        return WorkerResult(
            id=spec.id,
            status="ok",
            output_text=text,
            executor=self.name,
            model=model,
            duration_s=duration,
        )
