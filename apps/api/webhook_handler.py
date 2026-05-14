"""End-to-end GitHub webhook handler for MergeGuard.

Stages, in order:
    1. Verify ``X-Hub-Signature-256`` HMAC against ``GITHUB_WEBHOOK_SECRET``
    2. Filter event type (only ``pull_request`` with relevant actions)
    3. Build a :class:`WebhookEnvelope` for logging / future async queue use
    4. Resolve an installation token (GitHub App) or fall back to ``GITHUB_TOKEN``
    5. Fetch PR file patches via REST
    6. Run the orchestrator
    7. Post the sticky comment + check run back to GitHub
    8. Return summary JSON
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from packages.github_pr import (
    GitHubAuthError,
    GitHubFetchError,
    apply_tiered_actions,
    build_envelope,
    hydrate_pull_request_payload,
    load_app_auth_from_env,
    normalize_github_pr_payload,
    verify_hmac_sha256,
)
from packages.mongo import LocalMergeGuardStore
from packages.orchestration.engine import MergeGuardOrchestrator

logger = logging.getLogger(__name__)

RELEVANT_PR_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


class WebhookResponse:
    """Container for the handler's HTTP response."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body


def handle_github_webhook(
    repo_root: Path,
    body_bytes: bytes,
    headers: dict[str, str],
    *,
    store_path: Path,
) -> WebhookResponse:
    """Synchronous webhook entry point.

    ``headers`` is a case-insensitive-ish dict — caller passes lowercased keys.
    """
    t0 = time.monotonic()
    delivery_id = headers.get("x-github-delivery", "")
    event = headers.get("x-github-event", "")
    signature = headers.get("x-hub-signature-256")

    logger.info(
        "▶ webhook request received delivery_id=%s event=%s bytes=%d",
        delivery_id or "?", event or "?", len(body_bytes),
    )

    if not delivery_id:
        logger.warning("✗ rejected: missing X-GitHub-Delivery header")
        return WebhookResponse(400, {"error": "missing X-GitHub-Delivery header"})
    if not event:
        logger.warning("✗ rejected: missing X-GitHub-Event header delivery_id=%s", delivery_id)
        return WebhookResponse(400, {"error": "missing X-GitHub-Event header"})

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        logger.error("✗ GITHUB_WEBHOOK_SECRET not configured — request dropped")
        return WebhookResponse(500, {"error": "GITHUB_WEBHOOK_SECRET not configured"})

    if not verify_hmac_sha256(secret, body_bytes, signature):
        logger.warning("✗ HMAC verification FAILED delivery_id=%s event=%s", delivery_id, event)
        return WebhookResponse(401, {"error": "invalid signature"})
    logger.info("✓ HMAC verified delivery_id=%s", delivery_id)

    try:
        raw_payload: dict[str, Any] = json.loads(body_bytes)
    except json.JSONDecodeError as e:
        logger.warning("✗ invalid JSON body delivery_id=%s: %s", delivery_id, e)
        return WebhookResponse(400, {"error": f"invalid JSON body: {e}"})

    if event == "ping":
        logger.info("✓ ping acknowledged delivery_id=%s", delivery_id)
        return WebhookResponse(200, {"pong": True, "delivery_id": delivery_id})

    envelope = build_envelope(delivery_id, event, raw_payload)
    logger.info(
        "✓ envelope built delivery_id=%s event=%s action=%s repo=%s installation_id=%s sender=%s",
        envelope.delivery_id, envelope.event, envelope.action, envelope.repo,
        envelope.installation_id, envelope.sender_login,
    )

    if event != "pull_request":
        logger.info("⤳ skipping: event %r not handled (delivery_id=%s)", event, delivery_id)
        return WebhookResponse(
            200,
            {"skipped": True, "reason": f"event {event!r} not handled", "delivery_id": delivery_id},
        )

    if envelope.action not in RELEVANT_PR_ACTIONS:
        logger.info(
            "⤳ skipping: pull_request action %r not in %s (delivery_id=%s)",
            envelope.action, sorted(RELEVANT_PR_ACTIONS), delivery_id,
        )
        return WebhookResponse(
            200,
            {
                "skipped": True,
                "reason": f"pull_request action {envelope.action!r} not handled",
                "delivery_id": delivery_id,
            },
        )

    pr_number = (raw_payload.get("pull_request") or {}).get("number")
    repo_full = (raw_payload.get("repository") or {}).get("full_name")
    logger.info("▷ analysing PR #%s in %s (delivery_id=%s)", pr_number, repo_full, delivery_id)

    # Resolve a token: GitHub App preferred, fall back to PAT.
    try:
        token = _resolve_installation_token(envelope.installation_id)
    except GitHubAuthError as e:
        logger.exception("✗ failed to resolve installation token: %s", e)
        return WebhookResponse(500, {"error": f"github auth failed: {e}"})

    if not token:
        logger.error("✗ no GitHub credentials available — set GITHUB_APP_ID + key path, or GITHUB_TOKEN")
        return WebhookResponse(
            500,
            {
                "error": (
                    "no GitHub credentials available — set GITHUB_APP_ID + "
                    "GITHUB_APP_PRIVATE_KEY_PATH (preferred) or GITHUB_TOKEN"
                ),
            },
        )
    auth_kind = "github-app" if envelope.installation_id and load_app_auth_from_env() else "github-token"
    logger.info("✓ resolved %s token (length=%d)", auth_kind, len(token))

    # Fetch PR files + build canonical payload.
    t_fetch = time.monotonic()
    try:
        hydrated = hydrate_pull_request_payload(raw_payload, token, fetch_content=False)
        logger.info(
            "✓ fetched %d changed file(s) for PR #%s in %.0fms",
            len(hydrated.get("changed_files", [])), pr_number, (time.monotonic() - t_fetch) * 1000,
        )
        normalized = normalize_github_pr_payload(hydrated)
        logger.info("✓ payload normalized for PR #%s", pr_number)
    except (GitHubFetchError, ValueError) as e:
        logger.exception("✗ failed to hydrate PR payload: %s", e)
        return WebhookResponse(502, {"error": f"failed to fetch PR data: {e}"})

    # Run the orchestrator.
    t_run = time.monotonic()
    logger.info("▷ running orchestrator (12-agent pipeline) on PR #%s", pr_number)
    store = LocalMergeGuardStore(store_path)
    store.load()
    run = MergeGuardOrchestrator(repo_root, store).analyze_pull_request(normalized)
    summary = run.get("summary") or {}
    logger.info(
        "✓ orchestrator done in %.0fms run_id=%s state=%s status=%s risk=%s",
        (time.monotonic() - t_run) * 1000, run.get("id"), run.get("state"),
        summary.get("status"), summary.get("risk_score"),
    )

    # Post results back to GitHub (best-effort — collected into ActionReport).
    repo_full_name = normalized["repository"]["full_name"]
    pr_num = int(normalized["pull_request"]["number"])
    head_sha = normalized["pull_request"]["head_sha"]
    details_url = _details_url_for(run.get("id"))

    logger.info(
        "▷ applying tiered actions (status=%s risk=%s) on %s#%s",
        summary.get("status"), summary.get("risk_score"), repo_full_name, pr_num,
    )
    action_report = apply_tiered_actions(
        repo_full_name, pr_num, head_sha, summary, token, details_url=details_url
    )

    logger.info(
        "✓ tiered actions: comment_id=%s check_run_id=%s review=%s labels_added=%s labels_removed=%s reviewers=%s",
        action_report.comment_id,
        action_report.check_run_id,
        f"{action_report.review_action}#{action_report.review_id}" if action_report.review_action else None,
        action_report.labels_added or "[]",
        action_report.labels_removed or "[]",
        action_report.reviewers_requested or "[]",
    )
    for warning in action_report.warnings:
        logger.warning("⚠ tiered-action warning: %s", warning)

    logger.info(
        "◀ webhook complete delivery_id=%s pr=%s#%s total=%.0fms",
        delivery_id, repo_full_name, pr_num, (time.monotonic() - t0) * 1000,
    )
    return WebhookResponse(
        200,
        {
            "delivery_id": delivery_id,
            "run_id": run.get("id"),
            "state": run.get("state"),
            "status": summary.get("status"),
            "risk_score": summary.get("risk_score"),
            "actions": {
                "comment_id": action_report.comment_id,
                "check_run_id": action_report.check_run_id,
                "review_id": action_report.review_id,
                "review_action": action_report.review_action,
                "labels_added": action_report.labels_added,
                "labels_removed": action_report.labels_removed,
                "reviewers_requested": action_report.reviewers_requested,
                "warnings": action_report.warnings,
            },
        },
    )


def _resolve_installation_token(installation_id: int | None) -> str | None:
    """Return a usable Bearer token, or None if no credentials are configured."""
    auth = load_app_auth_from_env()
    if auth and installation_id:
        return auth.get_installation_token(installation_id).token
    return os.environ.get("GITHUB_TOKEN") or None


def _details_url_for(run_id: str | None) -> str | None:
    """Build a public dashboard URL for the given run, or None if unconfigured."""
    if not run_id:
        return None
    public = os.environ.get("MERGEGUARD_PUBLIC_URL", "").rstrip("/")
    if not public:
        return None
    return f"{public}/api/runs/{run_id}"
