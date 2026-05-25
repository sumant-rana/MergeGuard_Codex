from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


AGENT_MODULES = {
    "review-compression": "agents/review-compression/src/review_compression/main.py",
    "intent-extractor": "agents/intent-extractor/src/intent_extractor/main.py",
    "evidence-mapper": "agents/evidence-mapper/src/evidence_mapper/main.py",
    "semantic-diff-explainer": "agents/semantic-diff-explainer/src/semantic_diff_explainer/main.py",
    "semantic-evidence-agent": "agents/semantic-evidence-agent/src/semantic_evidence_agent/main.py",
    "concept-classifier": "agents/concept-classifier/src/concept_classifier/main.py",
    "slop-detector": "agents/slop-detector/src/slop_detector/main.py",
    "policy-gate": "agents/policy-gate/src/policy_gate/main.py",
    "prompt-canary": "agents/prompt-canary/src/prompt_canary/main.py",
    "contract-comparator": "agents/contract-comparator/src/contract_comparator/main.py",
    "test-coverage-validator": "agents/test-coverage-validator/src/test_coverage_validator/main.py",
    "truth-report-synthesizer": "agents/truth-report-synthesizer/src/truth_report_synthesizer/main.py",
    "pr-history-indexer": "agents/pr-history-indexer/src/pr_history_indexer/main.py",
    "docs-indexer": "agents/docs-indexer/src/docs_indexer/main.py",
}


class LocalPlatformClient:
    """Local stand-in for Magenta OE /invoke.

    The production worker will call the Agentic Platform `/invoke` API. In demo
    mode this client loads each deployable agent's entrypoint and records an
    OE-shaped execution envelope.
    """

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root)
        self._modules: dict[str, ModuleType] = {}
        self.executions: list[dict[str, Any]] = []

    def invoke(self, agent_id: str, payload: dict[str, Any], *, thread_id: str) -> dict[str, Any]:
        module = self._load(agent_id)
        result = module.app.invoke(payload)
        execution = {
            "execution_id": f"local-{len(self.executions) + 1}",
            "thread_id": thread_id,
            "agent_id": agent_id,
            "status": result.get("status", "completed"),
            "result": result,
        }
        self.executions.append(execution)
        return execution

    def _load(self, agent_id: str) -> ModuleType:
        if agent_id in self._modules:
            return self._modules[agent_id]
        if agent_id not in AGENT_MODULES:
            raise KeyError(f"Unknown agent: {agent_id}")
        path = self.repo_root / AGENT_MODULES[agent_id]
        spec = importlib.util.spec_from_file_location(f"mergeguard_agent_{agent_id.replace('-', '_')}", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load agent module: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._modules[agent_id] = module
        return module
