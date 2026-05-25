"""HTTP-shape tests for the onboarding docs-indexer endpoints."""

from __future__ import annotations

import time
import unittest

from apps.api.onboarding_handler import (
    handle_docs_retry,
    handle_docs_start,
    handle_docs_status,
)
from packages.history_store import InMemoryPRHistoryStore

from tests_agentic.docs_indexer_agent_test import FakeTransport, _load_agent_module


VALID_BODY = {
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
    "scan": {"paths": ["ARCHITECTURE.md"]},
    "storage": {"mode": "local", "repo_key": "mongodb/example-service"},
    "credentials": {"github_token": "test-token"},
}


class DocsOnboardingApiHandlerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_agent_module()

    def setUp(self) -> None:
        self.store = InMemoryPRHistoryStore()
        self.module.set_store_factory(lambda payload: self.store)
        self.module.set_transport(FakeTransport())

    def tearDown(self) -> None:
        self.module.reset_overrides()

    def _wait_for_status(self, session_id: str, status: str, timeout: float = 5.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            response = handle_docs_status(session_id, store=self.store)
            run = response["body"].get("run") if response["status"] == 200 else None
            if run and run.get("status") == status:
                return response["body"]
            time.sleep(0.02)
        self.fail(f"timed out waiting for status={status}")

    def test_start_rejects_missing_storage_mode(self) -> None:
        body = {**VALID_BODY, "storage": {"repo_key": "mongodb/example-service"}}
        response = handle_docs_start(
            session_id="sd_1",
            body=body,
            store=self.store,
            agent_module=self.module,
        )
        self.assertEqual(response["status"], 400)

    def test_start_returns_202_and_runs_in_background(self) -> None:
        response = handle_docs_start(
            session_id="sd_2",
            body=dict(VALID_BODY),
            store=self.store,
            agent_module=self.module,
        )
        self.assertEqual(response["status"], 202)
        self.assertEqual(response["body"]["onboarding_run_id"], "sd_2")
        final = self._wait_for_status("sd_2", "completed")
        self.assertGreaterEqual(final["run"]["summary"]["docs_indexed"], 2)
        self.assertGreaterEqual(final["run"]["summary"]["chunks_indexed"], 2)

    def test_status_returns_404_when_unknown(self) -> None:
        response = handle_docs_status("missing-doc-session", store=self.store)
        self.assertEqual(response["status"], 404)

    def test_retry_reuses_stored_payload_without_credentials(self) -> None:
        handle_docs_start(
            session_id="sd_retry",
            body=dict(VALID_BODY),
            store=self.store,
            agent_module=self.module,
        )
        self._wait_for_status("sd_retry", "completed")

        import os

        original = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = "retry-token"
        try:
            response = handle_docs_retry(
                session_id="sd_retry",
                body={},
                store=self.store,
                agent_module=self.module,
            )
        finally:
            if original is None:
                os.environ.pop("GITHUB_TOKEN", None)
            else:
                os.environ["GITHUB_TOKEN"] = original

        self.assertEqual(response["status"], 202)
        final = self._wait_for_status("sd_retry", "completed")
        self.assertGreaterEqual(final["run"].get("retry_count", 0), 1)
        self.assertNotIn("credentials", final["run"].get("request_payload") or {})


if __name__ == "__main__":
    unittest.main()
