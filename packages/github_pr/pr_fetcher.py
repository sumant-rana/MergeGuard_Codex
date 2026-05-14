"""Fetch full PR details (file patches, optional file content) from GitHub REST.

The webhook envelope already contains a ``pull_request`` object with title,
body, base/head SHAs, etc. — but the patches and file content live in
separate endpoints. This module fills in the gaps so the orchestrator sees
the same shape it would from a fixture.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

GITHUB_API = "https://api.github.com"
DEFAULT_MAX_FILES = 100
DEFAULT_MAX_CONTENT_BYTES = 256 * 1024  # 256 KiB cap per file
DEFAULT_MAX_LINKED_ISSUES = 5
DEFAULT_MAX_ISSUE_BODY_BYTES = 16 * 1024  # 16 KiB per issue body

# Recognise both keyword refs ("Closes #5", "Fixes #12") and bare ``#5``
# mentions in PR bodies. Keyword refs are stronger evidence of "this PR
# implements this issue", but bare mentions are also worth fetching for
# context (e.g., when the PR body says "see #12 for background").
_KEYWORD_REF_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)",
    re.IGNORECASE,
)
_BARE_REF_RE = re.compile(r"(?<![\w/])#(\d+)")


class GitHubFetchError(Exception):
    """Raised when the GitHub REST API returns an error."""


def fetch_pr_files(
    repo_full_name: str,
    pr_number: int,
    token: str,
    *,
    api_base_url: str = GITHUB_API,
    max_files: int = DEFAULT_MAX_FILES,
) -> list[dict[str, Any]]:
    """Fetch the list of changed files for a PR with their patches.

    Calls ``GET /repos/{repo}/pulls/{n}/files`` with pagination (per_page=100).
    Returns a list of file dicts matching MergeGuard's ``changed_files`` shape
    (``path``, ``status``, ``additions``, ``deletions``, ``changes``, ``patch``).
    """
    files: list[dict[str, Any]] = []
    page = 1
    while len(files) < max_files:
        url = (
            f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/pulls/{pr_number}/files"
            f"?per_page=100&page={page}"
        )
        page_files = _get_json(url, token)
        if not isinstance(page_files, list) or not page_files:
            break
        for item in page_files:
            files.append(
                {
                    "path": item.get("filename", ""),
                    "status": item.get("status", "modified"),
                    "additions": int(item.get("additions") or 0),
                    "deletions": int(item.get("deletions") or 0),
                    "changes": int(item.get("changes") or 0),
                    "patch": item.get("patch") or "",
                    "sha": item.get("sha") or "",
                    "raw_url": item.get("raw_url") or "",
                }
            )
            if len(files) >= max_files:
                break
        if len(page_files) < 100:
            break
        page += 1
    return files


def fetch_file_content(
    repo_full_name: str,
    path: str,
    ref: str,
    token: str,
    *,
    api_base_url: str = GITHUB_API,
    max_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
) -> str:
    """Fetch a file's raw content at the given ref. Returns empty string on error.

    Uses ``GET /repos/{repo}/contents/{path}?ref={sha}`` with raw media type.
    Soft-fails (returns empty) on 404 / large file errors — the analyzer
    tolerates missing content fields.
    """
    quoted_path = urllib.parse.quote(path, safe="/")
    url = (
        f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/contents/{quoted_path}"
        f"?ref={urllib.parse.quote(ref, safe='')}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.raw",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(max_bytes + 1)
    except (urllib.error.HTTPError, urllib.error.URLError):
        return ""

    if len(body) > max_bytes:
        return body[:max_bytes].decode("utf-8", "replace")
    return body.decode("utf-8", "replace")


def extract_linked_issue_numbers(
    pr_body: str,
    *,
    max_issues: int = DEFAULT_MAX_LINKED_ISSUES,
) -> list[int]:
    """Find issue numbers referenced in a PR body.

    Prioritises keyword refs (``Closes #N``) over bare refs (``#N``) since
    the former is stronger evidence that the PR implements that issue.
    De-duplicates and caps at ``max_issues``.
    """
    if not pr_body:
        return []
    keyword_hits: list[int] = []
    for match in _KEYWORD_REF_RE.finditer(pr_body):
        try:
            keyword_hits.append(int(match.group(1)))
        except (TypeError, ValueError):
            continue
    bare_hits: list[int] = []
    for match in _BARE_REF_RE.finditer(pr_body):
        try:
            bare_hits.append(int(match.group(1)))
        except (TypeError, ValueError):
            continue

    seen: set[int] = set()
    ordered: list[int] = []
    for number in [*keyword_hits, *bare_hits]:
        if number in seen:
            continue
        seen.add(number)
        ordered.append(number)
        if len(ordered) >= max_issues:
            break
    return ordered


def fetch_issue(
    repo_full_name: str,
    issue_number: int,
    token: str,
    *,
    api_base_url: str = GITHUB_API,
    max_body_bytes: int = DEFAULT_MAX_ISSUE_BODY_BYTES,
) -> dict[str, Any] | None:
    """Fetch a single issue by number. Returns None on 404 / network error.

    Returned shape (subset of GitHub's REST issue object):

        {"number", "title", "state", "body", "labels", "html_url"}

    The body is truncated to ``max_body_bytes`` so a very long issue
    doesn't dominate the intent-extractor's input window.
    """
    url = f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/issues/{issue_number}"
    try:
        raw = _get_json(url, token)
    except GitHubFetchError:
        return None
    if not isinstance(raw, dict):
        return None
    # GitHub returns PRs as "issues" too — filter them out so we don't
    # accidentally pull the PR's own metadata as an "issue".
    if raw.get("pull_request"):
        return None
    body = (raw.get("body") or "")
    if len(body) > max_body_bytes:
        body = body[:max_body_bytes] + "\n…[truncated by mergeguard]"
    labels = [
        label.get("name", "")
        for label in (raw.get("labels") or [])
        if isinstance(label, dict) and label.get("name")
    ]
    return {
        "number": int(raw.get("number") or issue_number),
        "title": str(raw.get("title") or ""),
        "state": str(raw.get("state") or ""),
        "body": body,
        "labels": labels,
        "html_url": str(raw.get("html_url") or ""),
    }


def fetch_linked_issues(
    repo_full_name: str,
    pr_body: str,
    token: str,
    *,
    api_base_url: str = GITHUB_API,
    max_issues: int = DEFAULT_MAX_LINKED_ISSUES,
) -> list[dict[str, Any]]:
    """Extract issue refs from a PR body and fetch each one. Failures soft-fail."""
    numbers = extract_linked_issue_numbers(pr_body, max_issues=max_issues)
    results: list[dict[str, Any]] = []
    for number in numbers:
        issue = fetch_issue(repo_full_name, number, token, api_base_url=api_base_url)
        if issue:
            results.append(issue)
    return results


def _get_json(url: str, token: str) -> Any:
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
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise GitHubFetchError(
            f"GitHub returned {e.code} on {url}: {e.read().decode('utf-8', 'replace')[:300]}"
        ) from e
    except urllib.error.URLError as e:
        raise GitHubFetchError(f"Network failure fetching {url}: {e}") from e


def hydrate_pull_request_payload(
    envelope_payload: dict[str, Any],
    token: str,
    *,
    fetch_content: bool = False,
    api_base_url: str = GITHUB_API,
) -> dict[str, Any]:
    """Convert a raw webhook payload into the MergeGuard canonical shape.

    The webhook gives us ``repository`` and ``pull_request`` already, but
    ``changed_files`` must be fetched separately. This function:
      1. Extracts repo + pr from the webhook payload
      2. Calls ``fetch_pr_files`` for patches
      3. Optionally fetches file content at head SHA (when ``fetch_content=True``)
      4. Returns a dict ready to feed to ``normalize_github_pr_payload``
    """
    repository = envelope_payload.get("repository") or {}
    pull_request = envelope_payload.get("pull_request") or {}
    repo_full_name = repository.get("full_name") or ""
    pr_number = pull_request.get("number")

    if not repo_full_name or pr_number is None:
        raise GitHubFetchError(
            f"webhook payload missing repository.full_name or pull_request.number: "
            f"repo={repo_full_name!r} pr={pr_number!r}"
        )

    changed_files = fetch_pr_files(
        repo_full_name, int(pr_number), token, api_base_url=api_base_url
    )

    # Fetch the bodies of any issues the PR closes / references. The
    # `intent-extractor` agent reads these as additional intent text so
    # acceptance criteria spelled out in the issue (but not duplicated in
    # the PR body) still influence the analysis.
    linked_issues: list[dict[str, Any]] = []
    pr_body = str(pull_request.get("body") or "")
    if pr_body:
        linked_issues = fetch_linked_issues(
            repo_full_name, pr_body, token, api_base_url=api_base_url
        )
        # Attach as a top-level field on `pull_request` so the existing
        # normaliser carries it through into `pull_request.linked_issues`.
        pull_request = {**pull_request, "linked_issues": linked_issues}

    if fetch_content:
        head_sha = (
            pull_request.get("head", {}).get("sha")
            if isinstance(pull_request.get("head"), dict)
            else pull_request.get("head_sha")
        )
        if head_sha:
            for file in changed_files:
                if file.get("status") == "removed":
                    continue
                file["content"] = fetch_file_content(
                    repo_full_name, file["path"], head_sha, token, api_base_url=api_base_url
                )

    return {
        "repository": repository,
        "pull_request": pull_request,
        "changed_files": changed_files,
        "settings": envelope_payload.get("settings", {}),
        "source": {"kind": "github-webhook"},
    }
