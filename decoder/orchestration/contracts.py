from __future__ import annotations

from pydantic import BaseModel, Field

from decoder.config import Tier

ORCHESTRATION_VERSION = "0.2"


class WorkerAssignment(BaseModel):
    id: str
    scope: list[str]
    estimated_bytes: int
    estimated_tokens: int
    prompt: str
    output_file: str


class TeamPlan(BaseModel):
    id: str
    name: str
    domain: str
    lead_prompt: str
    workers: list[WorkerAssignment] = Field(default_factory=list)


class OrchestrationPlan(BaseModel):
    version: str = ORCHESTRATION_VERSION
    slug: str
    source_path: str
    origin_url: str | None = None
    commit: str | None = None
    branch: str | None = None
    tier: Tier
    teams: list[TeamPlan] = Field(default_factory=list)
    output_root: str
    static_report_path: str
    notes: list[str] = Field(default_factory=list)
