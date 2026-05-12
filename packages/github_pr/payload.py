from __future__ import annotations

import copy
import re
from typing import Any


ISSUE_REF_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+"
    r"(?:(?:https://github\.com/[^/\s]+/[^/\s]+/issues/)|#)(\d+)",
    re.IGNORECASE,
)


def normalize_github_pr_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a real GitHub PR payload into the MergeGuard agent envelope shape."""

    payload = copy.deepcopy(raw.get("payload", raw))
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")

    repository = _normalize_repository(payload.get("repository"))
    pull_request = _normalize_pull_request(payload.get("pull_request"))
    changed_files = _normalize_changed_files(payload.get("changed_files") or payload.get("files"))
    settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}

    if payload.get("codeowners") and not settings.get("codeowners"):
        settings["codeowners"] = str(payload["codeowners"])

    pull_request["analysis_context"] = _analysis_context(pull_request)

    return {
        "repository": repository,
        "pull_request": pull_request,
        "changed_files": changed_files,
        "settings": settings,
        "source": payload.get("source", {"kind": "github-pr-local"}),
    }


def _normalize_repository(repository: Any) -> dict[str, Any]:
    if not isinstance(repository, dict):
        raise ValueError("repository is required")

    full_name = str(repository.get("full_name") or "")
    owner = str(repository.get("owner") or "")
    name = str(repository.get("name") or "")

    if full_name and (not owner or not name) and "/" in full_name:
        owner, name = full_name.split("/", 1)
    if not full_name and owner and name:
        full_name = f"{owner}/{name}"
    if not owner or not name:
        raise ValueError("repository.owner and repository.name are required")

    return {
        **repository,
        "owner": owner,
        "name": name,
        "full_name": full_name,
        "default_branch": repository.get("default_branch") or "main",
    }


def _normalize_pull_request(pr: Any) -> dict[str, Any]:
    if not isinstance(pr, dict):
        raise ValueError("pull_request is required")
    if pr.get("number") is None:
        raise ValueError("pull_request.number is required")

    body = str(pr.get("body") or "")
    issue_refs = _normalize_issue_refs(pr.get("issue_refs") or pr.get("closing_issues") or [])
    issue_refs.extend(_issue_refs_from_body(body, existing={item["number"] for item in issue_refs}))

    commit_history = _normalize_commit_history(pr.get("commit_history") or pr.get("commits") or [])

    return {
        **pr,
        "number": int(pr["number"]),
        "title": str(pr.get("title") or f"PR #{pr['number']}"),
        "body": body,
        "author": _author_name(pr.get("author")),
        "base_sha": str(pr.get("base_sha") or pr.get("base_ref_oid") or "unknown-base"),
        "head_sha": str(pr.get("head_sha") or pr.get("head_ref_oid") or "unknown-head"),
        "base_ref": pr.get("base_ref") or pr.get("baseRefName") or "",
        "head_ref": pr.get("head_ref") or pr.get("headRefName") or "",
        "labels": _normalize_labels(pr.get("labels") or []),
        "issue_refs": issue_refs,
        "commit_history": commit_history,
    }


def _normalize_changed_files(files: Any) -> list[dict[str, Any]]:
    if not isinstance(files, list):
        raise ValueError("changed_files must be a list")

    changed_files: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("filename") or "")
        if not path:
            continue
        additions = int(item.get("additions") or 0)
        deletions = int(item.get("deletions") or 0)
        changes = int(item.get("changes") or additions + deletions)
        changed_files.append(
            {
                **item,
                "path": path,
                "status": str(item.get("status") or "modified"),
                "additions": additions,
                "deletions": deletions,
                "changes": changes,
                "patch": str(item.get("patch") or ""),
                "content": str(item.get("content") or ""),
            }
        )

    if not changed_files:
        raise ValueError("at least one changed file is required")
    return changed_files


def _normalize_issue_refs(issue_refs: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for issue in issue_refs:
        if isinstance(issue, int):
            normalized.append({"number": issue})
        elif isinstance(issue, str) and issue.lstrip("#").isdigit():
            normalized.append({"number": int(issue.lstrip("#"))})
        elif isinstance(issue, dict) and issue.get("number") is not None:
            normalized.append(
                {
                    "number": int(issue["number"]),
                    "title": str(issue.get("title") or ""),
                    "state": str(issue.get("state") or ""),
                    "url": str(issue.get("url") or ""),
                }
            )
    return normalized


def _issue_refs_from_body(body: str, *, existing: set[int]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for match in ISSUE_REF_RE.finditer(body):
        number = int(match.group(1))
        if number not in existing:
            refs.append({"number": number})
            existing.add(number)
    return refs


def _normalize_commit_history(commits: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for commit in commits[:50]:
        if isinstance(commit, str):
            normalized.append({"message": commit})
        elif isinstance(commit, dict):
            normalized.append(
                {
                    "oid": str(commit.get("oid") or commit.get("sha") or ""),
                    "message": str(
                        commit.get("message")
                        or commit.get("messageHeadline")
                        or commit.get("headline")
                        or ""
                    ),
                    "body": str(commit.get("messageBody") or commit.get("body") or ""),
                    "authored_date": str(commit.get("authoredDate") or ""),
                    "authors": commit.get("authors") or [],
                }
            )
    return [commit for commit in normalized if commit.get("message") or commit.get("oid")]


def _normalize_labels(labels: list[Any]) -> list[str]:
    names = []
    for label in labels:
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, dict) and label.get("name"):
            names.append(str(label["name"]))
    return names


def _analysis_context(pr: dict[str, Any]) -> str:
    lines: list[str] = []
    if pr.get("analysis_context"):
        lines.append(str(pr["analysis_context"]))

    if pr.get("issue_refs"):
        lines.append("Linked issues solved:")
        for issue in pr["issue_refs"][:20]:
            label = f"#{issue.get('number')}"
            title = issue.get("title")
            state = issue.get("state")
            parts = [label, title, f"({state})" if state else ""]
            lines.append(" ".join(str(part) for part in parts if part))

    if pr.get("commit_history"):
        lines.append("Commit history:")
        for commit in pr["commit_history"][:25]:
            oid = str(commit.get("oid") or "")[:12]
            message = commit.get("message") or ""
            body = commit.get("body") or ""
            lines.append(" ".join(part for part in [oid, message, body] if part))

    return "\n".join(lines)


def _author_name(author: Any) -> str:
    if isinstance(author, dict):
        return str(author.get("login") or author.get("name") or "unknown")
    return str(author or "unknown")
