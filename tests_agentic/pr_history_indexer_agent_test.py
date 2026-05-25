"""End-to-end behaviour test for the ``pr-history-indexer`` agent module.

The agent is wired up to talk to either a docker MongoDB (``local``
mode) or Atlas (``cloud`` mode). For unit tests we replace its
``PRHistoryStore`` and HTTP transport via the module-level injection
seam exposed by ``pr_history_indexer.main``.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _load_agent_module() -> ModuleType:
    path = (
        REPO_ROOT
        / "agents/pr-history-indexer/src/pr_history_indexer/main.py"
    )
    spec = importlib.util.spec_from_file_location("pr_history_indexer_test_main", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load agent module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PR_PAGE = [
    {
        "number": 11,
        "title": "Fix retry timeout PAY-1",
        "body": "Resolves PAY-1.",
        "state": "closed",
        "merged_at": "2026-02-01T12:00:00Z",
        "created_at": "2026-01-30T10:00:00Z",
        "user": {"login": "alice"},
        "labels": [{"name": "bug"}],
        "html_url": "https://github.com/mongodb/example-service/pull/11",
    },
    {
        "number": 12,
        "title": "Add retry test",
        "body": "",
        "state": "closed",
        "merged_at": "2026-02-05T12:00:00Z",
        "created_at": "2026-02-04T10:00:00Z",
        "user": {"login": "alice"},
        "labels": [],
        "html_url": "https://github.com/mongodb/example-service/pull/12",
    },
]
FILES_FOR_11 = [
    {"filename": "src/payments/retry.ts", "status": "modified", "additions": 12, "deletions": 4},
    {"filename": "tests/payments/retry.test.ts", "status": "added", "additions": 8, "deletions": 0},
]
FILES_FOR_12 = [
    {"filename": "src/payments/retry.ts", "status": "modified", "additions": 4, "deletions": 1},
    {"filename": "tests/payments/retry.test.ts", "status": "modified", "additions": 12, "deletions": 0},
]


class FakeTransport:
    def __init__(self) -> None:
        self.routes = {
            "https://api.github.com/repos/mongodb/example-service/pulls?": PR_PAGE,
            "https://api.github.com/repos/mongodb/example-service/pulls/11/files": FILES_FOR_11,
            "https://api.github.com/repos/mongodb/example-service/pulls/12/files": FILES_FOR_12,
        }

    def __call__(self, url: str, token: str) -> object:
        for prefix, response in self.routes.items():
            if url.startswith(prefix):
                return response
        if "/pulls?" in url:
            return PR_PAGE
        return []


def _base_payload(mode: str = "local") -> dict:
    return {
        "onboarding_run_id": "onb_abc",
        "repository": {
            "owner": "mongodb",
            "name": "example-service",
            "full_name": "mongodb/example-service",
            "default_branch": "main",
        },
        "source": {
            "provider": "github",
            "mode": "token",
            "api_base_url": "https://api.github.com",
        },
        "scan": {
            "max_prs": 10,
            "states": ["merged", "closed"],
            "include_files": True,
            "include_comments": False,
            "include_reviews": True,
            "include_linked_issues": True,
        },
        "storage": {
            "mode": mode,
            "repo_key": "mongodb/example-service",
        },
        "credentials": {
            "github_token": "test-token",
        },
    }


class PRHistoryIndexerAgentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_agent_module()

    def setUp(self) -> None:
        from packages.history_store import InMemoryPRHistoryStore

        self.store = InMemoryPRHistoryStore()

        def store_factory(payload: dict) -> "InMemoryPRHistoryStore":
            return self.store

        self.module.set_store_factory(store_factory)
        self.module.set_transport(FakeTransport())

    def tearDown(self) -> None:
        self.module.reset_overrides()

    def test_agent_rejects_unsupported_storage_mode(self) -> None:
        payload = _base_payload(mode="in-process")
        result = self.module.app.invoke(payload)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["output"]["errors"])
        self.assertIn("local", " ".join(result["output"]["errors"]).lower())

    def test_agent_rejects_missing_credentials(self) -> None:
        payload = _base_payload(mode="local")
        payload["credentials"].pop("github_token")
        result = self.module.app.invoke(payload)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(
            any("github_token" in err.lower() for err in result["output"]["errors"])
        )

    def test_agent_indexes_prs_and_persists_history(self) -> None:
        payload = _base_payload(mode="local")
        result = self.module.app.invoke(payload)

        self.assertEqual(result["agent_id"], "pr-history-indexer")
        self.assertEqual(result["status"], "completed")
        output = result["output"]
        self.assertEqual(output["mode"], "local")
        self.assertEqual(output["repository"], "mongodb/example-service")
        self.assertGreaterEqual(output["scan_summary"]["prs_indexed"], 2)
        self.assertTrue(output["retrieval_ready"])

        prs = self.store.list_prior_prs("mongodb/example-service")
        self.assertEqual({pr["pr_number"] for pr in prs}, {11, 12})
        files = self.store.list_prior_pr_files("mongodb/example-service")
        self.assertGreaterEqual(len(files), 4)
        signals = self.store.get_history_signals("mongodb/example-service")
        self.assertIsNotNone(signals)
        assert signals is not None  # for type narrowing
        self.assertTrue(signals["frequently_changed_files"])
        run = self.store.get_onboarding_run("onb_abc")
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run["status"], "completed")

    def test_agent_writes_semantic_records_metadata(self) -> None:
        payload = _base_payload(mode="local")
        result = self.module.app.invoke(payload)

        self.assertEqual(result["status"], "completed")
        memory_records = self.store.list_memory_record_metadata()
        # Two PRs ⇒ two ``prior_pr_summary`` records at minimum.
        self.assertGreaterEqual(len(memory_records), 2)
        types = {record["type"] for record in memory_records}
        self.assertIn("prior_pr_summary", types)

    def test_agent_embeddings_are_scoped_by_repo_key(self) -> None:
        """Every persisted semantic record must carry repo_key for scoped retrieval.

        ``semantic-evidence-agent`` (and the future hybrid
        ``review-compression``) searches Magenta memory with
        ``user_id=repo_key``. If the writer drops ``repo_key`` from the
        metadata or stops setting ``user_id``, retrieval silently
        returns nothing for the current repo. Pin both invariants here.
        """
        from packages.agent_runtime import LocalAgentApp

        payload = _base_payload(mode="local")
        result = self.module.app.invoke(payload)
        self.assertEqual(result["status"], "completed")

        repo_key = "mongodb/example-service"

        # 1. Persisted metadata records all carry repo_key.
        memory_records = self.store.list_memory_record_metadata()
        self.assertTrue(memory_records)
        for record in memory_records:
            self.assertEqual(record.get("repo_key"), repo_key)

        # 2. The actual Magenta memory writes used user_id=repo_key.
        memory = getattr(self.module.app, "memory", None)
        if isinstance(self.module.app, LocalAgentApp) and memory is not None:
            stored_records = memory._records  # noqa: SLF001 - test introspection
            assert stored_records, "LocalSemanticMemory should hold the PR records"
            for stored in stored_records:
                if stored.get("source") != "pr-history-indexer":
                    continue
                self.assertEqual(stored.get("user_id"), repo_key)


if __name__ == "__main__":
    unittest.main()
