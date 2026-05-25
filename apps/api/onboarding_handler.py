"""HTTP handlers for the onboarding agent endpoints.

These are pure functions: they take a request body, a ``PRHistoryStore``,
and the loaded agent module, and return a ``{"status": int, "body": dict}``
response. ``apps/api/main.py`` dispatches URL paths to these helpers;
keeping them separate makes the flow trivially unit-testable without
spinning up the HTTP server.

The PR-history and docs flows share the same validation, in-flight
record bookkeeping, credential redaction, and background-spawn logic.
Each agent gets a thin facade (``handle_pr_history_*`` /
``handle_docs_*``) that pins the agent module and exposes the right
public name.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, UTC
from typing import Any

from packages.history_store import PRHistoryStore

logger = logging.getLogger(__name__)

SUPPORTED_MODES = ("local", "cloud")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sanitize_for_storage(body: dict[str, Any]) -> dict[str, Any]:
    """Strip credentials before persisting a request payload.

    Plan invariant: GitHub tokens / Magenta credentials never land in
    MongoDB. Retries that come in with an empty body fall back to
    ``GITHUB_TOKEN`` from the API environment.
    """
    return {key: value for key, value in body.items() if key != "credentials"}


def _validate_start_body(body: dict[str, Any]) -> str | None:
    storage = body.get("storage") or {}
    mode = (storage.get("mode") or "").strip().lower()
    if not mode:
        return "storage.mode is required (one of 'local', 'cloud')"
    if mode not in SUPPORTED_MODES:
        return f"storage.mode {mode!r} is not supported; expected one of {SUPPORTED_MODES}"
    repo = body.get("repository") or {}
    if not (repo.get("full_name") or (repo.get("owner") and repo.get("name"))):
        return "repository.full_name (or owner+name) is required"
    creds = body.get("credentials") or {}
    if not creds.get("github_token"):
        return "credentials.github_token is required"
    return None


def _record_running(
    *,
    store: PRHistoryStore,
    session_id: str,
    body: dict[str, Any],
    agent_id: str,
) -> None:
    storage = body.get("storage") or {}
    repo = body.get("repository") or {}
    repo_key = (
        storage.get("repo_key")
        or repo.get("full_name")
        or f"{repo.get('owner', '')}/{repo.get('name', '')}".strip("/")
    )
    store.start_onboarding_run(
        {
            "onboarding_run_id": session_id,
            "repo_key": repo_key,
            "status": "running",
            "started_at": _utc_now(),
            "scan": body.get("scan") or {},
            "storage_mode": storage.get("mode"),
            "agent": agent_id,
            "request_payload": _sanitize_for_storage(body),
        }
    )


def _spawn_agent(
    *,
    session_id: str,
    body: dict[str, Any],
    store: PRHistoryStore,
    agent_module: Any,
) -> None:
    """Run the agent in a daemon background thread.

    A sudden API process exit leaves the in-flight onboarding_run record
    in ``running`` until the next ``start``/``retry`` rewrites it; the
    user can always retry. We do not currently bound the thread to a
    timeout — the agent itself enforces scan caps.
    """

    def _execute() -> None:
        payload = {**body, "onboarding_run_id": session_id}
        try:
            agent_module.set_store_factory(lambda _payload: store)
            result = agent_module.app.invoke(payload)
            if result.get("status") == "completed":
                return
            errors = (result.get("output") or {}).get("errors") or []
            store.complete_onboarding_run(
                session_id,
                {
                    "status": "failed",
                    "errors": errors,
                    "agent_status": result.get("status"),
                },
            )
        except Exception as exc:  # noqa: BLE001 - we must mark the run failed
            logger.exception("onboarding agent thread failed")
            try:
                store.complete_onboarding_run(
                    session_id,
                    {"status": "failed", "errors": [f"{type(exc).__name__}: {exc}"]},
                )
            except Exception:  # noqa: BLE001
                logger.exception("failed to mark onboarding run failed")

    threading.Thread(target=_execute, daemon=True).start()


def _generic_start(
    *,
    session_id: str,
    body: dict[str, Any],
    store: PRHistoryStore,
    agent_module: Any,
    agent_id: str,
) -> dict[str, Any]:
    error = _validate_start_body(body)
    if error:
        return {"status": 400, "body": {"error": error}}

    _record_running(
        store=store, session_id=session_id, body=body, agent_id=agent_id
    )
    _spawn_agent(session_id=session_id, body=body, store=store, agent_module=agent_module)

    run = store.get_onboarding_run(session_id) or {}
    return {
        "status": 202,
        "body": {
            "onboarding_run_id": session_id,
            "state": "running",
            "run": run,
        },
    }


def _generic_status(session_id: str, *, store: PRHistoryStore) -> dict[str, Any]:
    run = store.get_onboarding_run(session_id)
    if run is None:
        return {"status": 404, "body": {"error": f"onboarding run {session_id!r} not found"}}
    return {"status": 200, "body": {"run": run}}


def _generic_retry(
    *,
    session_id: str,
    body: dict[str, Any],
    store: PRHistoryStore,
    agent_module: Any,
    agent_id: str,
) -> dict[str, Any]:
    existing = store.get_onboarding_run(session_id)
    if existing is None:
        return {
            "status": 404,
            "body": {"error": f"onboarding run {session_id!r} not found; call start first"},
        }

    reuse_payload = existing.get("request_payload") if not body else None
    effective_body = dict(body) if body else dict(reuse_payload or {})
    if not effective_body:
        return {
            "status": 400,
            "body": {
                "error": "retry requires a body or a previously-stored request_payload"
            },
        }

    if not (effective_body.get("credentials") or {}).get("github_token"):
        env_token = os.environ.get("GITHUB_TOKEN", "").strip()
        if env_token:
            effective_body["credentials"] = {
                **(effective_body.get("credentials") or {}),
                "github_token": env_token,
            }

    error = _validate_start_body(effective_body)
    if error:
        return {"status": 400, "body": {"error": error}}

    retry_count = int(existing.get("retry_count", 0)) + 1
    _record_running(
        store=store,
        session_id=session_id,
        body=effective_body,
        agent_id=agent_id,
    )
    # Bump the retry counter on the in-flight record (no credentials).
    store.start_onboarding_run(
        {
            "onboarding_run_id": session_id,
            "retry_count": retry_count,
            "status": "running",
            "request_payload": _sanitize_for_storage(effective_body),
        }
    )
    _spawn_agent(
        session_id=session_id,
        body=effective_body,
        store=store,
        agent_module=agent_module,
    )

    run = store.get_onboarding_run(session_id) or {}
    return {
        "status": 202,
        "body": {
            "onboarding_run_id": session_id,
            "state": "running",
            "retry_count": retry_count,
            "run": run,
        },
    }


# ── PR-history facade ────────────────────────────────────────────────


def handle_pr_history_start(
    *,
    session_id: str,
    body: dict[str, Any],
    store: PRHistoryStore,
    agent_module: Any,
) -> dict[str, Any]:
    """``POST /api/onboarding/{session_id}/pr-history/start``."""
    return _generic_start(
        session_id=session_id,
        body=body,
        store=store,
        agent_module=agent_module,
        agent_id="pr-history-indexer",
    )


def handle_pr_history_status(
    session_id: str,
    *,
    store: PRHistoryStore,
) -> dict[str, Any]:
    """``GET /api/onboarding/{session_id}/pr-history/status``."""
    return _generic_status(session_id, store=store)


def handle_pr_history_retry(
    *,
    session_id: str,
    body: dict[str, Any],
    store: PRHistoryStore,
    agent_module: Any,
) -> dict[str, Any]:
    """``POST /api/onboarding/{session_id}/pr-history/retry``."""
    return _generic_retry(
        session_id=session_id,
        body=body,
        store=store,
        agent_module=agent_module,
        agent_id="pr-history-indexer",
    )


# ── Docs facade ──────────────────────────────────────────────────────


def handle_docs_start(
    *,
    session_id: str,
    body: dict[str, Any],
    store: PRHistoryStore,
    agent_module: Any,
) -> dict[str, Any]:
    """``POST /api/onboarding/{session_id}/docs/start``."""
    return _generic_start(
        session_id=session_id,
        body=body,
        store=store,
        agent_module=agent_module,
        agent_id="docs-indexer",
    )


def handle_docs_status(
    session_id: str,
    *,
    store: PRHistoryStore,
) -> dict[str, Any]:
    """``GET /api/onboarding/{session_id}/docs/status``."""
    return _generic_status(session_id, store=store)


def handle_docs_retry(
    *,
    session_id: str,
    body: dict[str, Any],
    store: PRHistoryStore,
    agent_module: Any,
) -> dict[str, Any]:
    """``POST /api/onboarding/{session_id}/docs/retry``."""
    return _generic_retry(
        session_id=session_id,
        body=body,
        store=store,
        agent_module=agent_module,
        agent_id="docs-indexer",
    )
