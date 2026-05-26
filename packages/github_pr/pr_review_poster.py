"""Post a single line-anchored review on a PR.

Parallel to ``pr_poster.upsert_pr_comment`` but for the *review* endpoint —
``POST /pulls/{n}/reviews`` — which accepts a ``comments[]`` array that
GitHub renders as inline file comments grouped under one review.

We use ``event="COMMENT"`` so this never blocks the merge — hard blocks
stay in ``submit_pr_review`` with ``REQUEST_CHANGES``. The two paths are
coordinated by ``pr_actions.apply_tiered_actions``: blocked PRs get
REQUEST_CHANGES, everyone else can opt into the inline COMMENT review via
the ``MERGEGUARD_INLINE_REVIEWS`` env flag.

Idempotency model:
  * The previous MergeGuard inline review (if any) is dismissed first,
    then a fresh review is posted. GitHub keeps the dismissed review in
    the audit trail; the line comments remain as "outdated" history.
  * Callers persist the returned ``review_id`` so the next run can target
    it for dismissal.

This module does NOT inspect findings — pass in already-resolved comment
dicts shaped as ``{path, line, side, body, severity?, title?}``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Iterable

from .pr_poster import (
    GITHUB_API,
    STICKY_MARKER,
    GitHubPostError,
    _api_call,
)

# Marker embedded in the review body so we can identify MergeGuard reviews on
# subsequent runs (the sticky marker can't be reused on a review without
# polluting search across other GitHub Apps; we use a dedicated tag).
INLINE_REVIEW_MARKER = "<!-- mergeguard:inline-review -->"


def upsert_inline_review(
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    comments: list[dict[str, Any]],
    token: str,
    *,
    previous_review_id: int | None = None,
    review_body: str = "",
    api_base_url: str = GITHUB_API,
) -> dict[str, Any]:
    """Dismiss any prior MergeGuard inline review, then post a fresh one.

    Args:
        head_sha: the SHA the review's line anchors are valid for. GitHub
            rejects reviews whose ``commit_id`` doesn't match a head commit
            in the PR's known set.
        comments: list of ``{path, line, side, body}`` (extra keys are
            stripped). Empty list → nothing posted, prior review (if any)
            is still dismissed so we don't leave stale comments behind.
        previous_review_id: when supplied, dismissed before the new post.
            Pass ``None`` to discover via the API (slightly slower but
            useful when the caller hasn't persisted the id yet).
        review_body: optional summary shown above the inline comments.
            ``INLINE_REVIEW_MARKER`` is appended automatically.

    Returns:
        ``{"review_id": int | None, "comment_count": int, "dismissed_review_id": int | None}``
    """
    cleaned = _clean_comments(comments)

    target_review_id = previous_review_id
    if target_review_id is None:
        target_review_id = _find_prior_inline_review_id(
            repo_full_name, pr_number, token, api_base_url=api_base_url
        )

    dismissed_id: int | None = None
    if target_review_id:
        try:
            _dismiss_review(
                repo_full_name,
                pr_number,
                target_review_id,
                token,
                api_base_url=api_base_url,
                message="MergeGuard re-evaluation: inline comments refreshed.",
            )
            dismissed_id = target_review_id
        except GitHubPostError as exc:
            # Best-effort: a missing/already-dismissed review shouldn't
            # block the new post. Surface in the result so callers can
            # log it.
            dismissed_id = None
            review_body = (
                f"{review_body}\n\n_(note: failed to dismiss prior review {target_review_id}: {exc})_"
            ).strip()

    if not cleaned:
        return {
            "review_id": None,
            "comment_count": 0,
            "dismissed_review_id": dismissed_id,
        }

    body_with_marker = (
        f"{INLINE_REVIEW_MARKER}\n{review_body}".rstrip()
        if INLINE_REVIEW_MARKER not in review_body
        else review_body
    )

    payload = {
        "commit_id": head_sha,
        "event": "COMMENT",
        "body": body_with_marker,
        "comments": cleaned,
    }
    url = f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/pulls/{pr_number}/reviews"
    response = _api_call(url, token, method="POST", payload=payload)
    return {
        "review_id": response.get("id"),
        "comment_count": len(cleaned),
        "dismissed_review_id": dismissed_id,
    }


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _clean_comments(comments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop anything GitHub will reject; normalize shape."""
    out: list[dict[str, Any]] = []
    for raw in comments or []:
        path = str(raw.get("path") or "").strip()
        body = str(raw.get("body") or "").strip()
        line = raw.get("line")
        if not path or not body or not isinstance(line, int) or line <= 0:
            continue
        side = str(raw.get("side") or "RIGHT").upper()
        if side not in {"LEFT", "RIGHT"}:
            side = "RIGHT"
        out.append({"path": path, "line": line, "side": side, "body": body})
    return out


def _find_prior_inline_review_id(
    repo_full_name: str,
    pr_number: int,
    token: str,
    *,
    api_base_url: str,
) -> int | None:
    """Most recent non-dismissed MergeGuard inline review on this PR."""
    url = (
        f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/pulls/{pr_number}/reviews?per_page=100"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            reviews = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None
    if not isinstance(reviews, list):
        return None
    for review in reversed(reviews):
        if not isinstance(review, dict):
            continue
        body = review.get("body") or ""
        state = review.get("state") or ""
        # Match either marker so we can co-exist with a future variant.
        if INLINE_REVIEW_MARKER in body or (STICKY_MARKER in body and state == "COMMENTED"):
            if state not in {"DISMISSED"}:
                review_id = review.get("id")
                if isinstance(review_id, int):
                    return review_id
    return None


def _dismiss_review(
    repo_full_name: str,
    pr_number: int,
    review_id: int,
    token: str,
    *,
    api_base_url: str,
    message: str,
) -> dict[str, Any]:
    """PUT ``/reviews/{id}/dismissals``. ``event`` must be ``DISMISS``."""
    url = (
        f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/pulls/{pr_number}"
        f"/reviews/{review_id}/dismissals"
    )
    return _api_call(url, token, method="PUT", payload={"message": message, "event": "DISMISS"})
