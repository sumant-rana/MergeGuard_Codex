"""Historical-signal aggregation over normalized PR/PR-file records.

The signals are intentionally simple and deterministic so the agent can
run them in linear time over the indexed history (and we can reason
about them without an LLM). They feed the downstream ``review-compression``
hybrid triage agent's hotspot/co-change heuristics.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, UTC
from itertools import combinations
from typing import Any

_BUG_LABELS = {"bug", "regression", "incident", "hotfix", "p0", "p1", "sev1", "sev2"}
_TOP_N = 20
_TOP_HOTSPOTS = 15
_TOP_PAIRS = 25
_HOTSPOT_THRESHOLD = 20
_PROJECT_KEY_RE = re.compile(r"^([A-Z][A-Z0-9]{1,9})-\d+$")


def compute_history_signals(
    repo_key: str,
    prs: list[dict[str, Any]],
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the ``repo_history_signals`` document for one repository.

    Each list is independently capped (``_TOP_*``) so the persisted
    document stays small even when a repo has tens of thousands of PRs.
    """
    return {
        "repo_key": repo_key,
        "frequently_changed_files": _frequently_changed(files),
        "files_changed_together": _files_changed_together(files),
        "hotspot_paths": _hotspot_paths(files, prs),
        "owner_activity": _owner_activity(prs, files),
        "review_latency_by_area": _review_latency_by_area(prs, files),
        "jira_key_frequency": _jira_key_frequency(prs),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _frequently_changed(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for record in files:
        path = record.get("path")
        if path:
            counter[path] += 1
    return [{"path": path, "count": count} for path, count in counter.most_common(_TOP_N)]


def _files_changed_together(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_pr: dict[tuple[str, int], set[str]] = {}
    for record in files:
        repo = record.get("repo_key")
        number = record.get("pr_number")
        path = record.get("path")
        if not repo or number is None or not path:
            continue
        by_pr.setdefault((repo, int(number)), set()).add(path)

    pair_counts: Counter[tuple[str, str]] = Counter()
    for paths in by_pr.values():
        if len(paths) < 2:
            continue
        for pair in combinations(sorted(paths), 2):
            pair_counts[pair] += 1

    result: list[dict[str, Any]] = []
    for (path_a, path_b), count in pair_counts.most_common(_TOP_PAIRS):
        if count < 2:
            # Pairs that only appear once give no real co-change signal.
            continue
        result.append({"paths": [path_a, path_b], "count": count})
    return result


def _hotspot_paths(
    files: list[dict[str, Any]],
    prs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    pr_label_index: dict[tuple[str, int], list[str]] = {}
    for pr in prs:
        repo = pr.get("repo_key")
        number = pr.get("pr_number")
        if not repo or number is None:
            continue
        pr_label_index[(repo, int(number))] = [
            str(label).lower() for label in pr.get("labels") or []
        ]

    frequency: Counter[str] = Counter()
    bug_hits: Counter[str] = Counter()
    jira_hits: Counter[str] = Counter()
    for record in files:
        path = record.get("path")
        if not path:
            continue
        frequency[path] += 1
        labels_from_file = [str(label).lower() for label in record.get("labels") or []]
        labels_from_pr = pr_label_index.get(
            (record.get("repo_key", ""), int(record.get("pr_number") or 0)),
            [],
        )
        combined = set(labels_from_file) | set(labels_from_pr)
        if combined & _BUG_LABELS:
            bug_hits[path] += 1
        if record.get("linked_jira_keys"):
            jira_hits[path] += 1

    hotspots: list[dict[str, Any]] = []
    for path, count in frequency.items():
        score = min(100, count * 10 + bug_hits[path] * 15 + jira_hits[path] * 4)
        if score < _HOTSPOT_THRESHOLD:
            continue
        reasons: list[str] = []
        if count >= 3:
            reasons.append(f"changed {count} times")
        if bug_hits[path]:
            reasons.append(f"bug labels on {bug_hits[path]} PRs")
        if jira_hits[path]:
            reasons.append(f"linked Jira keys on {jira_hits[path]} PRs")
        hotspots.append({"path": path, "score": score, "reasons": reasons})
    hotspots.sort(key=lambda entry: entry["score"], reverse=True)
    return hotspots[:_TOP_HOTSPOTS]


def _owner_activity(
    prs: list[dict[str, Any]],
    files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    pr_counts: Counter[str] = Counter()
    pr_files: dict[str, list[tuple[str, int]]] = {}
    for pr in prs:
        author = pr.get("author") or ""
        if not author:
            continue
        pr_counts[author] += 1
        pr_files.setdefault(author, []).append(
            (pr.get("repo_key", ""), int(pr.get("pr_number") or 0))
        )

    file_counts: dict[str, int] = {}
    for owner, pr_keys in pr_files.items():
        pr_set = set(pr_keys)
        file_counts[owner] = sum(
            1
            for record in files
            if (record.get("repo_key", ""), int(record.get("pr_number") or 0)) in pr_set
        )

    result: list[dict[str, Any]] = []
    for owner, pr_count in pr_counts.most_common(_TOP_N):
        result.append(
            {
                "owner": owner,
                "pr_count": pr_count,
                "file_count": file_counts.get(owner, 0),
            }
        )
    return result


def _review_latency_by_area(
    prs: list[dict[str, Any]],
    files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Approximate per-area review latency in days.

    ``area`` = first path component (``src``, ``tests``, ``docs``, ...). We
    pair each PR's created→merged interval with every changed path's area;
    the resulting average is a rough hint for downstream triage, not a
    bookkeeping metric.
    """
    pr_lookup: dict[tuple[str, int], dict[str, Any]] = {
        (pr.get("repo_key", ""), int(pr.get("pr_number") or 0)): pr for pr in prs
    }
    durations_by_area: dict[str, list[float]] = {}
    for record in files:
        pr = pr_lookup.get(
            (record.get("repo_key", ""), int(record.get("pr_number") or 0))
        )
        if not pr:
            continue
        merged_at = pr.get("merged_at")
        created_at = pr.get("created_at")
        if not merged_at or not created_at:
            continue
        try:
            t1 = datetime.fromisoformat(str(merged_at).replace("Z", "+00:00"))
            t0 = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        duration_days = max((t1 - t0).total_seconds() / 86400.0, 0.0)
        path = record.get("path") or ""
        area = path.split("/", 1)[0] if "/" in path else path or "(root)"
        durations_by_area.setdefault(area, []).append(duration_days)

    result: list[dict[str, Any]] = []
    for area, samples in durations_by_area.items():
        if not samples:
            continue
        avg = sum(samples) / len(samples)
        result.append(
            {
                "area": area,
                "sample_size": len(samples),
                "avg_latency_days": round(avg, 2),
            }
        )
    result.sort(key=lambda entry: entry["avg_latency_days"], reverse=True)
    return result[:_TOP_N]


def _jira_key_frequency(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    project_counts: Counter[str] = Counter()
    for pr in prs:
        seen_projects: set[str] = set()
        for key in pr.get("linked_jira_keys") or []:
            match = _PROJECT_KEY_RE.match(str(key))
            if not match:
                continue
            seen_projects.add(match.group(1))
        for project in seen_projects:
            project_counts[project] += 1
    return [
        {"project": project, "count": count}
        for project, count in project_counts.most_common(_TOP_N)
    ]
