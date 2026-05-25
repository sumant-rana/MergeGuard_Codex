"""Unit tests for the GitHub REST-based PR history collector.

The collector is a pure function over a transport callable that returns
JSON pages. Tests pin the normalization, pagination, scan limits, and
jira-key extraction without touching the network.
"""

from __future__ import annotations

import unittest

from packages.github_pr.pr_history_collector import (
    collect_pr_history,
    extract_jira_keys,
    normalize_pr,
    normalize_pr_file,
    tokenize_path,
)


class FakeTransport:
    """Records each GET and returns scripted JSON responses by URL prefix."""

    def __init__(self, routes: dict[str, list]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str, token: str) -> object:
        self.calls.append(url)
        for prefix, response in self.routes.items():
            if url.startswith(prefix):
                return response
        return []


PR_PAGE = [
    {
        "number": 101,
        "title": "Fix retry timeout PAYMENTS-42",
        "body": "Resolves PAYMENTS-42. Closes #5.",
        "state": "closed",
        "merged_at": "2026-02-01T12:00:00Z",
        "closed_at": "2026-02-01T12:00:00Z",
        "created_at": "2026-01-31T09:00:00Z",
        "user": {"login": "alice"},
        "labels": [{"name": "bug"}, {"name": "payments"}],
        "html_url": "https://github.com/mongodb/example-service/pull/101",
        "requested_reviewers": [{"login": "bob"}],
    },
    {
        "number": 100,
        "title": "Docs update",
        "body": "",
        "state": "closed",
        "merged_at": None,
        "user": {"login": "carol"},
        "labels": [],
        "html_url": "https://github.com/mongodb/example-service/pull/100",
    },
]

FILES_FOR_101 = [
    {"filename": "src/payments/retry.ts", "status": "modified", "additions": 42, "deletions": 10},
    {"filename": "tests/payments/retry.test.ts", "status": "added", "additions": 30, "deletions": 0},
]

FILES_FOR_100 = [
    {"filename": "docs/readme.md", "status": "modified", "additions": 5, "deletions": 1},
]


class PRHistoryCollectorTest(unittest.TestCase):
    def test_extract_jira_keys_finds_keys_in_text(self) -> None:
        text = "Fix PAYMENTS-42 follows up on AUTH-9 and DATA-103."
        self.assertEqual(
            extract_jira_keys(text),
            ["PAYMENTS-42", "AUTH-9", "DATA-103"],
        )

    def test_tokenize_path_splits_on_slash_dot_and_dashes(self) -> None:
        self.assertEqual(
            tokenize_path("src/payments/retry-handler.test.ts"),
            ["src", "payments", "retry", "handler", "test", "ts"],
        )

    def test_normalize_pr_sets_required_fields(self) -> None:
        repo_key = "mongodb/example-service"
        pr = normalize_pr(repo_key, PR_PAGE[0])
        self.assertEqual(pr["repo_key"], repo_key)
        self.assertEqual(pr["pr_number"], 101)
        self.assertEqual(pr["state"], "merged")  # merged_at present → merged
        self.assertIn("PAYMENTS-42", pr["linked_jira_keys"])
        self.assertEqual(pr["author"], "alice")
        self.assertIn("bug", pr["labels"])

    def test_normalize_pr_file_classifies_language_and_tokens(self) -> None:
        pr_file = normalize_pr_file(
            "mongodb/example-service",
            101,
            FILES_FOR_101[0],
            labels=["bug"],
            linked_jira_keys=["PAYMENTS-42"],
        )
        self.assertEqual(pr_file["language"], "typescript")
        self.assertEqual(pr_file["path"], "src/payments/retry.ts")
        self.assertEqual(pr_file["change_size"], 52)
        self.assertIn("payments", pr_file["path_tokens"])
        self.assertIn("PAYMENTS-42", pr_file["linked_jira_keys"])

    def test_collect_pr_history_respects_max_prs(self) -> None:
        transport = FakeTransport(
            {
                "https://api.github.com/repos/mongodb/example-service/pulls": PR_PAGE,
                "https://api.github.com/repos/mongodb/example-service/pulls/101/files": FILES_FOR_101,
                "https://api.github.com/repos/mongodb/example-service/pulls/100/files": FILES_FOR_100,
            }
        )

        result = collect_pr_history(
            repo_full_name="mongodb/example-service",
            token="t",
            transport=transport,
            max_prs=1,
            include_files=True,
            states=["merged", "closed"],
            since=None,
        )

        self.assertEqual(len(result["prs"]), 1)
        self.assertEqual(result["prs"][0]["pr_number"], 101)
        self.assertEqual(len(result["files"]), 2)
        self.assertEqual(result["scan_summary"]["prs_seen"], 2)
        self.assertEqual(result["scan_summary"]["prs_indexed"], 1)

    def test_collect_pr_history_filters_unsupported_states(self) -> None:
        open_pr = {**PR_PAGE[1], "number": 200, "state": "open", "merged_at": None}
        transport = FakeTransport(
            {
                "https://api.github.com/repos/mongodb/example-service/pulls": [open_pr, *PR_PAGE],
                "https://api.github.com/repos/mongodb/example-service/pulls/101/files": FILES_FOR_101,
                "https://api.github.com/repos/mongodb/example-service/pulls/100/files": FILES_FOR_100,
            }
        )

        result = collect_pr_history(
            repo_full_name="mongodb/example-service",
            token="t",
            transport=transport,
            max_prs=10,
            include_files=True,
            states=["merged"],
            since=None,
        )

        states = {pr["state"] for pr in result["prs"]}
        self.assertEqual(states, {"merged"})

    def test_collect_pr_history_respects_since(self) -> None:
        transport = FakeTransport(
            {
                "https://api.github.com/repos/mongodb/example-service/pulls": PR_PAGE,
                "https://api.github.com/repos/mongodb/example-service/pulls/101/files": FILES_FOR_101,
                "https://api.github.com/repos/mongodb/example-service/pulls/100/files": FILES_FOR_100,
            }
        )

        result = collect_pr_history(
            repo_full_name="mongodb/example-service",
            token="t",
            transport=transport,
            max_prs=10,
            include_files=False,
            states=["merged", "closed"],
            since="2026-02-01T00:00:00Z",
        )

        numbers = [pr["pr_number"] for pr in result["prs"]]
        # PR 100 has no created_at and PR 101 was created 2026-01-31; both are before since
        # PR 100 has merged_at=None, PR 101 has merged_at=2026-02-01 → kept.
        self.assertEqual(numbers, [101])


if __name__ == "__main__":
    unittest.main()
