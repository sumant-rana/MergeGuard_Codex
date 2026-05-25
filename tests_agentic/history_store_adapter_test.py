"""Unit tests for the PR history storage adapter contract.

These tests pin the in-memory adapter behaviour and act as the contract
that any concrete adapter (MongoDB, Atlas) must satisfy.
"""

from __future__ import annotations

import unittest

from packages.history_store import (
    InMemoryPRHistoryStore,
    PRHistoryStore,
    pr_key,
    pr_file_key,
)


class InMemoryPRHistoryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store: PRHistoryStore = InMemoryPRHistoryStore()
        self.repo_key = "mongodb/example-service"

    def test_protocol_methods_exist(self) -> None:
        for method in (
            "upsert_repository",
            "start_onboarding_run",
            "upsert_prior_pr",
            "upsert_prior_pr_files",
            "save_history_signals",
            "complete_onboarding_run",
            "get_history_signals",
            "list_prior_prs",
        ):
            self.assertTrue(hasattr(self.store, method), f"missing {method}")

    def test_upsert_prior_pr_is_idempotent_on_repo_key_and_number(self) -> None:
        record = {
            "repo_key": self.repo_key,
            "pr_number": 123,
            "title": "Fix retry timeout",
            "state": "merged",
        }
        self.store.upsert_prior_pr(record)
        self.store.upsert_prior_pr({**record, "title": "Fix retry timeout (revised)"})

        prs = self.store.list_prior_prs(self.repo_key)
        self.assertEqual(len(prs), 1)
        self.assertEqual(prs[0]["title"], "Fix retry timeout (revised)")
        self.assertEqual(prs[0]["pr_number"], 123)

    def test_upsert_prior_pr_files_idempotent_per_path(self) -> None:
        files = [
            {
                "repo_key": self.repo_key,
                "pr_number": 99,
                "path": "src/a.py",
                "status": "modified",
                "additions": 10,
                "deletions": 1,
            },
            {
                "repo_key": self.repo_key,
                "pr_number": 99,
                "path": "src/b.py",
                "status": "added",
                "additions": 5,
                "deletions": 0,
            },
        ]
        self.store.upsert_prior_pr_files(files)
        # Re-upsert with one updated record.
        self.store.upsert_prior_pr_files(
            [
                {
                    "repo_key": self.repo_key,
                    "pr_number": 99,
                    "path": "src/a.py",
                    "status": "modified",
                    "additions": 22,
                    "deletions": 2,
                }
            ]
        )

        stored = sorted(self.store.list_prior_pr_files(self.repo_key, 99), key=lambda f: f["path"])
        self.assertEqual([f["path"] for f in stored], ["src/a.py", "src/b.py"])
        self.assertEqual(stored[0]["additions"], 22)

    def test_save_and_get_history_signals(self) -> None:
        self.store.save_history_signals(
            self.repo_key,
            {"hotspot_paths": [{"path": "src/x.py", "score": 80, "reasons": ["hot"]}]},
        )
        signals = self.store.get_history_signals(self.repo_key)
        self.assertEqual(signals["repo_key"], self.repo_key)
        self.assertEqual(signals["hotspot_paths"][0]["path"], "src/x.py")

    def test_onboarding_run_lifecycle(self) -> None:
        self.store.start_onboarding_run(
            {
                "onboarding_run_id": "onb_1",
                "repo_key": self.repo_key,
                "status": "running",
            }
        )
        self.store.complete_onboarding_run("onb_1", {"prs_indexed": 5, "status": "completed"})
        run = self.store.get_onboarding_run("onb_1")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["summary"]["prs_indexed"], 5)

    def test_pr_key_and_file_key_helpers_are_stable(self) -> None:
        self.assertEqual(pr_key(self.repo_key, 7), "mongodb/example-service#7")
        self.assertEqual(
            pr_file_key(self.repo_key, 7, "src/a.py"),
            "mongodb/example-service#7::src/a.py",
        )


if __name__ == "__main__":
    unittest.main()
