"""Storage adapter Protocol + in-memory reference implementation.

The Protocol is the contract every concrete backend must satisfy. The
in-memory adapter is the test double the agent uses when no live database
is wired up; it is **never** used in the deployed ``local``/``cloud`` modes.
"""

from __future__ import annotations

from datetime import datetime, UTC
from typing import Any, Protocol, runtime_checkable


def pr_key(repo_key: str, pr_number: int) -> str:
    """Stable composite key used by both Mongo and the in-memory adapter."""
    return f"{repo_key}#{pr_number}"


def pr_file_key(repo_key: str, pr_number: int, path: str) -> str:
    """Composite key for ``prior_pr_files`` upserts."""
    return f"{repo_key}#{pr_number}::{path}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@runtime_checkable
class PRHistoryStore(Protocol):
    """Persistence contract for the onboarding agents.

    This Protocol is shared by ``pr-history-indexer`` (PR records,
    history signals, semantic memory metadata) and ``docs-indexer``
    (documents, doc chunk metadata). Keeping a single Protocol lets the
    onboarding API and HTTP handlers depend on one type while the two
    agents stay free to evolve their own writes independently.
    """

    # ── onboarding lifecycle (shared) ───────────────────────────

    def upsert_repository(self, repo: dict[str, Any]) -> None: ...

    def start_onboarding_run(self, run: dict[str, Any]) -> None: ...

    def complete_onboarding_run(self, run_id: str, summary: dict[str, Any]) -> None: ...

    def get_onboarding_run(self, run_id: str) -> dict[str, Any] | None: ...

    # ── PR history ──────────────────────────────────────────────

    def upsert_prior_pr(self, record: dict[str, Any]) -> None: ...

    def list_prior_prs(self, repo_key: str) -> list[dict[str, Any]]: ...

    def upsert_prior_pr_files(self, records: list[dict[str, Any]]) -> None: ...

    def list_prior_pr_files(
        self, repo_key: str, pr_number: int | None = None
    ) -> list[dict[str, Any]]: ...

    def save_history_signals(self, repo_key: str, signals: dict[str, Any]) -> None: ...

    def get_history_signals(self, repo_key: str) -> dict[str, Any] | None: ...

    def save_memory_record_metadata(self, record: dict[str, Any]) -> None: ...

    # ── docs ────────────────────────────────────────────────────

    def upsert_doc(self, record: dict[str, Any]) -> None: ...

    def list_docs(self, repo_key: str) -> list[dict[str, Any]]: ...

    def get_doc(self, repo_key: str, path: str) -> dict[str, Any] | None: ...

    def save_doc_chunk_metadata(self, record: dict[str, Any]) -> None: ...

    def list_doc_chunk_metadata(self, repo_key: str) -> list[dict[str, Any]]: ...


class InMemoryPRHistoryStore:
    """Reference implementation for unit tests and laptop demos.

    Not used in deployed ``local`` or ``cloud`` modes; the agent module
    rejects those modes unless a real backend (Mongo) is wired up.
    """

    def __init__(self) -> None:
        self._repositories: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._prior_prs: dict[str, dict[str, Any]] = {}
        self._prior_files: dict[str, dict[str, Any]] = {}
        self._signals: dict[str, dict[str, Any]] = {}
        self._memory_metadata: list[dict[str, Any]] = []
        self._docs: dict[str, dict[str, Any]] = {}
        self._doc_chunks: list[dict[str, Any]] = []

    def upsert_repository(self, repo: dict[str, Any]) -> None:
        if not repo.get("repo_key"):
            raise ValueError("repository requires repo_key")
        existing = self._repositories.get(repo["repo_key"])
        now = _utc_now()
        merged = {**(existing or {}), **repo, "updated_at": now}
        merged.setdefault("created_at", now)
        self._repositories[repo["repo_key"]] = merged

    def start_onboarding_run(self, run: dict[str, Any]) -> None:
        run_id = run.get("onboarding_run_id")
        if not run_id:
            raise ValueError("onboarding run requires onboarding_run_id")
        now = _utc_now()
        existing = self._runs.get(run_id, {})
        merged = {
            **existing,
            **run,
            "status": run.get("status") or "running",
            "started_at": existing.get("started_at") or now,
            "updated_at": now,
            "warnings": list(run.get("warnings") or existing.get("warnings") or []),
            "errors": list(run.get("errors") or existing.get("errors") or []),
        }
        merged.setdefault("created_at", existing.get("created_at") or now)
        self._runs[run_id] = merged

    def complete_onboarding_run(self, run_id: str, summary: dict[str, Any]) -> None:
        existing = self._runs.get(run_id)
        if not existing:
            raise KeyError(f"onboarding run not found: {run_id}")
        now = _utc_now()
        existing["status"] = summary.get("status") or "completed"
        existing["summary"] = summary
        existing["completed_at"] = now
        existing["updated_at"] = now

    def get_onboarding_run(self, run_id: str) -> dict[str, Any] | None:
        run = self._runs.get(run_id)
        return None if run is None else dict(run)

    def upsert_prior_pr(self, record: dict[str, Any]) -> None:
        repo = record.get("repo_key")
        number = record.get("pr_number")
        if not repo or number is None:
            raise ValueError("prior_pr requires repo_key and pr_number")
        key = pr_key(repo, int(number))
        now = _utc_now()
        existing = self._prior_prs.get(key, {})
        merged = {**existing, **record}
        merged.setdefault("indexed_at", now)
        merged["updated_at"] = now
        self._prior_prs[key] = merged

    def list_prior_prs(self, repo_key: str) -> list[dict[str, Any]]:
        return [dict(pr) for pr in self._prior_prs.values() if pr.get("repo_key") == repo_key]

    def upsert_prior_pr_files(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            repo = record.get("repo_key")
            number = record.get("pr_number")
            path = record.get("path")
            if not repo or number is None or not path:
                raise ValueError("prior_pr_file requires repo_key, pr_number, path")
            key = pr_file_key(repo, int(number), path)
            existing = self._prior_files.get(key, {})
            self._prior_files[key] = {**existing, **record}

    def list_prior_pr_files(
        self, repo_key: str, pr_number: int | None = None
    ) -> list[dict[str, Any]]:
        files = [f for f in self._prior_files.values() if f.get("repo_key") == repo_key]
        if pr_number is not None:
            files = [f for f in files if int(f.get("pr_number", -1)) == int(pr_number)]
        return [dict(f) for f in files]

    def save_history_signals(self, repo_key: str, signals: dict[str, Any]) -> None:
        merged = {**signals, "repo_key": repo_key, "updated_at": _utc_now()}
        self._signals[repo_key] = merged

    def get_history_signals(self, repo_key: str) -> dict[str, Any] | None:
        signals = self._signals.get(repo_key)
        return None if signals is None else dict(signals)

    def save_memory_record_metadata(self, record: dict[str, Any]) -> None:
        self._memory_metadata.append({**record, "stored_at": _utc_now()})

    def list_memory_record_metadata(self) -> list[dict[str, Any]]:
        return [dict(record) for record in self._memory_metadata]

    # ── docs ────────────────────────────────────────────────────

    @staticmethod
    def _doc_key(repo_key: str, path: str) -> str:
        return f"{repo_key}::{path}"

    def upsert_doc(self, record: dict[str, Any]) -> None:
        repo = record.get("repo_key")
        path = record.get("path")
        if not repo or not path:
            raise ValueError("doc record requires repo_key and path")
        key = self._doc_key(repo, path)
        now = _utc_now()
        existing = self._docs.get(key, {})
        self._docs[key] = {
            **existing,
            **record,
            "indexed_at": existing.get("indexed_at") or now,
            "updated_at": now,
        }

    def list_docs(self, repo_key: str) -> list[dict[str, Any]]:
        return [
            dict(doc) for doc in self._docs.values() if doc.get("repo_key") == repo_key
        ]

    def get_doc(self, repo_key: str, path: str) -> dict[str, Any] | None:
        doc = self._docs.get(self._doc_key(repo_key, path))
        return None if doc is None else dict(doc)

    def save_doc_chunk_metadata(self, record: dict[str, Any]) -> None:
        if not record.get("label"):
            raise ValueError("doc chunk metadata requires label")
        self._doc_chunks.append({**record, "stored_at": _utc_now()})

    def list_doc_chunk_metadata(self, repo_key: str) -> list[dict[str, Any]]:
        return [
            dict(record)
            for record in self._doc_chunks
            if record.get("repo_key") == repo_key
        ]
