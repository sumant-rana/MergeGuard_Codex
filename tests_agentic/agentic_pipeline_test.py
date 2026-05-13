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
            self.assertEqual(len(run["agent_results"]), 12)
            self.assertGreaterEqual(summary["risk_score"], 90)
            self.assertEqual(summary["status"], "blocked")
            self.assertTrue(summary["prompt_findings"])
            self.assertTrue(summary["contract_findings"])
            self.assertTrue(summary["policy_findings"])
            self.assertTrue(summary["slop_findings"])
            self.assertTrue(summary["test_coverage_findings"])
            self.assertTrue(summary["related_tests"])
            self.assertEqual(len(summary["checks"]), 10)

    def test_disabled_agents_are_recorded_as_skipped(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        payload = json.loads((repo_root / "fixtures/agentic/demo_pr.json").read_text())
        enabled_agents = [
            "review-compression",
            "intent-extractor",
            "semantic-diff-explainer",
            "concept-classifier",
            "slop-detector",
            "policy-gate",
            "semantic-evidence-agent",
            "evidence-mapper",
            "test-coverage-validator",
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

    def test_analysis_run_stores_original_payload_for_pr_rerun(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        payload = json.loads((repo_root / "fixtures/agentic/demo_pr.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalMergeGuardStore(Path(tmp) / "store.json")
            store.load()
            run = MergeGuardOrchestrator(repo_root, store).analyze_demo_pr(payload)

            stored_pr = store.get_pull_request(run["pull_request_id"])
            stored_payload = store.latest_input_payload_for_pr(run["pull_request_id"])
            queue_row = store.queue()[0]

        self.assertIsNotNone(stored_pr)
        self.assertIsNotNone(stored_payload)
        self.assertEqual(stored_payload["pull_request"]["title"], payload["pull_request"]["title"])
        self.assertEqual(stored_payload["changed_files"][0]["path"], payload["changed_files"][0]["path"])
        self.assertEqual(queue_row["pull_request"]["id"], run["pull_request_id"])
        self.assertEqual(queue_row["latest_run"]["id"], run["id"])

    def test_stored_payload_can_drive_followup_analysis(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        payload = json.loads((repo_root / "fixtures/agentic/demo_pr.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalMergeGuardStore(Path(tmp) / "store.json")
            store.load()
            orchestrator = MergeGuardOrchestrator(repo_root, store)
            first_run = orchestrator.analyze_demo_pr(payload)
            stored_payload = store.latest_input_payload_for_pr(first_run["pull_request_id"])
            second_run = orchestrator.analyze_pull_request(stored_payload)

            latest = store.latest_run_for_pr(first_run["pull_request_id"])

        self.assertEqual(second_run["state"], "completed")
        self.assertNotEqual(first_run["id"], second_run["id"])
        self.assertEqual(second_run["pull_request_id"], first_run["pull_request_id"])
        self.assertEqual(latest["id"], second_run["id"])


if __name__ == "__main__":
    unittest.main()
