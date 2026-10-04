"""Core Pydantic models shared by orchestrator, agents, pipeline, state and routes."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def now() -> datetime:
    return datetime.now(timezone.utc)


class AgentRole(str, Enum):
    backend = "backend"
    frontend = "frontend"
    qa = "qa"


class EventType(str, Enum):
    """Status names exactly as the existing UI uses them."""
    THINKING = "THINKING"
    WRITE_CODE = "WRITE_CODE"
    COMMIT = "COMMIT"
    PR_CREATED = "PR_CREATED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    BLOCKED = "BLOCKED"


class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    committed = "committed"
    blocked = "blocked"


class Endpoint(BaseModel):
    method: str
    path: str


class TaskSpec(BaseModel):
    """Planner output for one agent (LLM-produced)."""
    role: AgentRole
    title: str
    description: str
    files_owned: List[str]


class FeatureAnalysis(BaseModel):
    """Planner output: prompt -> analysis. LLM-produced; validated, never hand-filled."""
    summary: str
    required_endpoints: List[Endpoint]
    required_schemas: List[str]
    tasks: List[TaskSpec]


class Contract(BaseModel):
    feature_id: str
    name: str
    openapi_yaml: str
    ts_types: str
    sha256: str
    source: str                      # "baseline-on-disk" | "llm-drafted"
    openapi_path: str
    types_path: str
    frozen_at: datetime = Field(default_factory=now)
    endpoints: List[Endpoint] = []
    schema_names: List[str] = []


class Task(BaseModel):
    id: str = Field(default_factory=lambda: new_id("task"))
    feature_id: str
    role: AgentRole
    title: str
    description: str
    files_owned: List[str]
    contract_sha256: str
    status: TaskStatus = TaskStatus.pending


class AgentProfile(BaseModel):
    role: AgentRole
    persona: str
    owner_name: str
    owner_email: str
    capabilities: List[str] = []


class UsageReport(BaseModel):
    role: AgentRole
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    elapsed_s: float = 0.0


class FileWrite(BaseModel):
    path: str
    content: str


class AgentResult(BaseModel):
    task_id: str
    role: AgentRole
    files: List[FileWrite]
    notes: str = ""
    usage: UsageReport
    started_at: float = 0.0
    finished_at: float = 0.0


class CommitRef(BaseModel):
    branch: str
    sha: str
    author_name: str
    author_email: str
    files: List[str]


class PRRef(BaseModel):
    number: int
    url: str
    branch: str


class ApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    merged = "merged"


class ApprovalRequest(BaseModel):
    id: str = Field(default_factory=lambda: new_id("appr"))
    feature_id: str
    task_id: str
    role: AgentRole
    pr: PRRef
    commit: CommitRef
    review_summary: str = ""
    status: ApprovalStatus = ApprovalStatus.pending
    merge_sha: Optional[str] = None
    decided_at: Optional[datetime] = None
    reason: Optional[str] = None


class Event(BaseModel):
    seq: int = 0
    ts: datetime = Field(default_factory=now)
    feature_id: Optional[str] = None
    type: EventType
    agent: Optional[AgentRole] = None
    message: str
    data: Dict[str, Any] = {}
