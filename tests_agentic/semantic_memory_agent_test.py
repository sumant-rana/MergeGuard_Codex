from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

from packages.mongo import LocalMergeGuardStore
from packages.orchestration.engine import AGENT_SEQUENCE, MergeGuardOrchestrator


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_semantic_memory_agent() -> ModuleType:
    path = (
        REPO_ROOT
        / "agents/semantic-evidence-agent/src/semantic_evidence_agent/main.py"
    )
    spec = importlib.util.spec_from_file_location("semantic_evidence_agent_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load semantic evidence agent from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SemanticEvidenceAgentTest(unittest.TestCase):
    def test_agent_retrieves_repository_test_memory_for_pr_intent(self) -> None:
        module = load_semantic_memory_agent()
        payload = json.loads((REPO_ROOT / "fixtures/agentic/demo_pr.json").read_text())
        payload["analysis_run_id"] = "run-test-memory"
        payload["prior_results"] = {
            "review-compression": {
                "output": {
                    "files": [
                        {
                            "path": "payments/refund_processor.ts",
                            "classification": "security-sensitive",
                            "risk_score": 72,
                            "risk_reasons": ["risk keywords: refund, retry, pii"],
                            "status": "modified",
                        }
                    ]
                }
            },
            "intent-extractor": {
                "output": {
                    "intent_items": [
                        {
                            "id": "intent-1",
                            "text": "Should retry transient refund failures without exposing customer PII",
                            "category": "should",
                            "terms": ["retry", "refund", "customer", "pii"],
                        }
                    ]
                }
            },
        }

        result = module.app.invoke(payload)

        self.assertEqual(result["agent_id"], "semantic-evidence-agent")
        self.assertEqual(result["status"], "completed")
        self.assertGreaterEqual(result["output"]["index"]["records_stored"], 1)
        self.assertIn(
            "payments/refund_processor.test.ts",
            [item["path"] for item in result["output"]["related_tests"]],
        )
        self.assertEqual(result["output"]["requirement_evidence"][0]["status"], "found")

    def test_orchestrator_runs_memory_agent_before_downstream_evidence(self) -> None:
        self.assertLess(
            AGENT_SEQUENCE.index("semantic-evidence-agent"),
            AGENT_SEQUENCE.index("evidence-mapper"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalMergeGuardStore(Path(tmp) / "store.json")
            store.load()
            payload = json.loads((REPO_ROOT / "fixtures/agentic/demo_pr.json").read_text())
            run = MergeGuardOrchestrator(REPO_ROOT, store).analyze_demo_pr(payload)

        self.assertEqual(run["state"], "completed")
        self.assertIn("semantic-evidence-agent", run["agent_results"])
        self.assertTrue(run["summary"]["related_tests"])
        self.assertIn(
            "MergeGuard / Repository Memory",
            [check["name"] for check in run["summary"]["checks"]],
        )


if __name__ == "__main__":
    unittest.main()

