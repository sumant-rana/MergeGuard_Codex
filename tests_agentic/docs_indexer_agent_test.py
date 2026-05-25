"""End-to-end behaviour test for the ``docs-indexer`` agent module."""

from __future__ import annotations

import base64
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _load_agent_module() -> ModuleType:
    path = REPO_ROOT / "agents/docs-indexer/src/docs_indexer/main.py"
    spec = importlib.util.spec_from_file_location("docs_indexer_test_main", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load agent module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


REPO_PREFIX = "https://api.github.com/repos/mongodb/example-service"


class FakeTransport:
    def __init__(self) -> None:
        self.routes = {
            f"{REPO_PREFIX}/contents/README.md": {
                "type": "file",
                "path": "README.md",
                "sha": "rs",
                "size": 30,
                "encoding": "base64",
                "content": _b64("# Hello\n\nWelcome to the example service."),
            },
            f"{REPO_PREFIX}/contents/docs?ref=main": [
                {"type": "file", "path": "docs/setup.md", "sha": "ds", "size": 10},
            ],
            f"{REPO_PREFIX}/contents/docs/setup.md": {
                "type": "file",
                "path": "docs/setup.md",
                "sha": "ds",
                "size": 10,
                "encoding": "base64",
                "content": _b64("# Setup\n\nRun the installer."),
            },
        }

    def __call__(self, url: str, token: str) -> object:
        for prefix, response in self.routes.items():
            if url.startswith(prefix):
                return response
        return None


def _base_payload(mode: str = "local") -> dict:
    return {
        "onboarding_run_id": "onb_docs",
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
            "paths": ["ARCHITECTURE.md"],
        },
        "storage": {
            "mode": mode,
            "repo_key": "mongodb/example-service",
        },
        "credentials": {"github_token": "test-token"},
    }


class DocsIndexerAgentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_agent_module()

    def setUp(self) -> None:
        from packages.history_store import InMemoryPRHistoryStore

        self.store = InMemoryPRHistoryStore()
        self.module.set_store_factory(lambda payload: self.store)
        self.module.set_transport(FakeTransport())

    def tearDown(self) -> None:
        self.module.reset_overrides()

    def test_agent_rejects_unsupported_storage_mode(self) -> None:
        payload = _base_payload(mode="in-process")
        result = self.module.app.invoke(payload)
        self.assertEqual(result["status"], "failed")
        self.assertIn("local", " ".join(result["output"]["errors"]).lower())

    def test_agent_rejects_missing_credentials(self) -> None:
        payload = _base_payload()
        payload["credentials"].pop("github_token")
        result = self.module.app.invoke(payload)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(
            any("github_token" in err.lower() for err in result["output"]["errors"])
        )

    def test_agent_indexes_docs_with_repo_scoped_embeddings(self) -> None:
        payload = _base_payload()
        result = self.module.app.invoke(payload)

        self.assertEqual(result["agent_id"], "docs-indexer")
        self.assertEqual(result["status"], "completed")
        output = result["output"]
        self.assertEqual(output["repository"], "mongodb/example-service")
        self.assertEqual(output["mode"], "local")
        self.assertGreaterEqual(output["scan_summary"]["docs_indexed"], 2)
        self.assertGreaterEqual(output["scan_summary"]["chunks_indexed"], 2)
        self.assertTrue(output["retrieval_ready"])

        # Docs persisted to the store with full content.
        docs = self.store.list_docs("mongodb/example-service")
        paths = sorted(doc["path"] for doc in docs)
        self.assertIn("README.md", paths)
        self.assertIn("docs/setup.md", paths)

        # Every chunk metadata record is scoped by repo_key.
        chunks = self.store.list_doc_chunk_metadata("mongodb/example-service")
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertEqual(chunk["repo_key"], "mongodb/example-service")
            self.assertEqual(chunk["user_id"], "mongodb/example-service")

        # Onboarding run finished cleanly.
        run = self.store.get_onboarding_run("onb_docs")
        assert run is not None
        self.assertEqual(run["status"], "completed")

    def test_agent_user_additional_paths_are_resolved(self) -> None:
        payload = _base_payload()
        payload["scan"]["paths"] = ["ARCHITECTURE.md"]
        result = self.module.app.invoke(payload)
        self.assertEqual(result["status"], "completed")
        requested = result["output"]["scan_summary"]["paths_requested"]
        # Defaults survive and the user addition is appended.
        self.assertIn("README.md", requested)
        self.assertIn("docs/", requested)
        self.assertIn("ARCHITECTURE.md", requested)


if __name__ == "__main__":
    unittest.main()
