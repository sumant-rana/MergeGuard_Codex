from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from packages.github_pr import normalize_github_pr_payload
from packages.mongo import LocalMergeGuardStore
from packages.orchestration.engine import MergeGuardOrchestrator


class GitHubPrIngestTest(unittest.TestCase):
    def test_normalized_github_pr_payload_runs_full_pipeline(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        payload = normalize_github_pr_payload(
            {
                "repository": {
                    "owner": "acme",
                    "name": "checkout",
                    "default_branch": "main",
                },
                "pull_request": {
                    "number": 42,
                    "title": "Fix refund retry issue",
                    "body": "Fixes #19. Ensure retry failure stays idempotent.",
                    "author": {"login": "alice"},
                    "base_sha": "base-sha",
                    "head_sha": "head-sha",
                    "base_ref": "main",
                    "head_ref": "refund-retry",
                    "issue_refs": [{"number": 19, "title": "Refund retry fails", "state": "open"}],
                    "commit_history": [
                        {
                            "oid": "abc123456789",
                            "message": "Fix refund retry idempotency",
                            "authored_date": "2026-05-12T00:00:00Z",
                        }
                    ],
                },
                "changed_files": [
                    {
                        "path": "payments/refund_retry.ts",
                        "additions": 12,
                        "deletions": 2,
                        "patch": "@@\n+export function retryRefund() { return retry(idempotentRefund) }\n",
                    }
                ],
                "settings": {"codeowners": "payments/ @payments-team"},
            }
        )

        self.assertIn("Commit history", payload["pull_request"]["analysis_context"])

        with tempfile.TemporaryDirectory() as tmp:
            store = LocalMergeGuardStore(Path(tmp) / "store.json")
            store.load()
            run = MergeGuardOrchestrator(repo_root, store).analyze_pull_request(payload)

        self.assertEqual(run["state"], "completed")
        self.assertEqual(len(run["agent_results"]), 9)
        self.assertEqual(run["pull_request"]["base_ref"], "main")
        self.assertEqual(run["pull_request"]["issue_refs"][0]["number"], 19)

        intent_items = run["agent_results"]["intent-extractor"]["output"]["intent_items"]
        intent_text = "\n".join(item["text"] for item in intent_items)
        self.assertIn("Fixes issue #19", intent_text)
        self.assertIn("Fix refund retry idempotency", intent_text)


if __name__ == "__main__":
    unittest.main()
