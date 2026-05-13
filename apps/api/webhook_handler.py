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
    GitHubPostError,
    build_envelope,
    hydrate_pull_request_payload,
    load_app_auth_from_env,
    normalize_github_pr_payload,
    post_check_run,
    status_to_check_conclusion,
    upsert_pr_comment,
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

    # Post results back to GitHub (best-effort — don't fail the webhook if posting fails).
    repo_full_name = normalized["repository"]["full_name"]
    pr_num = normalized["pull_request"]["number"]
    head_sha = normalized["pull_request"]["head_sha"]

    comment_body = summary.get("comment") or _fallback_comment(summary)
    try:
        comment = upsert_pr_comment(repo_full_name, int(pr_num), comment_body, token)
        logger.info(
            "✓ posted sticky PR comment id=%s on %s#%s",
            comment.get("id"), repo_full_name, pr_num,
        )
    except GitHubPostError as e:
        logger.warning("⚠ failed to upsert PR comment: %s", e)

    check_status, conclusion = status_to_check_conclusion(summary.get("status", "review"))
    try:
        check = post_check_run(
            repo_full_name,
            head_sha,
            name="MergeGuard",
            status=check_status,
            conclusion=conclusion,
            output_title=f"MergeGuard: {summary.get('status', 'review')}",
            output_summary=summary.get("top_blocker") or summary.get("next_action") or "",
            token=token,
        )
        logger.info(
            "✓ posted check_run id=%s status=%s conclusion=%s on %s@%s",
            check.get("id"), check_status, conclusion, repo_full_name, head_sha[:12],
        )
    except GitHubPostError as e:
        logger.warning("⚠ failed to post check run: %s", e)

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
        },
    )


def _resolve_installation_token(installation_id: int | None) -> str | None:
    """Return a usable Bearer token, or None if no credentials are configured."""
    auth = load_app_auth_from_env()
    if auth and installation_id:
        return auth.get_installation_token(installation_id).token
    return os.environ.get("GITHUB_TOKEN") or None


def _fallback_comment(summary: dict[str, Any]) -> str:
    status = summary.get("status", "review")
    risk = summary.get("risk_score", 0)
    return (
        f"**MergeGuard:** `{status}` (risk score: {risk})\n\n"
        f"{summary.get('top_blocker') or summary.get('next_action') or ''}"
    )
