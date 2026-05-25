"""Lightweight TypedDicts for PR history records.

We use TypedDict rather than dataclasses so the same dict shapes survive
the round-trip through MongoDB, BSON, and JSON without an extra
serialisation layer. Adapter implementations should accept any
``dict[str, Any]`` that matches these keys and ignore extras.
"""

from __future__ import annotations

from typing import Any, TypedDict


class PriorPR(TypedDict, total=False):
    repo_key: str
    pr_number: int
    title: str
    body: str
    state: str  # "merged" | "closed" | "open"
    merged_at: str
    closed_at: str
    created_at: str
    author: str
    labels: list[str]
    linked_jira_keys: list[str]
    reviewers: list[str]
    changed_file_count: int
    changed_paths: list[str]
    html_url: str
    source: str  # "github"
    indexed_at: str


class PriorPRFile(TypedDict, total=False):
    repo_key: str
    pr_number: int
    path: str
    status: str
    additions: int
    deletions: int
    change_size: int
    language: str
    path_tokens: list[str]
    labels: list[str]
    linked_jira_keys: list[str]


class HistorySignals(TypedDict, total=False):
    repo_key: str
    frequently_changed_files: list[dict[str, Any]]
    files_changed_together: list[dict[str, Any]]
    hotspot_paths: list[dict[str, Any]]
    owner_activity: list[dict[str, Any]]
    review_latency_by_area: list[dict[str, Any]]
    jira_key_frequency: list[dict[str, Any]]
    updated_at: str


class OnboardingRun(TypedDict, total=False):
    onboarding_run_id: str
    repo_key: str
    status: str  # "queued" | "running" | "completed" | "failed"
    created_at: str
    updated_at: str
    started_at: str
    completed_at: str
    scan: dict[str, Any]
    storage_mode: str
    summary: dict[str, Any]
    warnings: list[str]
    errors: list[str]
