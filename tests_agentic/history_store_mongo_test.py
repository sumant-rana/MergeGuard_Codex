"""Integration tests for ``MongoPRHistoryStore`` against a real MongoDB.

The test spins up a single MongoDB container via ``testcontainers`` for
the whole test class and tears it down at the end. It is skipped
gracefully when:

- ``pymongo`` is not installed,
- ``testcontainers`` is not installed,
- Docker is not available on the host (CI runners without Docker).

This keeps ``make test-agentic`` green on a vanilla laptop while still
exercising the real Mongo wire format for anyone with Docker.
"""

from __future__ import annotations

import os
import shutil
import unittest


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    # Honour an explicit opt-out for CI runners that have docker but
    # don't allow privileged containers.
    return os.environ.get("MERGEGUARD_SKIP_DOCKER_TESTS", "").lower() not in {"1", "true"}


try:
    import pymongo  # noqa: F401

    _have_pymongo = True
except Exception:  # noqa: BLE001
    _have_pymongo = False

try:
    from testcontainers.mongodb import MongoDbContainer  # type: ignore

    _have_testcontainers = True
except Exception:  # noqa: BLE001
    MongoDbContainer = None  # type: ignore[assignment]
    _have_testcontainers = False


@unittest.skipUnless(
    _have_pymongo and _have_testcontainers and _docker_available(),
    "Mongo integration tests require pymongo + testcontainers + Docker",
)
class MongoPRHistoryStoreIntegrationTest(unittest.TestCase):
    container = None
    uri = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.container = MongoDbContainer("mongo:7.0")
        cls.container.start()
        cls.uri = cls.container.get_connection_url()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.container is not None:
            cls.container.stop()

    def setUp(self) -> None:
        from packages.history_store.mongo_adapter import MongoPRHistoryStore

        self.store = MongoPRHistoryStore(
            uri=self.uri, db_name=f"mergeguard_test_{id(self)}"
        )
        self.repo_key = "mongodb/example-service"

    def tearDown(self) -> None:
        self.store.drop_database()
        self.store.close()

    def test_upsert_prior_pr_is_idempotent(self) -> None:
        record = {
            "repo_key": self.repo_key,
            "pr_number": 17,
            "title": "Initial",
            "state": "merged",
        }
        self.store.upsert_prior_pr(record)
        self.store.upsert_prior_pr({**record, "title": "Updated"})
        prs = self.store.list_prior_prs(self.repo_key)
        self.assertEqual(len(prs), 1)
        self.assertEqual(prs[0]["title"], "Updated")

    def test_upsert_prior_pr_files_indexed_per_path(self) -> None:
        files = [
            {
                "repo_key": self.repo_key,
                "pr_number": 17,
                "path": "src/a.py",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
            },
            {
                "repo_key": self.repo_key,
                "pr_number": 17,
                "path": "src/b.py",
                "status": "added",
                "additions": 5,
                "deletions": 0,
            },
        ]
        self.store.upsert_prior_pr_files(files)
        self.store.upsert_prior_pr_files(
            [{**files[0], "additions": 99}],
        )
        stored = sorted(
            self.store.list_prior_pr_files(self.repo_key, 17),
            key=lambda record: record["path"],
        )
        self.assertEqual([f["path"] for f in stored], ["src/a.py", "src/b.py"])
        self.assertEqual(stored[0]["additions"], 99)

    def test_history_signals_round_trip(self) -> None:
        self.store.save_history_signals(
            self.repo_key,
            {
                "hotspot_paths": [{"path": "src/x.py", "score": 80, "reasons": ["hot"]}],
            },
        )
        signals = self.store.get_history_signals(self.repo_key)
        assert signals is not None
        self.assertEqual(signals["repo_key"], self.repo_key)
        self.assertEqual(signals["hotspot_paths"][0]["path"], "src/x.py")

    def test_onboarding_run_lifecycle(self) -> None:
        self.store.start_onboarding_run(
            {
                "onboarding_run_id": "onb_x",
                "repo_key": self.repo_key,
                "status": "running",
            }
        )
        self.store.complete_onboarding_run("onb_x", {"prs_indexed": 4, "status": "completed"})
        run = self.store.get_onboarding_run("onb_x")
        assert run is not None
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["summary"]["prs_indexed"], 4)

    def test_upsert_doc_is_idempotent(self) -> None:
        record = {
            "repo_key": self.repo_key,
            "path": "README.md",
            "content": "# Hello",
            "content_size": 7,
            "sha": "abc",
        }
        self.store.upsert_doc(record)
        self.store.upsert_doc({**record, "content": "# Hello v2", "content_size": 9})
        docs = self.store.list_docs(self.repo_key)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["content"], "# Hello v2")

    def test_doc_chunk_metadata_round_trip(self) -> None:
        self.store.save_doc_chunk_metadata(
            {
                "repo_key": self.repo_key,
                "label": "mergeguard:doc:mongodb/example-service::README.md#chunk0",
                "path": "README.md",
                "chunk_index": 0,
                "embeddings_written": True,
            }
        )
        records = self.store.list_doc_chunk_metadata(self.repo_key)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["chunk_index"], 0)


if __name__ == "__main__":
    unittest.main()
