from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packages.mongo import LocalMergeGuardStore
from packages.orchestration.engine import MergeGuardOrchestrator


class AgenticPipelineTest(unittest.TestCase):
    def test_end_to_end_agent_sequence(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        payload = json.loads((repo_root / "fixtures/agentic/demo_pr.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalMergeGuardStore(Path(tmp) / "store.json")
            store.load()
            run = MergeGuardOrchestrator(repo_root, store).analyze_demo_pr(payload)
            summary = run["summary"]
            self.assertEqual(run["state"], "completed")
            self.assertEqual(len(run["agent_results"]), 9)
            self.assertGreaterEqual(summary["risk_score"], 90)
            self.assertEqual(summary["status"], "blocked")
            self.assertTrue(summary["prompt_findings"])
            self.assertTrue(summary["contract_findings"])
            self.assertTrue(summary["policy_findings"])
            self.assertEqual(len(summary["checks"]), 7)

    def test_disabled_agents_are_recorded_as_skipped(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        payload = json.loads((repo_root / "fixtures/agentic/demo_pr.json").read_text())
        enabled_agents = [
            "review-compression",
            "intent-extractor",
            "semantic-diff-explainer",
            "concept-classifier",
            "policy-gate",
            "evidence-mapper",
            "truth-report-synthesizer",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalMergeGuardStore(Path(tmp) / "store.json")
            store.load()
            run = MergeGuardOrchestrator(repo_root, store).analyze_demo_pr(
                payload,
                enabled_agents=enabled_agents,
            )

        self.assertEqual(run["state"], "completed")
        self.assertEqual(run["agent_results"]["prompt-canary"]["status"], "skipped")
        self.assertEqual(run["agent_results"]["contract-comparator"]["status"], "skipped")
        self.assertEqual(
            run["summary"]["disabled_agents"],
            ["prompt-canary", "contract-comparator"],
        )
        self.assertFalse(run["summary"].get("prompt_findings"))
        self.assertFalse(run["summary"].get("contract_findings"))


if __name__ == "__main__":
    unittest.main()
