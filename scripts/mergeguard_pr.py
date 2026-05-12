#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_API_URL = os.environ.get("MERGEGUARD_API_URL", "http://127.0.0.1:4100")
MAX_CONTENT_BYTES = 128_000
GH_CANDIDATE_PATHS = [
    os.environ.get("GH_BIN", ""),
    "/opt/homebrew/bin/gh",
    "/usr/local/bin/gh",
    "/usr/bin/gh",
]
PR_VIEW_FIELDS = [
    "number",
    "title",
    "body",
    "author",
    "baseRefName",
    "baseRefOid",
    "headRefName",
    "headRefOid",
    "url",
    "labels",
    "commits",
    "files",
    "closingIssuesReferences",
]
PR_VIEW_FALLBACK_FIELDS = [field for field in PR_VIEW_FIELDS if field != "closingIssuesReferences"]


class CommandError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or inspect a GitHub PR, then send it to local MergeGuard."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze", help="Analyze an existing PR")
    add_common_args(analyze_parser)
    analyze_parser.add_argument(
        "--pr",
        help="PR number, URL, or branch. Defaults to the PR for the current branch.",
    )

    create_parser = subparsers.add_parser("create", help="Create a PR, then analyze it")
    add_common_args(create_parser)
    create_parser.add_argument("--title", help="PR title")
    create_parser.add_argument("--body", help="PR body")
    create_parser.add_argument("--body-file", help="Read PR body from file")
    create_parser.add_argument("--base", help="Base branch")
    create_parser.add_argument("--head", help="Head branch")
    create_parser.add_argument("--draft", action="store_true", help="Create a draft PR")
    create_parser.add_argument("--fill", action="store_true", help="Use gh's commit-derived title/body")
    create_parser.add_argument("--label", action="append", default=[], help="Label to apply")
    create_parser.add_argument("--reviewer", action="append", default=[], help="Reviewer to request")
    create_parser.add_argument("--assignee", action="append", default=[], help="Assignee to add")

    args = parser.parse_args()
    repo = Path(args.repo).expanduser().resolve()
    try:
        if args.command == "create":
            selector = create_pr(args, repo)
            payload = collect_pr_payload(repo, selector=selector, created_by_cli=True)
        else:
            payload = collect_pr_payload(repo, selector=args.pr, created_by_cli=False)

        if args.payload_out:
            Path(args.payload_out).write_text(json.dumps(payload, indent=2) + "\n")
        if args.print_payload:
            print(json.dumps(payload, indent=2))

        if args.no_post:
            return 0

        response = post_to_mergeguard(args.api_url, payload)
        if args.json:
            print(json.dumps(response, indent=2))
        else:
            print_run_summary(response, args.api_url)
        return 0 if response.get("run", {}).get("state") != "failed" else 2
    except CommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo",
        default=".",
        help="Target repository path. Defaults to the current directory.",
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help=f"MergeGuard API URL. Defaults to {DEFAULT_API_URL}.",
    )
    parser.add_argument("--payload-out", help="Write collected PR payload to this file")
    parser.add_argument("--print-payload", action="store_true", help="Print collected PR payload")
    parser.add_argument("--no-post", action="store_true", help="Collect payload but do not call API")
    parser.add_argument("--json", action="store_true", help="Print API response as JSON")


def create_pr(args: argparse.Namespace, repo: Path) -> str | None:
    assert_create_branch_is_valid(args, repo)

    cmd = ["gh", "pr", "create"]
    if args.fill or not (args.title or args.body or args.body_file):
        cmd.append("--fill")
    if args.title:
        cmd += ["--title", args.title]
    if args.body:
        cmd += ["--body", args.body]
    if args.body_file:
        cmd += ["--body-file", args.body_file]
    if args.base:
        cmd += ["--base", args.base]
    if args.head:
        cmd += ["--head", args.head]
    if args.draft:
        cmd.append("--draft")
    for label in args.label:
        cmd += ["--label", label]
    for reviewer in args.reviewer:
        cmd += ["--reviewer", reviewer]
    for assignee in args.assignee:
        cmd += ["--assignee", assignee]

    output = run_text(cmd, repo).strip()
    selector = extract_pr_selector(output)
    if output:
        print(output)
    return selector


def assert_create_branch_is_valid(args: argparse.Namespace, repo: Path) -> None:
    if not args.base:
        return

    head = normalize_branch_name(args.head) if args.head else current_branch(repo)
    base = normalize_branch_name(args.base)
    if head and base and head == base:
        raise CommandError(
            "cannot create a PR because the head branch is the same as the base branch "
            f"({head!r}).\n"
            "Create and push a feature branch first, then rerun this command:\n"
            "  cd /path/to/target-repo\n"
            "  git switch -c mergeguard-demo\n"
            "  # make a change, then commit it\n"
            "  git push -u origin mergeguard-demo\n"
            "Then run mergeguard_pr.py create again, or pass --head mergeguard-demo."
        )


def current_branch(repo: Path) -> str:
    branch = run_text(["git", "branch", "--show-current"], repo).strip()
    if branch:
        return branch
    branch = run_text(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo).strip()
    return "" if branch == "HEAD" else branch


def normalize_branch_name(value: str | None) -> str:
    if not value:
        return ""
    branch = value.rsplit(":", 1)[-1]
    return branch.removeprefix("refs/heads/")


def collect_pr_payload(repo: Path, *, selector: str | None, created_by_cli: bool) -> dict[str, Any]:
    ensure_repo(repo)
    repo_info = gh_json(["gh", "repo", "view", "--json", "name,owner,defaultBranchRef,url"], repo)
    pr_info = gh_pr_view(repo, selector)
    patch_by_path = parse_diff_patches(gh_pr_diff(repo, selector))
    files = normalize_files(repo, pr_info.get("files") or [], patch_by_path)
    codeowners = read_codeowners(repo)

    repository = {
        "owner": nested(repo_info, "owner", "login") or nested(repo_info, "owner", "name"),
        "name": repo_info.get("name"),
        "full_name": "/".join(
            part
            for part in [
                nested(repo_info, "owner", "login") or nested(repo_info, "owner", "name"),
                repo_info.get("name"),
            ]
            if part
        ),
        "default_branch": nested(repo_info, "defaultBranchRef", "name") or "main",
        "url": repo_info.get("url", ""),
    }
    author = pr_info.get("author") or {}
    pull_request = {
        "number": pr_info["number"],
        "title": pr_info.get("title") or f"PR #{pr_info['number']}",
        "body": pr_info.get("body") or "",
        "author": author.get("login") or author.get("name") or "unknown",
        "base_ref": pr_info.get("baseRefName") or "",
        "head_ref": pr_info.get("headRefName") or "",
        "base_sha": pr_info.get("baseRefOid") or "",
        "head_sha": pr_info.get("headRefOid") or "",
        "url": pr_info.get("url") or "",
        "labels": [label.get("name") for label in pr_info.get("labels", []) if label.get("name")],
        "issue_refs": normalize_issues(pr_info),
        "commit_history": normalize_commits(pr_info.get("commits") or []),
        "source": {"collector": "scripts/mergeguard_pr.py", "created_by_cli": created_by_cli},
    }

    return {
        "repository": repository,
        "pull_request": pull_request,
        "changed_files": files,
        "settings": {
            "codeowners": codeowners,
            "linked_docs": [],
            "prompt_suites": [],
            "contracts": [],
        },
        "source": {"kind": "github-pr-local", "created_by_cli": created_by_cli},
    }


def ensure_repo(repo: Path) -> None:
    if not repo.exists():
        raise CommandError(f"repository path does not exist: {repo}")
    run_text(["git", "rev-parse", "--show-toplevel"], repo)


def gh_pr_view(repo: Path, selector: str | None) -> dict[str, Any]:
    fields = ",".join(PR_VIEW_FIELDS)
    try:
        return gh_json(["gh", "pr", "view", *selector_arg(selector), "--json", fields], repo)
    except CommandError:
        fallback = ",".join(PR_VIEW_FALLBACK_FIELDS)
        return gh_json(["gh", "pr", "view", *selector_arg(selector), "--json", fallback], repo)


def gh_pr_diff(repo: Path, selector: str | None) -> str:
    cmd = ["gh", "pr", "diff", *selector_arg(selector), "--patch", "--color=never"]
    try:
        return run_text(cmd, repo)
    except CommandError:
        return run_text(["gh", "pr", "diff", *selector_arg(selector), "--patch"], repo)


def normalize_files(
    repo: Path,
    gh_files: list[dict[str, Any]],
    patch_by_path: dict[str, str],
) -> list[dict[str, Any]]:
    files_by_path: dict[str, dict[str, Any]] = {}
    for item in gh_files:
        path = item.get("path")
        if not path:
            continue
        additions = int(item.get("additions") or 0)
        deletions = int(item.get("deletions") or 0)
        files_by_path[path] = {
            "path": path,
            "status": item.get("status") or "modified",
            "additions": additions,
            "deletions": deletions,
            "changes": additions + deletions,
            "patch": patch_by_path.get(path, ""),
            "content": read_current_content(repo, path),
        }

    for path, patch in patch_by_path.items():
        files_by_path.setdefault(
            path,
            {
                "path": path,
                "status": "modified",
                "additions": count_prefixed_lines(patch, "+"),
                "deletions": count_prefixed_lines(patch, "-"),
                "changes": count_prefixed_lines(patch, "+") + count_prefixed_lines(patch, "-"),
                "patch": patch,
                "content": read_current_content(repo, path),
            },
        )

    return sorted(files_by_path.values(), key=lambda item: item["path"])


def parse_diff_patches(diff_text: str) -> dict[str, str]:
    patches: dict[str, str] = {}
    current_lines: list[str] = []
    current_path = ""
    old_path = ""

    def flush() -> None:
        if current_path and current_lines:
            patches[current_path] = "\n".join(current_lines)

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush()
            current_lines = [line]
            match = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            old_path = match.group(1) if match else ""
            current_path = match.group(2) if match else ""
            continue

        if not current_lines:
            continue

        current_lines.append(line)
        if line.startswith("--- a/"):
            old_path = line[6:].strip()
        elif line.startswith("+++ b/"):
            current_path = line[6:].strip()
        elif line == "+++ /dev/null" and old_path:
            current_path = old_path

    flush()
    return patches


def normalize_issues(pr_info: dict[str, Any]) -> list[dict[str, Any]]:
    issues = []
    for issue in pr_info.get("closingIssuesReferences") or []:
        if issue.get("number") is not None:
            issues.append(
                {
                    "number": int(issue["number"]),
                    "title": issue.get("title") or "",
                    "state": issue.get("state") or "",
                    "url": issue.get("url") or "",
                }
            )
    existing = {issue["number"] for issue in issues}
    for number in issue_numbers_from_body(pr_info.get("body") or ""):
        if number not in existing:
            issues.append({"number": number, "title": "", "state": "", "url": ""})
            existing.add(number)
    return issues


def normalize_commits(commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for commit in commits:
        normalized.append(
            {
                "oid": commit.get("oid") or "",
                "message": commit.get("messageHeadline") or commit.get("message") or "",
                "body": commit.get("messageBody") or "",
                "authored_date": commit.get("authoredDate") or "",
                "authors": commit.get("authors") or [],
            }
        )
    return normalized


def issue_numbers_from_body(body: str) -> list[int]:
    pattern = re.compile(
        r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+"
        r"(?:(?:https://github\.com/[^/\s]+/[^/\s]+/issues/)|#)(\d+)",
        re.IGNORECASE,
    )
    return [int(match.group(1)) for match in pattern.finditer(body)]


def read_codeowners(repo: Path) -> str:
    for relative in [".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"]:
        path = repo / relative
        if path.exists() and path.is_file():
            return path.read_text(errors="replace")
    return ""


def read_current_content(repo: Path, relative_path: str) -> str:
    path = (repo / relative_path).resolve()
    try:
        path.relative_to(repo.resolve())
    except ValueError:
        return ""
    if not path.exists() or not path.is_file() or path.stat().st_size > MAX_CONTENT_BYTES:
        return ""
    return path.read_text(errors="replace")


def count_prefixed_lines(patch: str, prefix: str) -> int:
    return len(
        [
            line
            for line in patch.splitlines()
            if line.startswith(prefix)
            and not line.startswith(f"{prefix}{prefix}{prefix}")
        ]
    )


def selector_arg(selector: str | None) -> list[str]:
    return [selector] if selector else []


def extract_pr_selector(output: str) -> str | None:
    match = re.search(r"https://github\.com/[^\s]+/pull/\d+", output)
    if match:
        return match.group(0)
    match = re.search(r"(?:pull request|PR)\s+#?(\d+)", output, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def post_to_mergeguard(api_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    endpoint = api_url.rstrip("/") + "/api/github/pr/analyze"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise CommandError(f"MergeGuard API returned HTTP {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise CommandError(f"cannot reach MergeGuard API at {endpoint}: {exc}") from exc


def print_run_summary(response: dict[str, Any], api_url: str) -> None:
    run = response.get("run") or {}
    summary = run.get("summary") or {}
    print("MergeGuard processed PR")
    print(f"  run:    {run.get('id')}")
    print(f"  state:  {run.get('state')}")
    print(f"  status: {summary.get('status')}")
    print(f"  risk:   {summary.get('risk_score')}")
    print(f"  agents: {len(run.get('agent_results') or {})}")
    print(f"  url:    {api_url.rstrip('/')}")


def gh_json(cmd: list[str], cwd: Path) -> dict[str, Any]:
    return json.loads(run_text(cmd, cwd))


def run_text(cmd: list[str], cwd: Path) -> str:
    resolved_cmd = [resolve_command(cmd[0]), *cmd[1:]]
    try:
        process = subprocess.run(
            resolved_cmd,
            cwd=cwd,
            check=False,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise CommandError(command_not_found_message(cmd[0])) from exc

    if process.returncode != 0:
        stderr = process.stderr.strip()
        stdout = process.stdout.strip()
        detail = stderr or stdout or f"exit code {process.returncode}"
        raise CommandError(f"{' '.join(cmd)} failed: {detail}")
    return process.stdout


def resolve_command(command: str) -> str:
    if "/" in command:
        return command

    found = shutil.which(command)
    if found:
        return found

    if command == "gh":
        for candidate in GH_CANDIDATE_PATHS:
            if candidate and Path(candidate).exists():
                return candidate

    return command


def command_not_found_message(command: str) -> str:
    if command != "gh":
        return f"required command not found: {command}"
    return (
        "required command not found: gh. Install GitHub CLI with `brew install gh`, "
        "or set GH_BIN=/absolute/path/to/gh if it is installed outside PATH."
    )


def nested(data: dict[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
