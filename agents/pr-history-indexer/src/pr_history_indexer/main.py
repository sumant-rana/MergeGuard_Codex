"""Magenta agent: ``pr-history-indexer``.

Scans historical pull requests for an onboarding repository, persists
structured records and aggregated signals, and writes compact semantic
records into Magenta memory. Downstream agents (notably
``review-compression``) consume the resulting history context during
normal PR analysis.

The agent runs in two modes:

- ``local``:  docker-based MongoDB and (when available) the in-container
  Magenta memory shim.
- ``cloud``:  Atlas + Atlas Vector Search + Magenta memory in the
  tenant runtime.

Any other ``storage.mode`` is rejected on purpose: onboarding must not
silently fall back to in-process JSON for a productized scan.
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
from packages.github_pr.pr_history_collector import (  # noqa: E402
    Transport,
    collect_pr_history,
)
from packages.history_store import (  # noqa: E402
    InMemoryPRHistoryStore,
    PRHistoryStore,
)
from packages.history_store.signals import compute_history_signals  # noqa: E402

AGENT_ID = "pr-history-indexer"
SUPPORTED_MODES = ("local", "cloud")

app = create_app(
    AGENT_ID,
    "Onboarding agent that indexes prior PRs into structured storage and semantic memory.",
)


# ── Injection seams ──────────────────────────────────────────────────
#
# Unit tests replace the store factory and HTTP transport here so the
# agent stays decoupled from MongoDB and from real GitHub network calls.
# Production code paths (Magenta containers, MergeGuard API) supply real
# factories during onboarding wiring.

_StoreFactory = Callable[[dict[str, Any]], PRHistoryStore]
_store_factory: _StoreFactory | None = None
_transport_override: Transport | None = None


def set_store_factory(factory: _StoreFactory | None) -> None:
    """Override the store factory (tests + onboarding wiring)."""
    global _store_factory
    _store_factory = factory


def set_transport(transport: Transport | None) -> None:
    """Override the HTTP transport used by the collector (tests only)."""
    global _transport_override
    _transport_override = transport


def reset_overrides() -> None:
    """Clear all test overrides — call in ``tearDown``."""
    set_store_factory(None)
    set_transport(None)


# ── Output helpers ───────────────────────────────────────────────────


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _empty_signals(repo_key: str) -> dict[str, Any]:
    return {
        "repo_key": repo_key,
        "frequently_changed_files": [],
        "files_changed_together": [],
        "hotspot_paths": [],
        "owner_activity": [],
        "review_latency_by_area": [],
        "jira_key_frequency": [],
        "updated_at": _utc_now(),
    }


def _failure(payload: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    repo = payload.get("repository") or {}
    storage = payload.get("storage") or {}
    repo_key = (
        storage.get("repo_key")
        or repo.get("full_name")
        or f"{repo.get('owner', '')}/{repo.get('name', '')}".strip("/")
    )
    output = {
        "repository": repo_key,
        "mode": storage.get("mode") or "",
        "scan_summary": {
            "prs_seen": 0,
            "prs_indexed": 0,
            "prs_skipped": 0,
            "files_indexed": 0,
            "memory_records_written": 0,
            "embedding_provider": "",
        },
        "historical_signals": _empty_signals(repo_key),
        "retrieval_ready": False,
        "warnings": [],
        "errors": errors,
    }
    return make_agent_result(
        AGENT_ID,
        output,
        status="failed",
        confidence=0.0,
        messages=[f"pr-history-indexer failed: {errors[0] if errors else 'unknown'}"],
        trace=[{"step": "validate", "ok": False, "errors": errors}],
    )


# ── Validation ───────────────────────────────────────────────────────


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


# ── Store resolution ─────────────────────────────────────────────────


def _resolve_store(payload: dict[str, Any]) -> tuple[PRHistoryStore | None, list[str]]:
    """Return a concrete ``PRHistoryStore`` for the requested mode.

    Test wiring replaces ``_store_factory`` so unit tests can hand in an
    in-memory store. The deployed path goes through the Mongo adapter; if
    ``MONGODB_URI`` is missing we surface a clear error rather than
    silently falling back to in-process JSON.
    """
    if _store_factory is not None:
        return _store_factory(payload), []
    uri = os.environ.get("MONGODB_URI", "").strip()
    if not uri:
        return None, [
            "MONGODB_URI is not configured; cannot persist PR history in local/cloud mode"
        ]
    try:
        from packages.history_store.mongo_adapter import (  # noqa: WPS433 - lazy import
            MongoPRHistoryStore,
        )
    except ImportError as exc:
        return None, [f"pymongo not installed: {exc}"]
    db_name = os.environ.get("MONGODB_HISTORY_DB", "mergeguard")
    return MongoPRHistoryStore(uri=uri, db_name=db_name), []


# ── Repository identity ──────────────────────────────────────────────


def _repo_full_name(payload: dict[str, Any]) -> str:
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


# ── Semantic record construction ─────────────────────────────────────


def _semantic_text_for_pr(pr: dict[str, Any], file_records: list[dict[str, Any]]) -> str:
    paths = ", ".join(
        record.get("path", "") for record in file_records if record.get("path")
    )
    pieces = [
        f"PR #{pr.get('pr_number')} {pr.get('title', '')}.",
        f"State: {pr.get('state', '')}.",
    ]
    if pr.get("labels"):
        pieces.append(f"Labels: {', '.join(pr['labels'])}.")
    if pr.get("linked_jira_keys"):
        pieces.append(f"Linked Jira: {', '.join(pr['linked_jira_keys'])}.")
    if paths:
        pieces.append(f"Changed files: {paths}.")
    body = pr.get("body") or ""
    if body:
        pieces.append(body[:800])
    return " ".join(pieces).strip()


def _persist_semantic_record(
    *,
    store: PRHistoryStore,
    repo_key: str,
    pr: dict[str, Any],
    file_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Write one ``prior_pr_summary`` record to Magenta memory + metadata.

    Returns a metadata dict (also persisted via ``save_memory_record_metadata``)
    that downstream observability can read to confirm embeddings landed.
    """
    label = f"mergeguard:prior_pr:{repo_key}#{pr.get('pr_number')}"
    text = _semantic_text_for_pr(pr, file_records)
    metadata = {
        "repo_key": repo_key,
        # Mirror the vector-store ``user_id`` into the structured audit
        # record so anything that inspects ``memory_records`` directly
        # (dashboards, debugging) sees the same scope key as
        # ``search_semantic(user_id=repo_key, ...)``.
        "user_id": repo_key,
        "type": "prior_pr_summary",
        "pr_number": pr.get("pr_number"),
        "paths": [r.get("path") for r in file_records if r.get("path")],
        "labels": pr.get("labels") or [],
        "linked_jira_keys": pr.get("linked_jira_keys") or [],
        "source": AGENT_ID,
        "label": label,
    }
    stored = False
    memory = getattr(app, "memory", None)
    if memory is not None:
        try:
            stored = bool(
                memory.save_semantic(
                    text=text,
                    label=label,
                    user_id=repo_key,
                    source=AGENT_ID,
                    visibility="shared",
                    metadata=metadata,
                    upsert=True,
                    agent_id=AGENT_ID,
                )
            )
        except TypeError:
            # Older Magenta memory shims accept fewer kwargs; degrade
            # gracefully so onboarding never blocks on a signature drift.
            try:
                stored = bool(
                    memory.save_semantic(
                        text=text,
                        label=label,
                        user_id=repo_key,
                        metadata=metadata,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - want full reason in metadata
                metadata["embedding_error"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001
            metadata["embedding_error"] = f"{type(exc).__name__}: {exc}"

    metadata["embeddings_written"] = stored
    store.save_memory_record_metadata(metadata)
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
    repo_full_name = _repo_full_name(payload)
    scan = payload.get("scan") or {}
    onboarding_run_id = str(payload.get("onboarding_run_id") or "")

    started_at = _utc_now()
    try:
        store.upsert_repository(
            {
                "repo_key": repo_full_name,
                "owner": (payload.get("repository") or {}).get("owner", ""),
                "name": (payload.get("repository") or {}).get("name", ""),
                "default_branch": (payload.get("repository") or {}).get("default_branch", "main"),
            }
        )
        if onboarding_run_id:
            store.start_onboarding_run(
                {
                    "onboarding_run_id": onboarding_run_id,
                    "repo_key": repo_full_name,
                    "status": "running",
                    "started_at": started_at,
                    "scan": scan,
                    "storage_mode": mode,
                }
            )

        source = payload.get("source") or {}
        api_base = str(source.get("api_base_url") or "https://api.github.com")
        result = collect_pr_history(
            repo_full_name=repo_full_name,
            token=_github_token(payload),
            transport=_transport_override,
            max_prs=int(scan.get("max_prs") or 500),
            include_files=bool(scan.get("include_files", True)),
            states=list(scan.get("states") or ["merged", "closed"]),
            since=scan.get("since"),
            api_base_url=api_base,
        )
    except Exception as exc:  # noqa: BLE001 - surface as agent failure
        msg = f"{type(exc).__name__}: {exc}"
        if onboarding_run_id:
            try:
                store.complete_onboarding_run(
                    onboarding_run_id,
                    {"status": "failed", "error": msg},
                )
            except Exception:  # noqa: BLE001
                pass
        return _failure(payload, [msg])

    prs: list[dict[str, Any]] = result["prs"]
    files: list[dict[str, Any]] = result["files"]
    warnings: list[str] = list(result.get("warnings") or [])

    # Persist normalized records before computing signals so signal
    # aggregation can be retried independently if it ever raises.
    indexed_at = _utc_now()
    for pr in prs:
        store.upsert_prior_pr({**pr, "indexed_at": indexed_at})
    if files:
        store.upsert_prior_pr_files(files)

    signals = compute_history_signals(repo_full_name, prs, files)
    store.save_history_signals(repo_full_name, signals)

    # Semantic records — one ``prior_pr_summary`` per PR.
    files_by_pr: dict[int, list[dict[str, Any]]] = {}
    for record in files:
        files_by_pr.setdefault(int(record.get("pr_number") or 0), []).append(record)
    memory_records: list[dict[str, Any]] = []
    embeddings_written = 0
    for pr in prs:
        meta = _persist_semantic_record(
            store=store,
            repo_key=repo_full_name,
            pr=pr,
            file_records=files_by_pr.get(int(pr.get("pr_number") or 0), []),
        )
        memory_records.append(meta)
        if meta.get("embeddings_written"):
            embeddings_written += 1

    if memory_records and embeddings_written == 0:
        warnings.append(
            "no embeddings were written; downstream retrieval will use metadata only"
        )

    summary = {
        "prs_seen": result["scan_summary"]["prs_seen"],
        "prs_indexed": result["scan_summary"]["prs_indexed"],
        "prs_skipped": result["scan_summary"]["prs_skipped"],
        "files_indexed": result["scan_summary"]["files_indexed"],
        "memory_records_written": len(memory_records),
        "embeddings_written": embeddings_written,
        "embedding_provider": _embedding_provider(),
        "status": "completed",
    }
    if onboarding_run_id:
        store.complete_onboarding_run(onboarding_run_id, summary)

    output = {
        "repository": repo_full_name,
        "mode": mode,
        "scan_summary": summary,
        "historical_signals": signals,
        "retrieval_ready": embeddings_written > 0 or bool(prs),
        "warnings": warnings,
        "errors": [],
    }
    return make_agent_result(
        AGENT_ID,
        output,
        confidence=_confidence(prs, embeddings_written),
        messages=[
            (
                f"indexed {summary['prs_indexed']} prior PRs and "
                f"{summary['files_indexed']} files; "
                f"embeddings_written={embeddings_written}"
            )
        ],
        trace=[
            {"step": "validate", "ok": True},
            {
                "step": "collect",
                "prs_seen": summary["prs_seen"],
                "prs_indexed": summary["prs_indexed"],
            },
            {
                "step": "persist",
                "files_indexed": summary["files_indexed"],
                "memory_records": summary["memory_records_written"],
            },
            {"step": "aggregate", "hotspots": len(signals["hotspot_paths"])},
        ],
    )


def _embedding_provider() -> str:
    memory = getattr(app, "memory", None)
    if memory is None:
        return "none"
    return type(memory).__name__


def _confidence(prs: list[dict[str, Any]], embeddings_written: int) -> float:
    if not prs:
        return 0.4
    base = 0.6 + min(0.25, len(prs) * 0.02)
    if embeddings_written:
        base += 0.1
    return round(min(base, 0.95), 2)


register_entrypoint(app, run)


def main() -> None:
    """Run the Magenta agent service when executed by agentic dev/deploy."""
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
