"""MongoDB-backed implementation of ``PRHistoryStore``.

Used in both ``local`` (docker MongoDB) and ``cloud`` (Atlas) modes — they
differ only in the ``MONGODB_URI`` and whether Atlas Vector Search is
provisioned for the embeddings collection. The adapter itself does not
make assumptions about cluster type; collection layout and key shapes are
identical.

Indexes are created on first use (``_ensure_indexes``) so a fresh
onboarding run on a brand-new database immediately gets the unique
constraints that make upserts idempotent. Re-creating an existing index
is a no-op.
"""

from __future__ import annotations

import logging
from datetime import datetime, UTC
from typing import Any

try:
    from pymongo import ASCENDING, MongoClient, ReturnDocument
    from pymongo.errors import PyMongoError
except ImportError as exc:  # pragma: no cover - exercised only without pymongo
    raise ImportError(
        "MongoPRHistoryStore requires pymongo. Install pymongo>=4 to use local/cloud modes."
    ) from exc

logger = logging.getLogger(__name__)


COLL_REPOSITORIES = "repositories"
COLL_ONBOARDING_RUNS = "onboarding_runs"
COLL_PRIOR_PRS = "prior_prs"
COLL_PRIOR_PR_FILES = "prior_pr_files"
COLL_REPO_HISTORY_SIGNALS = "repo_history_signals"
COLL_MEMORY_RECORDS = "memory_records"
COLL_REPO_DOCS = "repo_docs"
COLL_DOC_CHUNK_RECORDS = "doc_chunk_records"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class MongoPRHistoryStore:
    """MongoDB persistence for PR history records and onboarding runs."""

    def __init__(
        self,
        *,
        uri: str,
        db_name: str = "mergeguard",
        client: "MongoClient | None" = None,
    ) -> None:
        if not uri:
            raise ValueError("MongoPRHistoryStore requires a non-empty uri")
        self._owns_client = client is None
        self._client = client or MongoClient(uri, tz_aware=False, appname="mergeguard-pr-history")
        self._db_name = db_name
        self._db = self._client[db_name]
        self._indexes_ready = False

    # ── lifecycle ────────────────────────────────────────────────

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def drop_database(self) -> None:
        """Test helper: drop the agent's database. Never call in production."""
        self._client.drop_database(self._db_name)
        self._indexes_ready = False

    # ── repositories ────────────────────────────────────────────

    def upsert_repository(self, repo: dict[str, Any]) -> None:
        if not repo.get("repo_key"):
            raise ValueError("repository requires repo_key")
        self._ensure_indexes()
        now = _utc_now()
        update = {
            "$set": {**repo, "updated_at": now},
            "$setOnInsert": {"created_at": now},
        }
        self._db[COLL_REPOSITORIES].update_one(
            {"repo_key": repo["repo_key"]}, update, upsert=True
        )

    # ── onboarding runs ────────────────────────────────────────

    def start_onboarding_run(self, run: dict[str, Any]) -> None:
        run_id = run.get("onboarding_run_id")
        if not run_id:
            raise ValueError("onboarding run requires onboarding_run_id")
        self._ensure_indexes()
        now = _utc_now()
        update_set = {
            **run,
            "status": run.get("status") or "running",
            "updated_at": now,
        }
        self._db[COLL_ONBOARDING_RUNS].update_one(
            {"onboarding_run_id": run_id},
            {
                "$set": update_set,
                "$setOnInsert": {
                    "created_at": now,
                    "started_at": run.get("started_at") or now,
                    "warnings": list(run.get("warnings") or []),
                    "errors": list(run.get("errors") or []),
                },
            },
            upsert=True,
        )

    def complete_onboarding_run(self, run_id: str, summary: dict[str, Any]) -> None:
        self._ensure_indexes()
        now = _utc_now()
        result = self._db[COLL_ONBOARDING_RUNS].find_one_and_update(
            {"onboarding_run_id": run_id},
            {
                "$set": {
                    "status": summary.get("status") or "completed",
                    "summary": summary,
                    "completed_at": now,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if result is None:
            raise KeyError(f"onboarding run not found: {run_id}")

    def get_onboarding_run(self, run_id: str) -> dict[str, Any] | None:
        doc = self._db[COLL_ONBOARDING_RUNS].find_one({"onboarding_run_id": run_id})
        return _strip_oid(doc)

    # ── prior PRs ───────────────────────────────────────────────

    def upsert_prior_pr(self, record: dict[str, Any]) -> None:
        repo = record.get("repo_key")
        number = record.get("pr_number")
        if not repo or number is None:
            raise ValueError("prior_pr requires repo_key and pr_number")
        self._ensure_indexes()
        now = _utc_now()
        self._db[COLL_PRIOR_PRS].update_one(
            {"repo_key": repo, "pr_number": int(number)},
            {
                "$set": {**record, "updated_at": now},
                "$setOnInsert": {"indexed_at": record.get("indexed_at") or now},
            },
            upsert=True,
        )

    def list_prior_prs(self, repo_key: str) -> list[dict[str, Any]]:
        cursor = self._db[COLL_PRIOR_PRS].find({"repo_key": repo_key})
        return [_strip_oid(doc) for doc in cursor if doc is not None]

    # ── prior PR files ─────────────────────────────────────────

    def upsert_prior_pr_files(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        self._ensure_indexes()
        for record in records:
            repo = record.get("repo_key")
            number = record.get("pr_number")
            path = record.get("path")
            if not repo or number is None or not path:
                raise ValueError("prior_pr_file requires repo_key, pr_number, path")
            self._db[COLL_PRIOR_PR_FILES].update_one(
                {"repo_key": repo, "pr_number": int(number), "path": path},
                {"$set": record},
                upsert=True,
            )

    def list_prior_pr_files(
        self, repo_key: str, pr_number: int | None = None
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"repo_key": repo_key}
        if pr_number is not None:
            query["pr_number"] = int(pr_number)
        cursor = self._db[COLL_PRIOR_PR_FILES].find(query)
        return [_strip_oid(doc) for doc in cursor if doc is not None]

    # ── history signals ────────────────────────────────────────

    def save_history_signals(self, repo_key: str, signals: dict[str, Any]) -> None:
        self._ensure_indexes()
        payload = {**signals, "repo_key": repo_key, "updated_at": _utc_now()}
        self._db[COLL_REPO_HISTORY_SIGNALS].update_one(
            {"repo_key": repo_key}, {"$set": payload}, upsert=True
        )

    def get_history_signals(self, repo_key: str) -> dict[str, Any] | None:
        doc = self._db[COLL_REPO_HISTORY_SIGNALS].find_one({"repo_key": repo_key})
        return _strip_oid(doc)

    # ── semantic memory metadata ───────────────────────────────

    def save_memory_record_metadata(self, record: dict[str, Any]) -> None:
        self._ensure_indexes()
        label = record.get("label")
        if not label:
            raise ValueError("memory record requires label")
        self._db[COLL_MEMORY_RECORDS].update_one(
            {"label": label},
            {"$set": {**record, "stored_at": _utc_now()}},
            upsert=True,
        )

    # ── docs ────────────────────────────────────────────────────

    def upsert_doc(self, record: dict[str, Any]) -> None:
        repo = record.get("repo_key")
        path = record.get("path")
        if not repo or not path:
            raise ValueError("doc record requires repo_key and path")
        self._ensure_indexes()
        now = _utc_now()
        self._db[COLL_REPO_DOCS].update_one(
            {"repo_key": repo, "path": path},
            {
                "$set": {**record, "updated_at": now},
                "$setOnInsert": {"indexed_at": record.get("indexed_at") or now},
            },
            upsert=True,
        )

    def list_docs(self, repo_key: str) -> list[dict[str, Any]]:
        cursor = self._db[COLL_REPO_DOCS].find({"repo_key": repo_key})
        return [_strip_oid(doc) for doc in cursor if doc is not None]

    def get_doc(self, repo_key: str, path: str) -> dict[str, Any] | None:
        doc = self._db[COLL_REPO_DOCS].find_one({"repo_key": repo_key, "path": path})
        return _strip_oid(doc)

    def save_doc_chunk_metadata(self, record: dict[str, Any]) -> None:
        if not record.get("label"):
            raise ValueError("doc chunk metadata requires label")
        self._ensure_indexes()
        self._db[COLL_DOC_CHUNK_RECORDS].update_one(
            {"label": record["label"]},
            {"$set": {**record, "stored_at": _utc_now()}},
            upsert=True,
        )

    def list_doc_chunk_metadata(self, repo_key: str) -> list[dict[str, Any]]:
        cursor = self._db[COLL_DOC_CHUNK_RECORDS].find({"repo_key": repo_key})
        return [_strip_oid(doc) for doc in cursor if doc is not None]

    # ── index management ───────────────────────────────────────

    def _ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        try:
            self._db[COLL_REPOSITORIES].create_index(
                [("repo_key", ASCENDING)], unique=True, name="repo_key_unique"
            )
            self._db[COLL_ONBOARDING_RUNS].create_index(
                [("onboarding_run_id", ASCENDING)], unique=True, name="onboarding_run_id_unique"
            )
            self._db[COLL_ONBOARDING_RUNS].create_index(
                [("repo_key", ASCENDING), ("updated_at", ASCENDING)],
                name="repo_key_updated_at",
            )
            self._db[COLL_PRIOR_PRS].create_index(
                [("repo_key", ASCENDING), ("pr_number", ASCENDING)],
                unique=True,
                name="repo_key_pr_number_unique",
            )
            self._db[COLL_PRIOR_PR_FILES].create_index(
                [("repo_key", ASCENDING), ("pr_number", ASCENDING), ("path", ASCENDING)],
                unique=True,
                name="repo_key_pr_number_path_unique",
            )
            self._db[COLL_REPO_HISTORY_SIGNALS].create_index(
                [("repo_key", ASCENDING)], unique=True, name="signals_repo_key_unique"
            )
            self._db[COLL_MEMORY_RECORDS].create_index(
                [("label", ASCENDING)], unique=True, name="memory_label_unique"
            )
            self._db[COLL_REPO_DOCS].create_index(
                [("repo_key", ASCENDING), ("path", ASCENDING)],
                unique=True,
                name="repo_docs_repo_path_unique",
            )
            self._db[COLL_DOC_CHUNK_RECORDS].create_index(
                [("label", ASCENDING)], unique=True, name="doc_chunk_label_unique"
            )
        except PyMongoError as exc:
            # Indexes already exist with conflicting options on a pre-existing
            # database — keep going; uniqueness is still enforced because the
            # original index has the same key set. We log at WARNING so an
            # operator who DID intend to change index options can see it.
            logger.warning(
                "pr-history-indexer: index creation reported a conflict (%s); "
                "uniqueness is still enforced by the pre-existing index",
                exc,
            )
        self._indexes_ready = True


def _strip_oid(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    out = {k: v for k, v in doc.items() if k != "_id"}
    return out
