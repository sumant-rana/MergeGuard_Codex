"""Tiered GitHub actions based on MergeGuard's summary status.

The webhook handler used to inline three calls: ``upsert_pr_comment``,
``post_check_run``, and (eventually) a label call. As we grew the action
surface (labels, reviewers, request-changes reviews), the inline approach
got noisy. This module centralizes the decision: given a summary, do the
right set of GitHub actions and report what was done.

Tiered policy:

    status == "pass"
        • Sticky comment
        • Check-run: success
        • Label: mergeguard:safe (plus removal of any prior status labels)

    status == "review"
        • Sticky comment (with checklist if available)
        • Check-run: neutral
        • Labels: mergeguard:needs-review + concern-specific tags
        • Auto-request reviewers from CODEOWNERS (if present in the
          ``review-compression`` agent output)

    status == "blocked"
        • Sticky comment (with explicit BLOCKED callout + top blocker)
        • Check-run: failure
        • Labels: mergeguard:blocked + concern-specific tags
        • Submit a PR review with event=REQUEST_CHANGES — the hard-block
          mechanism that prevents merge regardless of branch protection
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .pr_poster import (
    GitHubPostError,
    STICKY_MARKER,
    add_pr_labels,
    dismiss_pr_review,
    find_pending_mergeguard_review,
    list_pr_labels,
    post_check_run,
    remove_pr_label,
    request_pr_reviewers,
    status_to_check_conclusion,
    submit_pr_review,
    upsert_pr_comment,
)

logger = logging.getLogger(__name__)

ALL_STATUS_LABELS = ("mergeguard:safe", "mergeguard:needs-review", "mergeguard:blocked")

# Concept-classifier concept → label name. Only concepts we want to flag
# at the PR level — granular ones (e.g. ``retry``, ``timeout``) stay in
# the comment body.
CONCEPT_LABEL_MAP: dict[str, str] = {
    "pii_read": "mergeguard:has-pii",
    "pii_write": "mergeguard:has-pii",
    "secret": "mergeguard:secret-exposed",
    "secret_exposure": "mergeguard:secret-exposed",
    "auth": "mergeguard:auth-touched",
    "billing": "mergeguard:financial",
    "external_http": "mergeguard:external-http",
    "sql": "mergeguard:sql-touched",
    "prompt": "mergeguard:prompt-touched",
}


@dataclass
class ActionReport:
    """Audit trail of what was actually done on GitHub."""

    status: str
    comment_id: int | None = None
    check_run_id: int | None = None
    review_id: int | None = None
    review_action: str | None = None  # "submitted" | "dismissed" | None
    labels_added: list[str] = field(default_factory=list)
    labels_removed: list[str] = field(default_factory=list)
    reviewers_requested: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def apply_tiered_actions(
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    summary: dict[str, Any],
    token: str,
    *,
    details_url: str | None = None,
) -> ActionReport:
    """Apply the full bundle of GitHub actions for a completed MergeGuard run.

    All sub-calls are best-effort: a failed comment doesn't prevent the
    check run from being posted, and so on. Failures are captured in
    ``ActionReport.warnings`` for the caller to log.
    """
    status = summary.get("status", "review")
    report = ActionReport(status=status)

    # 1) Sticky comment
    comment_body = summary.get("comment") or _fallback_comment(summary)
    try:
        comment = upsert_pr_comment(repo_full_name, pr_number, comment_body, token)
        report.comment_id = comment.get("id")
    except GitHubPostError as e:
        report.warnings.append(f"comment: {e}")

    # 2) Check run
    check_status, conclusion = status_to_check_conclusion(status)
    try:
        check = post_check_run(
            repo_full_name,
            head_sha,
            name="MergeGuard",
            status=check_status,
            conclusion=conclusion,
            output_title=f"MergeGuard: {status}",
            output_summary=_check_run_summary(summary),
            details_url=details_url,
            token=token,
        )
        report.check_run_id = check.get("id")
    except GitHubPostError as e:
        report.warnings.append(f"check_run: {e}")

    # 3) Labels — status label + concern labels
    target_labels = _labels_for(summary)
    try:
        existing = set(list_pr_labels(repo_full_name, pr_number, token))
        # Drop stale status labels that don't match the new status.
        for stale in ALL_STATUS_LABELS:
            if stale in existing and stale not in target_labels:
                try:
                    remove_pr_label(repo_full_name, pr_number, stale, token)
                    report.labels_removed.append(stale)
                except GitHubPostError as e:
                    report.warnings.append(f"remove_label[{stale}]: {e}")
        to_add = [label for label in target_labels if label not in existing]
        if to_add:
            add_pr_labels(repo_full_name, pr_number, to_add, token)
            report.labels_added = to_add
    except GitHubPostError as e:
        report.warnings.append(f"labels: {e}")

    # 4) Reviewers (only on 'review' — 'blocked' uses request_changes instead)
    if status == "review":
        reviewers = _reviewers_for(summary)
        if reviewers:
            try:
                request_pr_reviewers(repo_full_name, pr_number, reviewers, token)
                report.reviewers_requested = reviewers
            except GitHubPostError as e:
                report.warnings.append(f"reviewers: {e}")

    # 5) Review — request_changes on 'blocked', dismiss prior on 'pass'/'review'
    try:
        existing_review = find_pending_mergeguard_review(repo_full_name, pr_number, token)
        if status == "blocked":
            # Only submit if there isn't already a pending MergeGuard request_changes.
            if not existing_review:
                review = submit_pr_review(
                    repo_full_name,
                    pr_number,
                    event="REQUEST_CHANGES",
                    body=_blocked_review_body(summary),
                    token=token,
                )
                report.review_id = review.get("id")
                report.review_action = "submitted"
        else:
            # PR has been unblocked — dismiss any prior MergeGuard request_changes.
            if existing_review:
                review_id = int(existing_review.get("id") or 0)
                if review_id:
                    dismiss_pr_review(
                        repo_full_name,
                        pr_number,
                        review_id,
                        f"MergeGuard re-evaluation: status is now `{status}` — earlier blockers cleared.",
                        token,
                    )
                    report.review_id = review_id
                    report.review_action = "dismissed"
    except GitHubPostError as e:
        report.warnings.append(f"review: {e}")

    return report


# ---------------------------------------------------------------------------
# Helpers — pure functions over the summary dict (testable without GitHub)
# ---------------------------------------------------------------------------


def labels_for(summary: dict[str, Any]) -> list[str]:
    """Public wrapper for tests."""
    return _labels_for(summary)


def _labels_for(summary: dict[str, Any]) -> list[str]:
    status = summary.get("status", "review")
    labels: list[str] = []
    if status == "blocked":
        labels.append("mergeguard:blocked")
    elif status == "review":
        labels.append("mergeguard:needs-review")
    else:
        labels.append("mergeguard:safe")

    risk_score = int(summary.get("risk_score") or 0)
    if risk_score >= 70:
        labels.append("mergeguard:high-risk")

    seen: set[str] = set()
    for finding in summary.get("concept_findings") or []:
        if not isinstance(finding, dict):
            continue
        concept = (finding.get("concept") or "").lower()
        label = CONCEPT_LABEL_MAP.get(concept)
        if label and label not in seen:
            seen.add(label)
            labels.append(label)

    return labels


def _reviewers_for(summary: dict[str, Any]) -> list[str]:
    """Distinct reviewer logins drawn from the review-compression hotspots.

    Skips bot accounts (anything ending in ``[bot]``) and empty strings.
    Caps at 6 reviewers to stay within GitHub's per-call limit.
    """
    seen: set[str] = set()
    result: list[str] = []
    for owner in summary.get("owners") or []:
        if not isinstance(owner, str):
            continue
        name = owner.lstrip("@").strip()
        if not name or name.endswith("[bot]") or name in seen:
            continue
        seen.add(name)
        result.append(name)
        if len(result) >= 6:
            break
    return result


def _check_run_summary(summary: dict[str, Any]) -> str:
    status = summary.get("status", "review")
    risk = summary.get("risk_score", 0)
    top = summary.get("top_blocker") or summary.get("next_action") or ""
    return f"status={status} risk_score={risk}\n\n{top}".strip()


def _blocked_review_body(summary: dict[str, Any]) -> str:
    blockers = summary.get("policy_findings") or summary.get("concept_findings") or []
    top = summary.get("top_blocker") or summary.get("next_action") or ""
    lines = [
        STICKY_MARKER,
        "## :no_entry: MergeGuard: changes requested",
        "",
        f"This PR was classified as **blocked** with a risk score of "
        f"**{summary.get('risk_score', 0)}**. A maintainer must dismiss this "
        f"review (or push a fix that clears the blockers) before merge.",
        "",
    ]
    if top:
        lines += ["**Top blocker:**", f"> {top}", ""]
    if blockers:
        lines.append("**Findings flagged as block-severity:**")
        for finding in blockers[:8]:
            if not isinstance(finding, dict):
                continue
            severity = finding.get("severity", "")
            if severity and severity != "block":
                continue
            message = finding.get("message") or finding.get("reason") or finding.get("concept") or ""
            path = finding.get("path") or ""
            lines.append(f"- `{path}` — {message}" if path else f"- {message}")
        lines.append("")
    lines.append("_To unblock: address the findings above (or have a maintainer dismiss this review)._")
    return "\n".join(lines)


def _fallback_comment(summary: dict[str, Any]) -> str:
    status = summary.get("status", "review")
    risk = summary.get("risk_score", 0)
    next_action = summary.get("top_blocker") or summary.get("next_action") or ""
    return (
        f"**MergeGuard:** `{status}` (risk score: {risk})\n\n"
        f"{next_action}"
    )
