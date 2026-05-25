"""Unit tests for historical-signal aggregation over normalized PR records."""

from __future__ import annotations

import unittest

from packages.history_store.signals import compute_history_signals


def _pr(repo_key: str, number: int, *, labels=None, jira=None, author="dev") -> dict:
    return {
        "repo_key": repo_key,
        "pr_number": number,
        "state": "merged",
        "labels": list(labels or []),
        "linked_jira_keys": list(jira or []),
        "author": author,
        "reviewers": ["reviewer-1"],
    }


def _file(repo_key: str, number: int, path: str, *, labels=None, jira=None) -> dict:
    return {
        "repo_key": repo_key,
        "pr_number": number,
        "path": path,
        "status": "modified",
        "additions": 10,
        "deletions": 2,
        "change_size": 12,
        "language": "typescript",
        "path_tokens": ["src"],
        "labels": list(labels or []),
        "linked_jira_keys": list(jira or []),
    }


class HistorySignalsTest(unittest.TestCase):
    repo_key = "mongodb/example-service"

    def _build_dataset(self) -> tuple[list[dict], list[dict]]:
        prs = [
            _pr(self.repo_key, 1, labels=["bug"], jira=["PAY-1"]),
            _pr(self.repo_key, 2, labels=["bug", "payments"], jira=["PAY-2"]),
            _pr(self.repo_key, 3, labels=["payments"], jira=["PAY-3"]),
            _pr(self.repo_key, 4, labels=["docs"]),
        ]
        files = [
            _file(self.repo_key, 1, "src/payments/retry.ts", labels=["bug"], jira=["PAY-1"]),
            _file(self.repo_key, 1, "tests/payments/retry.test.ts"),
            _file(self.repo_key, 2, "src/payments/retry.ts", labels=["bug", "payments"], jira=["PAY-2"]),
            _file(self.repo_key, 2, "tests/payments/retry.test.ts"),
            _file(self.repo_key, 3, "src/payments/retry.ts", labels=["payments"], jira=["PAY-3"]),
            _file(self.repo_key, 4, "docs/payments.md"),
        ]
        return prs, files

    def test_frequently_changed_files_sorted_desc(self) -> None:
        prs, files = self._build_dataset()
        signals = compute_history_signals(self.repo_key, prs, files)
        top = signals["frequently_changed_files"][0]
        self.assertEqual(top["path"], "src/payments/retry.ts")
        self.assertEqual(top["count"], 3)

    def test_files_changed_together_finds_pairs(self) -> None:
        prs, files = self._build_dataset()
        signals = compute_history_signals(self.repo_key, prs, files)
        pairs = {(tuple(sorted(p["paths"])), p["count"]) for p in signals["files_changed_together"]}
        self.assertIn(
            (
                ("src/payments/retry.ts", "tests/payments/retry.test.ts"),
                2,
            ),
            pairs,
        )

    def test_hotspot_paths_weights_frequency_and_bug_labels(self) -> None:
        prs, files = self._build_dataset()
        signals = compute_history_signals(self.repo_key, prs, files)
        hotspots = signals["hotspot_paths"]
        self.assertTrue(hotspots)
        top = hotspots[0]
        self.assertEqual(top["path"], "src/payments/retry.ts")
        self.assertGreaterEqual(top["score"], 50)
        self.assertIn("bug labels", " ".join(top["reasons"]).lower() if top["reasons"] else "")

    def test_owner_activity_counts_per_author(self) -> None:
        prs, files = self._build_dataset()
        signals = compute_history_signals(self.repo_key, prs, files)
        owners = {entry["owner"]: entry["pr_count"] for entry in signals["owner_activity"]}
        self.assertEqual(owners.get("dev"), 4)

    def test_jira_key_frequency_groups_by_project(self) -> None:
        prs, files = self._build_dataset()
        signals = compute_history_signals(self.repo_key, prs, files)
        projects = {entry["project"]: entry["count"] for entry in signals["jira_key_frequency"]}
        self.assertEqual(projects.get("PAY"), 3)

    def test_repo_key_and_updated_at_present(self) -> None:
        prs, files = self._build_dataset()
        signals = compute_history_signals(self.repo_key, prs, files)
        self.assertEqual(signals["repo_key"], self.repo_key)
        self.assertIn("updated_at", signals)


if __name__ == "__main__":
    unittest.main()
