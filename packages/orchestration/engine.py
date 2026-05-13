from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from packages.core.models import AgentEnvelope, Repository, to_plain
from packages.mongo import LocalMergeGuardStore
from packages.orchestration import LocalPlatformClient


AGENT_SEQUENCE = [
    "review-compression",
    "intent-extractor",
    "semantic-diff-explainer",
    "concept-classifier",
    "slop-detector",
    "policy-gate",
    "prompt-canary",
    "contract-comparator",
    "semantic-evidence-agent",
    "evidence-mapper",
    "test-coverage-validator",
    "truth-report-synthesizer",
]

AGENT_CATALOG = [
    {
        "id": "review-compression",
        "label": "Change Triage",
        "stage": "Triage",
        "description": "Ranks changed files by review risk, owners, hotspots, and skim-safe areas.",
    },
    {
        "id": "intent-extractor",
        "label": "Intent Extractor",
        "stage": "Intent",
        "description": "Extracts should, must-not, and out-of-scope claims from PR text.",
    },
    {
        "id": "semantic-diff-explainer",
        "label": "Behavior Impact",
        "stage": "Behavior",
        "description": "Explains runtime behavior changes, divergent examples, and blast radius.",
    },
    {
        "id": "concept-classifier",
        "label": "Concept Classifier",
        "stage": "Concepts",
        "description": "Tags risky concepts such as PII writes, billing effects, and HTTP calls.",
    },
    {
        "id": "slop-detector",
        "label": "Slop Detector",
        "stage": "Review Hygiene",
        "description": "Flags debug leftovers, placeholders, weak tests, and noisy files for removal or rework.",
    },
    {
        "id": "policy-gate",
        "label": "Policy Guardrails",
        "stage": "Policy",
        "description": "Enforces team review rules and owner override requirements.",
    },
    {
        "id": "prompt-canary",
        "label": "Prompt Drift Check",
        "stage": "Prompt",
        "description": "Checks prompt, model, format, refusal, latency, and cost drift.",
    },
    {
        "id": "contract-comparator",
        "label": "Contract Comparator",
        "stage": "Contracts",
        "description": "Compares runtime contract shapes and suggests property tests.",
    },
    {
        "id": "semantic-evidence-agent",
        "label": "Repository Memory",
        "stage": "Memory",
        "description": "Retrieves related tests, docs, prior PRs, and policy context from Magenta memory.",
    },
    {
        "id": "evidence-mapper",
        "label": "Verification Evidence",
        "stage": "Evidence",
        "description": "Links requirements and risky changes to tests, evidence, or missing proof.",
    },
    {
        "id": "test-coverage-validator",
        "label": "Test Coverage Validator",
        "stage": "Tests",
        "description": "Checks whether changed tests cover PR intent, behavior, and source changes.",
    },
    {
        "id": "truth-report-synthesizer",
        "label": "Truth Report",
        "stage": "Report",
        "description": "Synthesizes all prior outputs into readiness, checks, and PR comment.",
    },
]


class MergeGuardOrchestrator:
    def __init__(self, repo_root: str | Path, store: LocalMergeGuardStore) -> None:
        self.repo_root = Path(repo_root)
        self.store = store
        self.platform = LocalPlatformClient(self.repo_root)

    def analyze_demo_pr(
        self,
        payload: dict[str, Any],
        *,
        enabled_agents: list[str] | None = None,
        agent_delay_ms: int = 0,
    ) -> dict[str, Any]:
        return self.analyze_pull_request(
            payload,
            enabled_agents=enabled_agents,
            agent_delay_ms=agent_delay_ms,
        )

    def analyze_pull_request(
        self,
        payload: dict[str, Any],
        *,
        enabled_agents: list[str] | None = None,
        agent_delay_ms: int = 0,
    ) -> dict[str, Any]:
        repository = payload["repository"]
        repository["full_name"] = (
            repository.get("full_name") or f"{repository['owner']}/{repository['name']}"
        )
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
            input_payload=payload,
        )
        prior_results: dict[str, Any] = {}
        settings = payload.get("settings", {})
        enabled = normalize_enabled_agents(enabled_agents)
        delay_seconds = max(0, min(int(agent_delay_ms or 0), 3_000)) / 1000
        try:
            for index, agent_id in enumerate(AGENT_SEQUENCE):
                if agent_id not in enabled:
                    skipped = skipped_agent_execution(agent_id, run["id"])
                    self.store.record_agent_execution(run["id"], skipped)
                    prior_results[agent_id] = skipped["result"]
                    continue

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
                if delay_seconds and index < len(AGENT_SEQUENCE) - 1:
                    time.sleep(delay_seconds)
            summary = summary_from_results(prior_results, enabled)
            return self.store.complete_run(run["id"], summary)
        except Exception as exc:
            return self.store.fail_run(run["id"], f"{type(exc).__name__}: {exc}")


def normalize_enabled_agents(enabled_agents: list[str] | None) -> set[str]:
    if not enabled_agents:
        return set(AGENT_SEQUENCE)
    known = set(AGENT_SEQUENCE)
    enabled = {agent_id for agent_id in enabled_agents if agent_id in known}
    return enabled or set(AGENT_SEQUENCE)


def skipped_agent_execution(agent_id: str, run_id: str) -> dict[str, Any]:
    result = {
        "agent_id": agent_id,
        "status": "skipped",
        "confidence": 1.0,
        "messages": ["skipped by dashboard agent toggle"],
        "output": {"skipped": True, "reason": "disabled by dashboard toggle"},
        "trace": [{"step": "skip", "reason": "disabled"}],
    }
    return {
        "execution_id": f"local-skip-{agent_id}",
        "thread_id": run_id,
        "agent_id": agent_id,
        "status": "skipped",
        "result": result,
    }


def summary_from_results(prior_results: dict[str, Any], enabled: set[str]) -> dict[str, Any]:
    truth = prior_results.get("truth-report-synthesizer", {})
    summary = truth.get("output", {}).get("summary")
    if summary:
        summary["enabled_agents"] = sorted(enabled, key=AGENT_SEQUENCE.index)
        summary["disabled_agents"] = [
            agent_id for agent_id in AGENT_SEQUENCE if agent_id not in enabled
        ]
        return summary

    compression = prior_results.get("review-compression", {}).get("output", {})
    disabled = [agent_id for agent_id in AGENT_SEQUENCE if agent_id not in enabled]
    return {
        "risk_score": compression.get("risk_score", 0),
        "status": "review" if disabled else "pass",
        "top_blocker": (
            "Truth report synthesizer disabled."
            if "truth-report-synthesizer" in disabled
            else None
        ),
        "next_action": "Enable the truth-report-synthesizer agent to generate final readiness.",
        "hotspots": compression.get("hotspots", []),
        "must_inspect": compression.get("must_inspect", []),
        "safe_to_skim": compression.get("safe_to_skim", []),
        "owner_summary": compression.get("owner_summary", []),
        "hotspot_themes": compression.get("hotspot_themes", []),
        "test_coverage": prior_results.get("test-coverage-validator", {}).get("output", {}),
        "semantic_memory": prior_results.get("semantic-evidence-agent", {}).get("output", {}),
        "memory_matches": prior_results.get("semantic-evidence-agent", {})
        .get("output", {})
        .get("semantic_matches", []),
        "memory_evidence": prior_results.get("semantic-evidence-agent", {})
        .get("output", {})
        .get("requirement_evidence", []),
        "slop": prior_results.get("slop-detector", {}).get("output", {}),
        "slop_findings": prior_results.get("slop-detector", {})
        .get("output", {})
        .get("slop_findings", []),
        "remove_candidates": prior_results.get("slop-detector", {})
        .get("output", {})
        .get("remove_candidates", []),
        "rework_candidates": prior_results.get("slop-detector", {})
        .get("output", {})
        .get("rework_candidates", []),
        "test_coverage_findings": prior_results.get("test-coverage-validator", {})
        .get("output", {})
        .get("coverage_findings", []),
        "checks": [],
        "comment": "",
        "enabled_agents": sorted(enabled, key=AGENT_SEQUENCE.index),
        "disabled_agents": disabled,
    }


def repository_from_payload(payload: dict[str, Any]) -> Repository:
    repo = payload["repository"]
    return Repository(
        owner=repo["owner"],
        name=repo["name"],
        default_branch=repo.get("default_branch", "main"),
    )
