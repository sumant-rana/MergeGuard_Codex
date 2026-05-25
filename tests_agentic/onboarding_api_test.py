"""HTTP-shape tests for the onboarding PR-history API endpoints.

We test the pure handler functions (not the HTTP server itself) so the
suite stays free of socket/port flakes. The handlers are a thin shell
that validates input, persists onboarding state, and dispatches to the
``pr-history-indexer`` agent.
"""

from __future__ import annotations

import json
import threading
import time
import unittest

from apps.api.onboarding_handler import (
    handle_pr_history_retry,
    handle_pr_history_start,
    handle_pr_history_status,
)
from packages.history_store import InMemoryPRHistoryStore


# Reuse the agent module + fake transport from the agent test.
from tests_agentic.pr_history_indexer_agent_test import FakeTransport, _load_agent_module


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
    "scan": {
        "max_prs": 10,
        "states": ["merged", "closed"],
        "include_files": True,
    },
    "storage": {
        "mode": "local",
        "repo_key": "mongodb/example-service",
    },
    "credentials": {"github_token": "test-token"},
}


class OnboardingApiHandlerTest(unittest.TestCase):
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
            response = handle_pr_history_status(session_id, store=self.store)
            run = response["body"].get("run") if response["status"] == 200 else None
            if run and run.get("status") == status:
                return response["body"]
            time.sleep(0.02)
        self.fail(f"timed out waiting for status={status}")

    # ── POST start ──────────────────────────────────────────────

    def test_start_rejects_missing_storage_mode(self) -> None:
        body = {**VALID_BODY, "storage": {"repo_key": "mongodb/example-service"}}
        response = handle_pr_history_start(
            session_id="sess_1",
            body=body,
            store=self.store,
            agent_module=self.module,
        )
        self.assertEqual(response["status"], 400)
        self.assertIn("storage.mode", response["body"]["error"])

    def test_start_rejects_unsupported_mode(self) -> None:
        body = {**VALID_BODY, "storage": {"mode": "in-process", "repo_key": "mongodb/example-service"}}
        response = handle_pr_history_start(
            session_id="sess_2",
            body=body,
            store=self.store,
            agent_module=self.module,
        )
        self.assertEqual(response["status"], 400)
        self.assertIn("local", response["body"]["error"])

    def test_start_returns_202_and_runs_in_background(self) -> None:
        response = handle_pr_history_start(
            session_id="sess_3",
            body=dict(VALID_BODY),
            store=self.store,
            agent_module=self.module,
        )
        self.assertEqual(response["status"], 202)
        self.assertEqual(response["body"]["onboarding_run_id"], "sess_3")
        self.assertEqual(response["body"]["state"], "running")

        final = self._wait_for_status("sess_3", "completed")
        self.assertEqual(final["run"]["summary"]["prs_indexed"], 2)

    # ── GET status ──────────────────────────────────────────────

    def test_status_returns_404_when_unknown(self) -> None:
        response = handle_pr_history_status("missing", store=self.store)
        self.assertEqual(response["status"], 404)

    # ── POST retry ──────────────────────────────────────────────

    def test_retry_requires_existing_session(self) -> None:
        response = handle_pr_history_retry(
            session_id="never_started",
            body={},
            store=self.store,
            agent_module=self.module,
        )
        self.assertEqual(response["status"], 404)

    def test_retry_reuses_stored_payload_when_body_empty(self) -> None:
        handle_pr_history_start(
            session_id="sess_retry",
            body=dict(VALID_BODY),
            store=self.store,
            agent_module=self.module,
        )
        self._wait_for_status("sess_retry", "completed")

        # Stored payload is sanitized (no credentials), so a body-less
        # retry must supply credentials. The dashboard one-click case
        # relies on GITHUB_TOKEN being set in the API environment;
        # mimic that for the test.
        import os

        original = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = "retry-token"
        try:
            response = handle_pr_history_retry(
                session_id="sess_retry",
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
        final = self._wait_for_status("sess_retry", "completed")
        # ``retry_count`` should bump each retry so the dashboard can show it.
        self.assertGreaterEqual(final["run"].get("retry_count", 0), 1)
        # Stored payload must not leak credentials.
        self.assertNotIn(
            "credentials",
            final["run"].get("request_payload") or {},
        )


if __name__ == "__main__":
    unittest.main()
