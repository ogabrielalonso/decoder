"""Executor that runs workers via the `claude` CLI (Claude subscription)."""

from __future__ import annotations

import json
import subprocess
import time
from typing import ClassVar

from decoder.execution.base import (
    DENIED_TOOLS,
    READ_ONLY_TOOLS,
    ModelTier,
    WorkerExecutor,
    WorkerResult,
    WorkerSpec,
)

# Markers that identify a TRANSIENT rate-limit/overload (vs a hard error or a
# quality problem). When seen, the executor backs off and retries the SAME tier
# instead of surfacing an error that would make the runner escalate to opus.
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "overload",
    "quota",
    "429",
    "529",
    "capacity",
    "usage limit",
    "try again",
)
_MAX_RATE_RETRIES = 3
_BACKOFF_BASE_S = 30


class ClaudeCliExecutor(WorkerExecutor):
    """Spawns `claude -p` headless, authenticating via the user subscription.

    Findings baked in (see RFC §12/§13):
    - NEVER pass --bare: it forces API-key auth and breaks subscription OAuth.
    - --allowedTools does NOT block Write, so we ALSO pass --disallowedTools for
      every mutating tool; workers are read-only by construction.
    - Prompt is fed on stdin (not argv) to avoid shell-escaping huge prompts.
    - --output-format json gives us the result text plus cost/usage to parse.
    """

    name = "claude"
    model_map: ClassVar[dict[ModelTier, str]] = {
        ModelTier.CHEAP: "haiku",
        ModelTier.BALANCED: "sonnet",
        ModelTier.STRONG: "opus",
    }

    def __init__(self, binary: str = "claude", model: str | None = None) -> None:
        self.binary = binary
        self.model_override = model

    def build_command(self, spec: WorkerSpec) -> list[str]:
        cmd = [
            self.binary,
            "-p",
            "--model",
            self.model_for(spec.tier),
            "--output-format",
            "json",
            "--allowedTools",
            ",".join(READ_ONLY_TOOLS),
            "--disallowedTools",
            *DENIED_TOOLS,
        ]
        for d in spec.all_read_dirs():
            cmd += ["--add-dir", str(d)]
        return cmd

    def execute(self, spec: WorkerSpec) -> WorkerResult:
        """Run the worker, retrying TRANSIENT rate-limits with backoff.

        A rate-limit/overload (even one the CLI reports with a zero exit code and
        an empty body) is retried on the SAME tier after a growing delay, so a
        temporary quota cap does not surface as a hard error that would make the
        runner escalate to opus or degrade the worker. Hard errors and quality
        problems are returned immediately for the runner's contract loop to judge.
        """
        cmd = self.build_command(spec)
        model = self.model_for(spec.tier)
        last_detail = "rate-limited (no successful attempt)"

        for attempt in range(_MAX_RATE_RETRIES + 1):
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

            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                if _is_rate_limit(err) and attempt < _MAX_RATE_RETRIES:
                    last_detail = err[:200]
                    time.sleep(_BACKOFF_BASE_S * (attempt + 1))
                    continue
                return WorkerResult(
                    id=spec.id,
                    status="error",
                    executor=self.name,
                    model=model,
                    duration_s=duration,
                    error=err[:2000] or "non-zero exit",
                )

            text, cost, kind, detail = _parse_claude_json(proc.stdout)
            if kind == "rate_limit" and attempt < _MAX_RATE_RETRIES:
                last_detail = detail or "rate limited"
                time.sleep(_BACKOFF_BASE_S * (attempt + 1))
                continue
            if kind == "error":
                return WorkerResult(
                    id=spec.id,
                    status="error",
                    executor=self.name,
                    model=model,
                    duration_s=duration,
                    error=detail or "claude returned is_error",
                )
            return WorkerResult(
                id=spec.id,
                status="ok",
                output_text=text,
                executor=self.name,
                model=model,
                duration_s=duration,
                cost_usd=cost,
            )

        return WorkerResult(
            id=spec.id,
            status="error",
            executor=self.name,
            model=model,
            duration_s=0.0,
            error=f"rate-limited after {_MAX_RATE_RETRIES} retries: {last_detail}",
        )


def _is_rate_limit(text: str) -> bool:
    """True if a message looks like a transient rate-limit / overload / capacity cap."""
    low = text.lower()
    return any(m in low for m in _RATE_LIMIT_MARKERS)


def _parse_claude_json(stdout: str) -> tuple[str, float | None, str, str | None]:
    """Parse `claude --output-format json` into (text, cost, kind, detail).

    ``kind`` is one of:
    - ``"ok"``: a real result body to hand to the contract validator
    - ``"rate_limit"``: transient cap (the executor backs off and retries)
    - ``"error"``: hard error reported by the CLI envelope

    The CLI signals failure via ``is_error`` / ``subtype`` / ``api_error_status``
    even when the process exits 0; we inspect those so a rate-limit or error with
    an empty/garbage body is not mistaken for a successful empty result (the bug
    that silently dropped 13 workers).
    """
    raw = stdout.strip()
    if not raw:
        # No output at all with a zero exit is the classic silent rate cap.
        return "", None, "rate_limit", "empty stdout"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Unknown shape: keep raw text and let the contract validator judge it.
        return raw, None, "ok", None
    if not isinstance(data, dict):
        return raw, None, "ok", None

    cost = data.get("total_cost_usd")
    if not isinstance(cost, (int, float)):
        cost = None
    text = str(data.get("result") or data.get("text") or "")
    is_err = bool(data.get("is_error"))
    subtype = str(data.get("subtype") or "")
    api_status = data.get("api_error_status")
    blob = f"{subtype} {api_status} {text}".strip()

    if is_err or (subtype and subtype != "success"):
        if _is_rate_limit(blob):
            return text, cost, "rate_limit", blob[:200]
        return text, cost, "error", (text or subtype or "claude is_error")[:500]
    if not text.strip():
        # Success envelope but empty body: treat as a transient cap and back off.
        return "", cost, "rate_limit", "empty result body"
    return text, cost, "ok", None
