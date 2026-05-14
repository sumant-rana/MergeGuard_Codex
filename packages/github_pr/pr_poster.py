"""Post MergeGuard analysis results back to GitHub: sticky comment + check run.

Both require an installation token (see ``app_client.py``). The "sticky"
comment is the existing-comment-with-marker pattern — find a previous
MergeGuard comment, update it; otherwise create a new one.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

GITHUB_API = "https://api.github.com"

STICKY_MARKER = "<!-- mergeguard:sticky -->"


class GitHubPostError(Exception):
    """Raised when posting back to GitHub fails."""


def upsert_pr_comment(
    repo_full_name: str,
    pr_number: int,
    body: str,
    token: str,
    *,
    api_base_url: str = GITHUB_API,
) -> dict[str, Any]:
    """Create or update MergeGuard's sticky PR comment.

    Identifies the previous sticky comment by the embedded ``STICKY_MARKER``.
    If found → ``PATCH /issues/comments/{id}``; otherwise ``POST /issues/{n}/comments``.
    """
    body_with_marker = body if STICKY_MARKER in body else f"{STICKY_MARKER}\n{body}"
    existing = _find_sticky_comment(repo_full_name, pr_number, token, api_base_url=api_base_url)

    if existing:
        url = f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/issues/comments/{existing['id']}"
        return _api_call(url, token, method="PATCH", payload={"body": body_with_marker})

    url = f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/issues/{pr_number}/comments"
    return _api_call(url, token, method="POST", payload={"body": body_with_marker})


def post_check_run(
    repo_full_name: str,
    head_sha: str,
    name: str,
    status: str,
    token: str,
    *,
    conclusion: str | None = None,
    output_title: str | None = None,
    output_summary: str | None = None,
    details_url: str | None = None,
    api_base_url: str = GITHUB_API,
) -> dict[str, Any]:
    """Create a check run on the given head SHA.

    Args:
        status: ``queued`` | ``in_progress`` | ``completed``
        conclusion: required when ``status == "completed"``. One of
            ``success`` | ``failure`` | ``neutral`` | ``cancelled`` |
            ``skipped`` | ``timed_out`` | ``action_required``.
    """
    payload: dict[str, Any] = {
        "name": name,
        "head_sha": head_sha,
        "status": status,
    }
    if conclusion:
        payload["conclusion"] = conclusion
    if details_url:
        payload["details_url"] = details_url
    if output_title or output_summary:
        payload["output"] = {
            "title": output_title or name,
            "summary": output_summary or "",
        }

    url = f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/check-runs"
    return _api_call(url, token, method="POST", payload=payload)


def _find_sticky_comment(
    repo_full_name: str,
    pr_number: int,
    token: str,
    *,
    api_base_url: str,
) -> dict[str, Any] | None:
    """Return the first existing MergeGuard sticky comment, or None."""
    page = 1
    while True:
        url = (
            f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/issues/{pr_number}/comments"
            f"?per_page=100&page={page}"
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
                comments = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError):
            return None

        if not isinstance(comments, list) or not comments:
            return None
        for comment in comments:
            if STICKY_MARKER in (comment.get("body") or ""):
                return comment
        if len(comments) < 100:
            return None
        page += 1


def _api_call(
    url: str,
    token: str,
    *,
    method: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    body_bytes = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body_bytes,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise GitHubPostError(
            f"GitHub returned {e.code} on {method} {url}: "
            f"{e.read().decode('utf-8', 'replace')[:300]}"
        ) from e
    except urllib.error.URLError as e:
        raise GitHubPostError(f"Network failure on {method} {url}: {e}") from e


def status_to_check_conclusion(status: str) -> tuple[str, str]:
    """Map MergeGuard status strings to (check_status, check_conclusion).

    MergeGuard summary status is one of ``"pass" | "review" | "blocked"``.
    """
    if status == "blocked":
        return "completed", "failure"
    if status == "review":
        return "completed", "neutral"
    return "completed", "success"


# ---------------------------------------------------------------------------
# Labels + reviewers + reviews — the building blocks for tiered PR actions
# (see ``packages/github_pr/pr_actions.py``).
# ---------------------------------------------------------------------------


def list_pr_labels(
    repo_full_name: str,
    pr_number: int,
    token: str,
    *,
    api_base_url: str = GITHUB_API,
) -> list[str]:
    """Return the current label names on a PR."""
    url = (
        f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/issues/{pr_number}/labels?per_page=100"
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
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        raise GitHubPostError(f"Failed to list labels on {repo_full_name}#{pr_number}: {e}") from e
    return [item.get("name", "") for item in payload if isinstance(item, dict)]


def add_pr_labels(
    repo_full_name: str,
    pr_number: int,
    labels: list[str],
    token: str,
    *,
    api_base_url: str = GITHUB_API,
) -> list[str]:
    """Idempotently add labels to a PR. Returns the full label list after the call."""
    if not labels:
        return list_pr_labels(repo_full_name, pr_number, token, api_base_url=api_base_url)
    url = f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/issues/{pr_number}/labels"
    payload = _api_call(url, token, method="POST", payload={"labels": labels})
    # GitHub returns the full label set as a list of dicts.
    if isinstance(payload, list):
        return [item.get("name", "") for item in payload if isinstance(item, dict)]
    return []


def remove_pr_label(
    repo_full_name: str,
    pr_number: int,
    label: str,
    token: str,
    *,
    api_base_url: str = GITHUB_API,
) -> None:
    """Remove a single label from a PR. Silently ignores 404 (already removed)."""
    url = (
        f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/issues/{pr_number}"
        f"/labels/{urllib.parse.quote(label, safe='')}"
    )
    request = urllib.request.Request(
        url,
        method="DELETE",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30):
            return
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return
        raise GitHubPostError(
            f"Failed to remove label {label!r} from {repo_full_name}#{pr_number}: {e.code} "
            f"{e.read().decode('utf-8', 'replace')[:200]}"
        ) from e
    except urllib.error.URLError as e:
        raise GitHubPostError(
            f"Network failure removing label {label!r} from {repo_full_name}#{pr_number}: {e}"
        ) from e


def request_pr_reviewers(
    repo_full_name: str,
    pr_number: int,
    reviewers: list[str],
    token: str,
    *,
    team_reviewers: list[str] | None = None,
    api_base_url: str = GITHUB_API,
) -> dict[str, Any]:
    """Request reviews from individual users and/or teams.

    ``reviewers`` is a list of GitHub usernames (no leading ``@``).
    ``team_reviewers`` is a list of team slugs (org repos only).
    No-ops if both lists are empty.
    """
    if not reviewers and not team_reviewers:
        return {}
    url = f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/pulls/{pr_number}/requested_reviewers"
    body: dict[str, Any] = {}
    if reviewers:
        body["reviewers"] = reviewers
    if team_reviewers:
        body["team_reviewers"] = team_reviewers
    return _api_call(url, token, method="POST", payload=body)


def submit_pr_review(
    repo_full_name: str,
    pr_number: int,
    event: str,
    body: str,
    token: str,
    *,
    api_base_url: str = GITHUB_API,
) -> dict[str, Any]:
    """Submit a PR review.

    Args:
        event: ``APPROVE`` | ``REQUEST_CHANGES`` | ``COMMENT``.
            ``REQUEST_CHANGES`` is the hard-block mechanism that prevents
            merge until the review is dismissed by a maintainer or replaced
            by an APPROVE from the same author.
    """
    url = f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/pulls/{pr_number}/reviews"
    return _api_call(url, token, method="POST", payload={"event": event, "body": body})


def find_pending_mergeguard_review(
    repo_full_name: str,
    pr_number: int,
    token: str,
    *,
    api_base_url: str = GITHUB_API,
    marker: str = STICKY_MARKER,
) -> dict[str, Any] | None:
    """Return the most recent non-dismissed MergeGuard ``REQUEST_CHANGES`` review.

    A review is identified as MergeGuard's by the embedded ``STICKY_MARKER``
    in its body. We look at the latest review page only — enough for the
    common case where a PR has < 100 reviews.
    """
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
        if review.get("state") not in {"CHANGES_REQUESTED", "PENDING"}:
            continue
        if marker in (review.get("body") or ""):
            return review
    return None


def dismiss_pr_review(
    repo_full_name: str,
    pr_number: int,
    review_id: int,
    message: str,
    token: str,
    *,
    api_base_url: str = GITHUB_API,
) -> dict[str, Any]:
    """Dismiss a CHANGES_REQUESTED review so the PR can merge again."""
    url = (
        f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/pulls/{pr_number}"
        f"/reviews/{review_id}/dismissals"
    )
    return _api_call(url, token, method="PUT", payload={"message": message, "event": "DISMISS"})
