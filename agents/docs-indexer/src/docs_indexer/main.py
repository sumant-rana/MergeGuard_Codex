"""Magenta agent: ``docs-indexer``.

Onboarding-only agent that fetches repository documentation (README,
``docs/``, plus any user-requested paths), persists full doc content in
MongoDB/Atlas, splits each doc into semantic chunks, and writes each
chunk into Magenta semantic memory keyed by ``repo_key`` so downstream
agents can scope retrieval to the current repository.

We chose the "store-docs-AND-embeddings" approach (see
``docs/DOCS_INDEXER.md``): the vector store already keeps chunk text, but
the full doc copy in MongoDB enables re-embedding without re-fetching
from GitHub, hybrid search, and surrounding-context expansion on a hit.

Like ``pr-history-indexer``, only ``storage.mode`` ∈ {``local``,
``cloud``} is accepted; anything else fails fast.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Callable

for _repo_root in [*Path(__file__).resolve().parents, Path("/app")]:
    if (_repo_root / "packages").is_dir():
        _repo_root_str = str(_repo_root)
        if _repo_root_str not in sys.path:
            sys.path.insert(0, _repo_root_str)
        break

from packages.agent_runtime import (  # noqa: E402
    create_app,
    make_agent_result,
    register_entrypoint,
)
from packages.github_pr.docs_collector import (  # noqa: E402
    Transport,
    collect_docs,
    resolve_paths,
)
from packages.history_store import (  # noqa: E402
    InMemoryPRHistoryStore,
    PRHistoryStore,
)
from packages.history_store.docs_chunker import chunk_doc  # noqa: E402

AGENT_ID = "docs-indexer"
SUPPORTED_MODES = ("local", "cloud")

app = create_app(
    AGENT_ID,
    "Onboarding agent that indexes repository documentation into structured storage and semantic memory.",
)


# ── Injection seams ──────────────────────────────────────────────────

_StoreFactory = Callable[[dict[str, Any]], PRHistoryStore]
_store_factory: _StoreFactory | None = None
_transport_override: Transport | None = None


def set_store_factory(factory: _StoreFactory | None) -> None:
    global _store_factory
    _store_factory = factory


def set_transport(transport: Transport | None) -> None:
    global _transport_override
    _transport_override = transport


def reset_overrides() -> None:
    set_store_factory(None)
    set_transport(None)


# ── Helpers ──────────────────────────────────────────────────────────


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _repo_key(payload: dict[str, Any]) -> str:
    storage = payload.get("storage") or {}
    repo = payload.get("repository") or {}
    return (
        storage.get("repo_key")
        or repo.get("full_name")
        or f"{repo.get('owner', '')}/{repo.get('name', '')}".strip("/")
    )


def _github_token(payload: dict[str, Any]) -> str:
    creds = payload.get("credentials") or {}
    return str(creds.get("github_token") or os.environ.get("GITHUB_TOKEN") or "")


def _empty_summary() -> dict[str, Any]:
    return {
        "paths_requested": [],
        "files_seen": 0,
        "files_skipped": 0,
        "docs_indexed": 0,
        "chunks_indexed": 0,
        "embeddings_written": 0,
        "embedding_provider": "",
        "status": "failed",
    }


def _failure(payload: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    repo_key = _repo_key(payload)
    output = {
        "repository": repo_key,
        "mode": (payload.get("storage") or {}).get("mode") or "",
        "scan_summary": _empty_summary(),
        "retrieval_ready": False,
        "warnings": [],
        "errors": errors,
    }
    return make_agent_result(
        AGENT_ID,
        output,
        status="failed",
        confidence=0.0,
        messages=[f"docs-indexer failed: {errors[0] if errors else 'unknown'}"],
        trace=[{"step": "validate", "ok": False, "errors": errors}],
    )


def _validate_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    storage = payload.get("storage") or {}
    mode = (storage.get("mode") or "").lower()
    if mode not in SUPPORTED_MODES:
        errors.append(
            f"storage.mode must be one of {SUPPORTED_MODES}; got {storage.get('mode')!r}"
        )
    repo = payload.get("repository") or {}
    if not (storage.get("repo_key") or repo.get("full_name") or (repo.get("owner") and repo.get("name"))):
        errors.append("repository.full_name or owner+name is required")
    creds = payload.get("credentials") or {}
    if not creds.get("github_token") and not os.environ.get("GITHUB_TOKEN"):
        errors.append("credentials.github_token (or GITHUB_TOKEN env) is required")
    return errors


def _resolve_store(payload: dict[str, Any]) -> tuple[PRHistoryStore | None, list[str]]:
    if _store_factory is not None:
        return _store_factory(payload), []
    uri = os.environ.get("MONGODB_URI", "").strip()
    if not uri:
        return None, [
            "MONGODB_URI is not configured; cannot persist docs in local/cloud mode"
        ]
    try:
        from packages.history_store.mongo_adapter import (  # noqa: WPS433
            MongoPRHistoryStore,
        )
    except ImportError as exc:
        return None, [f"pymongo not installed: {exc}"]
    db_name = os.environ.get("MONGODB_HISTORY_DB", "mergeguard")
    return MongoPRHistoryStore(uri=uri, db_name=db_name), []


# ── Embedding ────────────────────────────────────────────────────────


def _persist_chunk(
    *,
    store: PRHistoryStore,
    repo_key: str,
    chunk: dict[str, Any],
) -> dict[str, Any]:
    """Write a chunk's embedding (best effort) + audit metadata.

    Every embedding write carries ``user_id=repo_key`` so downstream
    retrieval (``semantic-evidence-agent``, future hybrid
    ``review-compression``) can scope queries to the current repo.
    """
    metadata = {
        "repo_key": repo_key,
        "user_id": repo_key,
        "type": "doc_chunk",
        "path": chunk["path"],
        "chunk_index": chunk["chunk_index"],
        "heading": chunk.get("heading", ""),
        "source": AGENT_ID,
        "label": chunk["label"],
    }
    stored = False
    memory = getattr(app, "memory", None)
    if memory is not None:
        try:
            stored = bool(
                memory.save_semantic(
                    text=chunk["text"],
                    label=chunk["label"],
                    user_id=repo_key,
                    source=AGENT_ID,
                    visibility="shared",
                    metadata=metadata,
                    upsert=True,
                    agent_id=AGENT_ID,
                )
            )
        except TypeError:
            try:
                stored = bool(
                    memory.save_semantic(
                        text=chunk["text"],
                        label=chunk["label"],
                        user_id=repo_key,
                        metadata=metadata,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                metadata["embedding_error"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001
            metadata["embedding_error"] = f"{type(exc).__name__}: {exc}"

    metadata["embeddings_written"] = stored
    store.save_doc_chunk_metadata(metadata)
    return metadata


# ── Entrypoint ───────────────────────────────────────────────────────


def run(payload: dict[str, Any]) -> dict[str, Any]:
    errors = _validate_payload(payload)
    if errors:
        return _failure(payload, errors)

    store, store_errors = _resolve_store(payload)
    if store is None:
        return _failure(payload, store_errors)

    storage = payload.get("storage") or {}
    mode = (storage.get("mode") or "").lower()
    repo_key = _repo_key(payload)
    scan = payload.get("scan") or {}
    onboarding_run_id = str(payload.get("onboarding_run_id") or "")
    repo = payload.get("repository") or {}
    default_branch = str(repo.get("default_branch") or "main")
    paths = scan.get("paths") if isinstance(scan.get("paths"), list) else None
    resolved_paths = resolve_paths(paths)

    started_at = _utc_now()
    try:
        store.upsert_repository(
            {
                "repo_key": repo_key,
                "owner": repo.get("owner", ""),
                "name": repo.get("name", ""),
                "default_branch": default_branch,
            }
        )
        if onboarding_run_id:
            store.start_onboarding_run(
                {
                    "onboarding_run_id": onboarding_run_id,
                    "repo_key": repo_key,
                    "status": "running",
                    "started_at": started_at,
                    "scan": {"paths": resolved_paths},
                    "storage_mode": mode,
                    "agent": AGENT_ID,
                }
            )

        source = payload.get("source") or {}
        api_base = str(source.get("api_base_url") or "https://api.github.com")
        result = collect_docs(
            repo_full_name=repo_key,
            token=_github_token(payload),
            transport=_transport_override,
            paths=resolved_paths,
            default_branch=default_branch,
            api_base_url=api_base,
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        if onboarding_run_id:
            try:
                store.complete_onboarding_run(
                    onboarding_run_id,
                    {"status": "failed", "errors": [msg]},
                )
            except Exception:  # noqa: BLE001
                pass
        return _failure(payload, [msg])

    docs: list[dict[str, Any]] = result["docs"]
    warnings: list[str] = list(result.get("warnings") or [])

    # Persist full docs first so chunk embedding can be re-driven later
    # without re-fetching from GitHub.
    for doc in docs:
        store.upsert_doc({**doc, "indexed_at": _utc_now()})

    # Chunk + embed.
    chunks_total = 0
    embeddings_written = 0
    for doc in docs:
        chunks = chunk_doc(
            repo_key=repo_key,
            path=doc["path"],
            language=doc.get("language", "other"),
            content=doc.get("content", ""),
        )
        for chunk in chunks:
            meta = _persist_chunk(store=store, repo_key=repo_key, chunk=chunk)
            chunks_total += 1
            if meta.get("embeddings_written"):
                embeddings_written += 1

    if chunks_total and embeddings_written == 0:
        warnings.append(
            "no embeddings were written; downstream retrieval will use metadata only"
        )

    summary = {
        **result["scan_summary"],
        "chunks_indexed": chunks_total,
        "embeddings_written": embeddings_written,
        "embedding_provider": _embedding_provider(),
        "status": "completed",
    }
    if onboarding_run_id:
        store.complete_onboarding_run(onboarding_run_id, summary)

    output = {
        "repository": repo_key,
        "mode": mode,
        "scan_summary": summary,
        "retrieval_ready": bool(docs),
        "warnings": warnings,
        "errors": [],
    }
    return make_agent_result(
        AGENT_ID,
        output,
        confidence=_confidence(docs, embeddings_written),
        messages=[
            (
                f"indexed {summary['docs_indexed']} docs and "
                f"{summary['chunks_indexed']} chunks; "
                f"embeddings_written={embeddings_written}"
            )
        ],
        trace=[
            {"step": "validate", "ok": True},
            {"step": "collect", "docs": summary["docs_indexed"]},
            {"step": "embed", "chunks": summary["chunks_indexed"], "written": embeddings_written},
        ],
    )


def _embedding_provider() -> str:
    memory = getattr(app, "memory", None)
    if memory is None:
        return "none"
    return type(memory).__name__


def _confidence(docs: list[dict[str, Any]], embeddings_written: int) -> float:
    if not docs:
        return 0.4
    base = 0.62 + min(0.22, len(docs) * 0.02)
    if embeddings_written:
        base += 0.1
    return round(min(base, 0.95), 2)


register_entrypoint(app, run)


def main() -> None:
    if not hasattr(app, "run"):
        raise RuntimeError(
            "Magenta SDK is required to run this agent service. "
            "Use the onboarding orchestrator for demo mode."
        )
    app.run()


if __name__ == "__main__":
    main()


__all__ = [
    "app",
    "run",
    "set_store_factory",
    "set_transport",
    "reset_overrides",
    "AGENT_ID",
    "SUPPORTED_MODES",
    "InMemoryPRHistoryStore",
]
