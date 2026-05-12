from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, UTC
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def to_plain(value: Any) -> Any:
    if is_dataclass(value):
        return {key: to_plain(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    return value


@dataclass(slots=True)
class Repository:
    owner: str
    name: str
    default_branch: str = "main"
    repo_id: str = "demo-repo"
    installation_id: str = "demo-installation"

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(slots=True)
class PullRequest:
    number: int
    title: str
    body: str
    author: str
    base_sha: str
    head_sha: str
    repository: Repository
    labels: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ChangedFile:
    path: str
    status: str = "modified"
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    patch: str = ""
    content: str = ""


@dataclass(slots=True)
class AnalysisRun:
    id: str
    pull_request: PullRequest
    state: str = "queued"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    agent_results: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentEnvelope:
    analysis_run_id: str
    pull_request: dict[str, Any]
    changed_files: list[dict[str, Any]]
    prior_results: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentResult:
    agent_id: str
    status: str
    output: dict[str, Any]
    confidence: float = 1.0
    messages: list[str] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        return to_plain(self)
