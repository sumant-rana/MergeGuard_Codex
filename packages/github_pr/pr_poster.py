"""Post MergeGuard analysis results back to GitHub: sticky comment + check run.

Both require an installation token (see ``app_client.py``). The "sticky"
comment is the existing-comment-with-marker pattern — find a previous
MergeGuard comment, update it; otherwise create a new one.
"""

from __future__ import annotations

import json
import urllib.error
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
