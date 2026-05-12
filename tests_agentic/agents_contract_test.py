from __future__ import annotations

import json
import unittest
from pathlib import Path

from packages.agent_runtime.magenta_compat import _payload_from_message, _payload_from_state
from packages.orchestration import LocalPlatformClient


class AgentContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.payload = json.loads((cls.repo_root / "fixtures/agentic/demo_pr.json").read_text())
        cls.platform = LocalPlatformClient(cls.repo_root)

    def invoke(self, agent_id: str, prior: dict | None = None) -> dict:
        payload = {
            "analysis_run_id": "test-run",
            "pull_request": self.payload["pull_request"],
            "changed_files": self.payload["changed_files"],
            "settings": self.payload["settings"],
            "prior_results": prior or {},
        }
        return self.platform.invoke(agent_id, payload, thread_id="test-run")["result"]

    def test_review_compression_contract(self) -> None:
        result = self.invoke("review-compression")
        output = result["output"]
        self.assertEqual(result["agent_id"], "review-compression")
        self.assertGreaterEqual(output["risk_score"], 80)
        self.assertTrue(output["must_inspect"])
        self.assertTrue(output["safe_to_skim"])

    def test_intent_and_evidence_contract(self) -> None:
        compression = self.invoke("review-compression")
        intent = self.invoke("intent-extractor")
        evidence = self.invoke(
            "evidence-mapper",
            {"review-compression": compression, "intent-extractor": intent},
        )
        self.assertGreaterEqual(len(intent["output"]["intent_items"]), 4)
        self.assertTrue(evidence["output"]["evidence_links"])
        self.assertTrue(evidence["output"]["missing_evidence_findings"])

    def test_semantic_diff_contract(self) -> None:
        compression = self.invoke("review-compression")
        result = self.invoke("semantic-diff-explainer", {"review-compression": compression})
        self.assertTrue(result["output"]["behavioral_deltas"])
        self.assertTrue(result["output"]["blast_radius"])

    def test_concept_and_policy_contract(self) -> None:
        compression = self.invoke("review-compression")
        concepts = self.invoke("concept-classifier", {"review-compression": compression})
        policy = self.invoke("policy-gate", {"concept-classifier": concepts})
        self.assertTrue(concepts["output"]["concept_findings"])
        self.assertEqual(policy["output"]["policy_status"], "block")

    def test_prompt_and_contract_contract(self) -> None:
        compression = self.invoke("review-compression")
        prompt = self.invoke("prompt-canary", {"review-compression": compression})
        contract = self.invoke("contract-comparator")
        self.assertEqual(prompt["output"]["prompt_canary_runs"][0]["status"], "fail")
        self.assertTrue(contract["output"]["contract_findings"])

    def test_platform_message_payload_contract(self) -> None:
        payload = {"payload": {"analysis_run_id": "run-1", "changed_files": []}}
        self.assertEqual(_payload_from_message({"content": json.dumps(payload)}), payload["payload"])
        self.assertEqual(
            _payload_from_state({"messages": [{"content": json.dumps(payload)}]}),
            payload["payload"],
        )


if __name__ == "__main__":
    unittest.main()
