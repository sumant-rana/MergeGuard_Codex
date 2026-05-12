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


if __name__ == "__main__":
    unittest.main()
