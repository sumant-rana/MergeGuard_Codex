"""GitHub REST PR-history collector for the ``pr-history-indexer`` agent.

The collector is decoupled from the network: it takes a ``transport``
callable ``(url, token) -> json`` so unit tests can stub it. The default
transport reuses :func:`packages.github_pr.pr_fetcher._get_json`.

Why REST and not the ``gh`` CLI:

- The agent runs inside containers (``local`` = docker, ``cloud`` =
  Magenta workspace). Neither container ships ``gh``.
- The existing PR-ingest path already uses REST via
  ``packages/github_pr/pr_fetcher.py``; staying on REST keeps the auth and
  rate-limit story consistent.
- ``gh`` stays as a laptop convenience for onboarding CLI ergonomics
  (e.g. ``scripts/mergeguard_pr.py``), not as the agent runtime backend.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable

from .pr_fetcher import GITHUB_API, _get_json

JIRA_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")

# Extension → language. Kept short on purpose; the goal is to mark
# obviously test/doc-heavy files for downstream signal aggregation,
# not to build a complete language classifier.
LANGUAGE_BY_EXT: dict[str, str] = {
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".py": "python",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".scala": "scala",
    ".rb": "ruby",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".c": "c",
    ".h": "c",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".sh": "shell",
}

DEFAULT_PER_PAGE = 100
DEFAULT_MAX_PRS = 500
DEFAULT_MAX_FILES_PER_PR = 100
DEFAULT_MAX_BODY_BYTES = 16 * 1024

Transport = Callable[[str, str], Any]


def _default_transport(url: str, token: str) -> Any:
    return _get_json(url, token)


def extract_jira_keys(text: str) -> list[str]:
    """Return Jira-style keys in first-seen order, de-duplicated.

    Conservative regex: ``[A-Z][A-Z0-9]{1,9}-\\d+``. We do not validate the
    project prefix against a list because onboarding may run before the Jira
    agent has imported any project metadata; downstream consumers treat the
    keys as hints, not authoritative links.
    """
    if not text:
        return []
    seen: set[str] = set()
    keys: list[str] = []
    for match in JIRA_KEY_RE.finditer(text):
        key = match.group(0)
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def tokenize_path(path: str) -> list[str]:
    """Lowercase tokens used for path-based co-change matching downstream."""
    if not path:
        return []
    cleaned = path.lower()
    tokens: list[str] = []
    current = ""
    for char in cleaned:
        if char.isalnum() or char == "_":
            current += char
        else:
            if current:
                tokens.append(current)
            current = ""
    if current:
        tokens.append(current)
    return [t for t in tokens if t]


def _language_for(path: str) -> str:
    lowered = path.lower()
    for ext, lang in LANGUAGE_BY_EXT.items():
        if lowered.endswith(ext):
            return lang
    return "other"


def _labels_of(raw: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for entry in raw.get("labels") or []:
        if isinstance(entry, dict) and entry.get("name"):
            labels.append(str(entry["name"]))
        elif isinstance(entry, str):
            labels.append(entry)
    return labels


def _reviewers_of(raw: dict[str, Any]) -> list[str]:
    reviewers: list[str] = []
    for entry in raw.get("requested_reviewers") or []:
        if isinstance(entry, dict) and entry.get("login"):
            reviewers.append(str(entry["login"]))
    return reviewers


def _truncate_body(body: str | None, max_bytes: int) -> str:
    if not body:
        return ""
    if len(body) <= max_bytes:
        return body
    return body[:max_bytes] + "\n...[truncated]"


def _resolve_state(raw: dict[str, Any]) -> str:
    """Treat any PR with a merged_at timestamp as ``merged``.

    GitHub returns ``state`` ∈ {open, closed}; merged PRs are ``closed``
    with a non-null ``merged_at``. The downstream signal aggregation
    cares about merged-ness, so we normalize here once.
    """
    if raw.get("merged_at"):
        return "merged"
    state = raw.get("state")
    return str(state) if state else "open"


def normalize_pr(repo_key: str, raw: dict[str, Any]) -> dict[str, Any]:
    body = _truncate_body(raw.get("body"), DEFAULT_MAX_BODY_BYTES)
    state = _resolve_state(raw)
    labels = _labels_of(raw)
    jira_keys = extract_jira_keys(f"{raw.get('title') or ''}\n{body}")
    user = raw.get("user") or {}
    return {
        "repo_key": repo_key,
        "pr_number": int(raw.get("number") or 0),
        "title": str(raw.get("title") or ""),
        "body": body,
        "state": state,
        "merged_at": raw.get("merged_at") or "",
        "closed_at": raw.get("closed_at") or "",
        "created_at": raw.get("created_at") or "",
        "author": str(user.get("login") or ""),
        "labels": labels,
        "linked_jira_keys": jira_keys,
        "reviewers": _reviewers_of(raw),
        "html_url": str(raw.get("html_url") or ""),
        "source": "github",
    }


def normalize_pr_file(
    repo_key: str,
    pr_number: int,
    raw: dict[str, Any],
    *,
    labels: list[str] | None = None,
    linked_jira_keys: list[str] | None = None,
) -> dict[str, Any]:
    path = str(raw.get("filename") or raw.get("path") or "")
    additions = int(raw.get("additions") or 0)
    deletions = int(raw.get("deletions") or 0)
    return {
        "repo_key": repo_key,
        "pr_number": int(pr_number),
        "path": path,
        "status": str(raw.get("status") or "modified"),
        "additions": additions,
        "deletions": deletions,
        "change_size": additions + deletions,
        "language": _language_for(path),
        "path_tokens": tokenize_path(path),
        "labels": list(labels or []),
        "linked_jira_keys": list(linked_jira_keys or []),
    }


def _is_before(timestamp: str | None, cutoff: datetime) -> bool:
    """Return True if a non-empty GitHub timestamp falls strictly before cutoff."""
    if not timestamp:
        return False
    try:
        normalized = timestamp.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized) < cutoff
    except ValueError:
        return False


def _meets_since_window(raw: dict[str, Any], since: datetime | None) -> bool:
    """A PR passes the ``since`` filter if it was last-active on or after ``since``.

    We consult ``merged_at``, then ``closed_at``, then ``updated_at``, then
    ``created_at`` and decide on the first available timestamp. If every
    timestamp is missing we treat the PR as out of window — when the caller
    has explicitly asked for ``since``, lack of evidence is a rejection so
    we never silently broaden the scan.
    """
    if since is None:
        return True
    for key in ("merged_at", "closed_at", "updated_at", "created_at"):
        ts = raw.get(key)
        if not ts:
            continue
        try:
            normalized = ts.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized) >= since
        except ValueError:
            continue
    return False


def _parse_since(since: str | None) -> datetime | None:
    if not since:
        return None
    try:
        return datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        return None


def collect_pr_history(
    *,
    repo_full_name: str,
    token: str,
    transport: Transport | None = None,
    max_prs: int = DEFAULT_MAX_PRS,
    include_files: bool = True,
    states: list[str] | None = None,
    since: str | None = None,
    api_base_url: str = GITHUB_API,
    per_page: int = DEFAULT_PER_PAGE,
    max_files_per_pr: int = DEFAULT_MAX_FILES_PER_PR,
) -> dict[str, Any]:
    """Walk historical PRs for a repo and return normalized records + scan summary.

    Pagination follows the standard ``per_page`` + ``page`` REST pattern.
    The walker stops as soon as ``max_prs`` PRs have been accepted or a
    short page (``< per_page`` results) is returned. Files are fetched
    per PR only when ``include_files=True`` (the largest cost driver).
    """
    repo_key = repo_full_name
    request = transport or _default_transport
    allowed_states = {state.lower() for state in (states or ["merged", "closed"])}
    since_dt = _parse_since(since)
    state_param = "all"  # filter ourselves so we see merged PRs too

    prs: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    warnings: list[str] = []
    prs_seen = 0
    page = 1

    while True:
        url = (
            f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/pulls"
            f"?state={state_param}&per_page={per_page}&page={page}&sort=updated&direction=desc"
        )
        try:
            page_data = request(url, token)
        except Exception as exc:
            warnings.append(f"pulls page {page} failed: {type(exc).__name__}: {exc}")
            break
        if not isinstance(page_data, list) or not page_data:
            break
        for raw in page_data:
            prs_seen += 1
            # Always count what we see (drives prs_skipped in the summary)
            # but only index up to max_prs. We still walk the rest of the
            # current page so the seen count reflects how aggressive the
            # cap was relative to what GitHub returned in this batch.
            if len(prs) >= max_prs:
                continue
            normalized = normalize_pr(repo_key, raw)
            if normalized["state"] not in allowed_states:
                continue
            if not _meets_since_window(raw, since_dt):
                continue
            pr_number = normalized["pr_number"]
            if include_files:
                file_records = _collect_files_for_pr(
                    repo_full_name=repo_full_name,
                    repo_key=repo_key,
                    pr_number=pr_number,
                    token=token,
                    request=request,
                    api_base_url=api_base_url,
                    per_page=per_page,
                    max_files=max_files_per_pr,
                    labels=normalized["labels"],
                    jira_keys=normalized["linked_jira_keys"],
                    warnings=warnings,
                )
                files.extend(file_records)
                normalized["changed_file_count"] = len(file_records)
                normalized["changed_paths"] = [
                    record["path"] for record in file_records if record.get("path")
                ]
            else:
                normalized.setdefault("changed_file_count", 0)
                normalized.setdefault("changed_paths", [])
            prs.append(normalized)
        if len(prs) >= max_prs:
            break
        if len(page_data) < per_page:
            break
        page += 1

    return {
        "prs": prs,
        "files": files,
        "warnings": warnings,
        "scan_summary": {
            "prs_seen": prs_seen,
            "prs_indexed": len(prs),
            "prs_skipped": max(prs_seen - len(prs), 0),
            "files_indexed": len(files),
        },
    }


def _collect_files_for_pr(
    *,
    repo_full_name: str,
    repo_key: str,
    pr_number: int,
    token: str,
    request: Transport,
    api_base_url: str,
    per_page: int,
    max_files: int,
    labels: list[str],
    jira_keys: list[str],
    warnings: list[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page = 1
    while len(out) < max_files:
        url = (
            f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/pulls/{pr_number}/files"
            f"?per_page={per_page}&page={page}"
        )
        try:
            page_data = request(url, token)
        except Exception as exc:
            warnings.append(
                f"files for PR #{pr_number} page {page} failed: {type(exc).__name__}: {exc}"
            )
            break
        if not isinstance(page_data, list) or not page_data:
            break
        for raw in page_data:
            out.append(
                normalize_pr_file(
                    repo_key,
                    pr_number,
                    raw,
                    labels=labels,
                    linked_jira_keys=jira_keys,
                )
            )
            if len(out) >= max_files:
                break
        if len(page_data) < per_page:
            break
        page += 1
    return out
