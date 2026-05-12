from __future__ import annotations

from pathlib import Path
from typing import Any

from packages.core.models import AgentEnvelope, Repository, to_plain
from packages.mongo import LocalMergeGuardStore
from packages.orchestration import LocalPlatformClient


AGENT_SEQUENCE = [
    "review-compression",
    "intent-extractor",
    "semantic-diff-explainer",
    "concept-classifier",
    "policy-gate",
    "prompt-canary",
    "contract-comparator",
    "evidence-mapper",
    "truth-report-synthesizer",
]


class MergeGuardOrchestrator:
    def __init__(self, repo_root: str | Path, store: LocalMergeGuardStore) -> None:
        self.repo_root = Path(repo_root)
        self.store = store
        self.platform = LocalPlatformClient(self.repo_root)

    def analyze_demo_pr(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.analyze_pull_request(payload)

    def analyze_pull_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        repository = payload["repository"]
        repository["full_name"] = repository.get("full_name") or f"{repository['owner']}/{repository['name']}"
        pr_payload = payload["pull_request"]
        pr_record = self.store.upsert_pull_request(
            {
                **pr_payload,
                "number": pr_payload["number"],
                "title": pr_payload["title"],
                "body": pr_payload.get("body", ""),
                "author": pr_payload.get("author", "unknown"),
                "base_sha": pr_payload.get("base_sha", "base"),
                "head_sha": pr_payload.get("head_sha", "head"),
                "repository": repository,
                "labels": pr_payload.get("labels", []),
            }
        )
        run = self.store.create_analysis_run(
            pr_record["id"],
            pr_record["head_sha"],
            pull_request=pr_record,
        )
        prior_results: dict[str, Any] = {}
        settings = payload.get("settings", {})
        try:
            for agent_id in AGENT_SEQUENCE:
                envelope = AgentEnvelope(
                    analysis_run_id=run["id"],
                    pull_request=pr_record,
                    changed_files=payload.get("changed_files", []),
                    prior_results=prior_results,
                    settings=settings,
                )
                execution = self.platform.invoke(
                    agent_id,
                    to_plain(envelope),
                    thread_id=run["id"],
                )
                self.store.record_agent_execution(run["id"], execution)
                prior_results[agent_id] = execution["result"]
            summary = prior_results["truth-report-synthesizer"]["output"]["summary"]
            return self.store.complete_run(run["id"], summary)
        except Exception as exc:
            return self.store.fail_run(run["id"], f"{type(exc).__name__}: {exc}")


def repository_from_payload(payload: dict[str, Any]) -> Repository:
    repo = payload["repository"]
    return Repository(
        owner=repo["owner"],
        name=repo["name"],
        default_branch=repo.get("default_branch", "main"),
    )
