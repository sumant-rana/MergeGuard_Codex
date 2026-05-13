"""Fetch full PR details (file patches, optional file content) from GitHub REST.

The webhook envelope already contains a ``pull_request`` object with title,
body, base/head SHAs, etc. — but the patches and file content live in
separate endpoints. This module fills in the gaps so the orchestrator sees
the same shape it would from a fixture.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

GITHUB_API = "https://api.github.com"
DEFAULT_MAX_FILES = 100
DEFAULT_MAX_CONTENT_BYTES = 256 * 1024  # 256 KiB cap per file


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
