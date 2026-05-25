"""Pin the docs-storage extensions to the shared onboarding store."""

from __future__ import annotations

import unittest

from packages.history_store import InMemoryPRHistoryStore


class DocsStoreAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryPRHistoryStore()
        self.repo_key = "mongodb/example-service"

    def test_docs_methods_exist(self) -> None:
        for method in (
            "upsert_doc",
            "list_docs",
            "get_doc",
            "save_doc_chunk_metadata",
            "list_doc_chunk_metadata",
        ):
            self.assertTrue(hasattr(self.store, method), f"missing {method}")

    def test_upsert_doc_is_idempotent_on_repo_key_and_path(self) -> None:
        record = {
            "repo_key": self.repo_key,
            "path": "README.md",
            "sha": "abc",
            "content": "# Hello",
            "content_size": 7,
        }
        self.store.upsert_doc(record)
        self.store.upsert_doc({**record, "content": "# Hello v2", "content_size": 9})

        docs = self.store.list_docs(self.repo_key)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["content"], "# Hello v2")

    def test_get_doc_returns_none_when_absent(self) -> None:
        self.assertIsNone(self.store.get_doc(self.repo_key, "missing.md"))

    def test_docs_are_scoped_per_repo_key(self) -> None:
        self.store.upsert_doc(
            {"repo_key": "org/a", "path": "README.md", "content": "a"}
        )
        self.store.upsert_doc(
            {"repo_key": "org/b", "path": "README.md", "content": "b"}
        )
        a_docs = self.store.list_docs("org/a")
        b_docs = self.store.list_docs("org/b")
        self.assertEqual([d["content"] for d in a_docs], ["a"])
        self.assertEqual([d["content"] for d in b_docs], ["b"])

    def test_doc_chunk_metadata_round_trip(self) -> None:
        self.store.save_doc_chunk_metadata(
            {
                "repo_key": self.repo_key,
                "label": "mergeguard:doc:mongodb/example-service::README.md#chunk0",
                "path": "README.md",
                "chunk_index": 0,
                "heading": "Hello",
                "embeddings_written": True,
            }
        )
        records = self.store.list_doc_chunk_metadata(self.repo_key)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["chunk_index"], 0)
        self.assertEqual(records[0]["embeddings_written"], True)


if __name__ == "__main__":
    unittest.main()
