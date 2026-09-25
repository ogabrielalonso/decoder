from enum import StrEnum
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Tier(StrEnum):
    NANO = "nano"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    HUGE = "huge"


TIER_LOC_BOUNDS: dict[Tier, tuple[int, int]] = {
    Tier.NANO: (0, 1_000),
    Tier.SMALL: (1_000, 10_000),
    Tier.MEDIUM: (10_000, 100_000),
    Tier.LARGE: (100_000, 1_000_000),
    Tier.HUGE: (1_000_000, 10**12),
}

TIER_TEAM_PLAN: dict[Tier, dict[str, int]] = {
    Tier.NANO: {"teams": 0, "workers_per_team": 1},
    Tier.SMALL: {"teams": 1, "workers_per_team": 3},
    Tier.MEDIUM: {"teams": 4, "workers_per_team": 3},
    Tier.LARGE: {"teams": 4, "workers_per_team": 5},
    Tier.HUGE: {"teams": 4, "workers_per_team": 5},
}

# Backstop only: the real worker count is derived adaptively from repo size
# (tokens AND file count). A low cap here was what collapsed huge repos into a
# handful of overloaded workers; keep this high enough to never bind in practice.
MAX_WORKERS_PER_TEAM = 300

# Context window of the model the workers ACTUALLY run on. In this environment
# subagents are Sonnet-locked (a requested model:'opus' is ignored), and only Opus
# has a 1M window: Sonnet is ~200k. So sizing must target 200k, NOT 1M. A worker
# whose (injected-symbol prompt + file content it reads + JSON output) exceeds this
# overflows the window and degrades (silent compaction of earlier files).
WORKER_CONTEXT_LIMIT = 200_000

# Output-bound cap: each Alpha worker emits one JSON object covering EVERY file in
# its scope. Too many files per worker -> the JSON output grows until it truncates
# (the "10/10 files have NO purpose" failure). Combined with the per-worker token
# target (see _DEFAULT_TARGET_TOKENS_PER_WORKER), this keeps each worker inside the
# Sonnet window: file-content target + symbol prompt + output all fit under 200k.
MAX_FILES_PER_WORKER = 100

# Output-bound cap #2: the worker also describes EVERY symbol. When a worker holds
# too many symbols (especially symbol-dense "god module" files piled together), the
# agent skimps: it lists the symbols but omits the per-symbol descriptions. Capping
# symbols/worker isolates dense files so the agent can describe all of them. This is
# what a files+tokens-only sizing missed (symbols, not files, drive the output).
MAX_SYMBOLS_PER_WORKER = 120


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DECODER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    workspace_dir: Path = Field(default=Path("workspace"))
    cache_dir: Path = Field(default=Path(".decoder_cache"))
    output_dir: Path = Field(default=Path("docs"))

    embedding_model: str = Field(default="BAAI/bge-small-en-v1.5")
    chroma_persist_dir: Path = Field(default=Path(".decoder_cache/chroma"))

    log_level: str = Field(default="INFO")

    ignore_patterns: tuple[str, ...] = Field(
        default=(
            "node_modules",
            ".venv",
            "venv",
            "__pycache__",
            ".git",
            "dist",
            "build",
            ".next",
            "target",
            ".idea",
            ".vscode",
        )
    )

    def ensure_dirs(self) -> None:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "decode").mkdir(parents=True, exist_ok=True)
        (self.output_dir / "compare").mkdir(parents=True, exist_ok=True)


settings = Settings()
