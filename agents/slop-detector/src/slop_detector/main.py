from __future__ import annotations

import re
import sys
from pathlib import PurePosixPath, Path
from typing import Any

for _repo_root in [*Path(__file__).resolve().parents, Path("/app")]:
    if (_repo_root / "packages").is_dir():
        _repo_root_str = str(_repo_root)
        if _repo_root_str not in sys.path:
            sys.path.insert(0, _repo_root_str)
        break

from packages.agent_runtime import create_app, make_agent_result, register_entrypoint  # noqa: E402
from packages.core.analysis_utils import (  # noqa: E402
    important_terms,
    is_generated,
    is_test,
    normalize_path,
)

AGENT_ID = "slop-detector"

app = create_app(
    AGENT_ID,
    "Detect review slop such as debug leftovers, placeholders, noisy files, and weak tests.",
)


SLOP_PATH_TOKENS = {
    "scratch",
    "tmp",
    "temp",
    "debug",
    "playground",
    "experiment",
    "wip",
    "draft",
    "sample",
    "example",
}

DEBUG_PATTERNS = [
    (re.compile(r"\bconsole\.(log|debug|trace)\s*\(", re.I), "console logging"),
    (re.compile(r"\bdebugger\b", re.I), "debugger statement"),
    (re.compile(r"\bpdb\.set_trace\s*\(", re.I), "python debugger"),
    (re.compile(r"\bbreakpoint\s*\(", re.I), "breakpoint call"),
    (re.compile(r"\bprint\s*\(", re.I), "temporary print"),
    (re.compile(r"\bdump(var)?\s*\(", re.I), "debug dump"),
]

PLACEHOLDER_PATTERNS = [
    (re.compile(r"\bTODO\b.*\b(remove|cleanup|before merge|wire|real|later)\b", re.I), "todo marker"),
    (re.compile(r"\b(FIXME|XXX|HACK|WIP|DO NOT COMMIT)\b", re.I), "work-in-progress marker"),
    (re.compile(r"\b(lorem ipsum|placeholder|stubbed?|fake|dummy)\b", re.I), "placeholder content"),
    (re.compile(r"not implemented", re.I), "not implemented path"),
]

WEAK_IMPLEMENTATION_PATTERNS = [
    (re.compile(r"\breturn\s+true\s*;?", re.I), "always-true return"),
    (re.compile(r"\breturn\s+null\s*;?", re.I), "null placeholder return"),
    (re.compile(r"\breturn\s+undefined\s*;?", re.I), "undefined placeholder return"),
    (re.compile(r"\bpass\s*(#.*)?$", re.I | re.M), "empty python body"),
    (re.compile(r"\bany\b|:\s*any\b", re.I), "broad any typing"),
]

ASSERTION_PATTERNS = [
    re.compile(r"\bexpect\s*\(", re.I),
    re.compile(r"\bassert(That|Equal|True|False|Raises)?\s*\(", re.I),
    re.compile(r"\bshould\.", re.I),
    re.compile(r"\bto(Equal|Be|Contain|Have|Throw)\b", re.I),
]


@app.tool()
def inspect_file(
    file: dict[str, Any],
    raw_file: dict[str, Any] | None = None,
    intent_terms: list[str] | None = None,
) -> dict[str, Any] | None:
    """Return one consolidated slop finding for a changed file when hygiene signals are present."""
    raw_file = raw_file or {}
    path = normalize_path(file.get("path") or raw_file.get("path") or "")
    if not path:
        return None
    patch = str(raw_file.get("patch") or file.get("patch") or "")
    content = str(raw_file.get("content") or file.get("content") or "")
    additions = int(file.get("additions") or raw_file.get("additions") or 0)
    changes = int(file.get("changes") or raw_file.get("changes") or additions)
    classification = str(file.get("classification") or "")
    text = "\n".join(part for part in [path, patch, content] if part)
    added_lines = added_patch_lines(patch)

    signals: list[str] = []
    categories: list[str] = []
    score = 0

    path_tokens = set(important_terms(path.replace("/", " ")))
    if path_tokens & SLOP_PATH_TOKENS:
        signals.append(f"path looks temporary: {', '.join(sorted(path_tokens & SLOP_PATH_TOKENS))}")
        categories.append("temporary_file")
        score += 18

    debug_hits = pattern_hits(DEBUG_PATTERNS, text)
    if debug_hits:
        signals.extend(debug_hits)
        categories.append("debug_artifact")
        score += 28 + min(12, 3 * len(debug_hits))

    placeholder_hits = pattern_hits(PLACEHOLDER_PATTERNS, text)
    if placeholder_hits:
        signals.extend(placeholder_hits)
        categories.append("placeholder_work")
        score += 22 + min(10, 2 * len(placeholder_hits))

    weak_hits = pattern_hits(WEAK_IMPLEMENTATION_PATTERNS, "\n".join(added_lines))
    if weak_hits and not is_docs_like(path):
        signals.extend(weak_hits)
        categories.append("weak_implementation")
        score += 18 + min(10, 2 * len(weak_hits))

    commented_ratio = commented_added_ratio(added_lines)
    if commented_ratio >= 0.42 and len(added_lines) >= 8:
        signals.append(f"{round(commented_ratio * 100)}% of added lines are comments")
        categories.append("commented_or_dead_code")
        score += 18

    duplicate_count = repeated_added_line_count(added_lines)
    if duplicate_count >= 4:
        signals.append(f"{duplicate_count} repeated added lines")
        categories.append("copy_paste_noise")
        score += 14

    if is_generated(path) or classification == "generated":
        if changes > 120 or additions > 80:
            signals.append("large generated/lock/snapshot churn")
            categories.append("generated_churn")
            score += 24

    if is_test(path):
        test_smell = test_slop_signal(path, text, added_lines)
        if test_smell:
            signals.append(test_smell)
            categories.append("weak_test")
            score += 22

    unrelated = unrelated_change_signal(path, text, intent_terms or [])
    if unrelated and changes >= 18 and classification not in {"docs", "generated"}:
        signals.append(unrelated)
        categories.append("unrelated_noise")
        score += 16

    if additions >= 350 and not categories:
        signals.append("large additive change with no clear review category")
        categories.append("broad_noise")
        score += 14

    if not categories:
        return None

    score = min(100, score + min(12, changes // 35))
    severity = "review_required" if score >= 45 else "warn"
    disposition = disposition_for(path, categories, score)
    return {
        "path": path,
        "category": primary_category(categories),
        "categories": sorted(set(categories)),
        "severity": severity,
        "score": score,
        "confidence": confidence_for(score, signals),
        "message": message_for(path, disposition, categories),
        "signals": dedupe(signals)[:8],
        "suggested_action": suggested_action_for(disposition, categories),
        "disposition": disposition,
        "line_citations": [{"path": path, "range": "changed hunk"}],
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    prior = payload.get("prior_results", {})
    compression = prior.get("review-compression", {}).get("output", {})
    intent = prior.get("intent-extractor", {}).get("output", {})
    files = classified_files(payload, compression)
    raw_by_path = {
        normalize_path(file.get("path", "")): file
        for file in payload.get("changed_files", [])
        if file.get("path")
    }
    intent_terms = intent_terms_for(payload, intent)
    findings = [
        finding
        for file in files
        if (
            finding := inspect_file(
                file,
                raw_by_path.get(normalize_path(file.get("path", ""))),
                intent_terms,
            )
        )
    ]
    findings = sorted(findings, key=lambda item: (-int(item["score"]), item["path"]))
    for index, finding in enumerate(findings, start=1):
        finding["id"] = f"slop-{index}"

    remove_candidates = [finding for finding in findings if finding["disposition"] == "remove"]
    rework_candidates = [finding for finding in findings if finding["disposition"] == "rework"]
    score = slop_score(findings)
    status = "review" if findings else "pass"
    output = {
        "slop_score": score,
        "slop_status": status,
        "slop_findings": findings,
        "remove_candidates": remove_candidates,
        "rework_candidates": rework_candidates,
        "file_scores": [
            {
                "path": item["path"],
                "score": item["score"],
                "disposition": item["disposition"],
                "category": item["category"],
            }
            for item in findings
        ],
    }
    return make_agent_result(
        AGENT_ID,
        output,
        confidence=0.82 if findings else 0.72,
        messages=[message_summary(output)],
        trace=[
            {
                "step": "detect_review_slop",
                "files": len(files),
                "findings": len(findings),
                "remove_candidates": len(remove_candidates),
                "rework_candidates": len(rework_candidates),
            }
        ],
    )


def classified_files(payload: dict[str, Any], compression: dict[str, Any]) -> list[dict[str, Any]]:
    if compression.get("files"):
        return compression["files"]
    files = []
    for file in payload.get("changed_files", []):
        path = normalize_path(file.get("path", ""))
        files.append(
            {
                **file,
                "path": path,
                "classification": "test" if is_test(path) else "generated" if is_generated(path) else "logic",
                "risk_score": 24,
                "risk_reasons": [],
                "symbols": [],
            }
        )
    return files


def intent_terms_for(payload: dict[str, Any], intent: dict[str, Any]) -> list[str]:
    pr = payload.get("pull_request", {})
    terms = important_terms(" ".join(str(part) for part in [pr.get("title"), pr.get("body")] if part))
    for item in intent.get("intent_items", []):
        terms.extend(item.get("terms") or important_terms(item.get("text", "")))
    return sorted(set(terms))[:24]


def added_patch_lines(patch: str) -> list[str]:
    lines = []
    for raw in patch.splitlines():
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            lines.append(raw[1:].strip())
    return lines


def pattern_hits(patterns: list[tuple[re.Pattern[str], str]], text: str) -> list[str]:
    return [label for pattern, label in patterns if pattern.search(text)]


def commented_added_ratio(lines: list[str]) -> float:
    meaningful = [line for line in lines if line and not line in {"{", "}", ");"}]
    if not meaningful:
        return 0.0
    comments = [
        line
        for line in meaningful
        if line.startswith(("//", "#", "/*", "*", "<!--")) or line.endswith("-->")
    ]
    return len(comments) / len(meaningful)


def repeated_added_line_count(lines: list[str]) -> int:
    seen: dict[str, int] = {}
    for line in lines:
        clean = re.sub(r"\s+", " ", line.strip())
        if len(clean) < 12 or clean in {"};", "});"}:
            continue
        seen[clean] = seen.get(clean, 0) + 1
    return sum(count - 1 for count in seen.values() if count > 1)


def test_slop_signal(path: str, text: str, added_lines: list[str]) -> str | None:
    if not added_lines:
        return None
    lower = text.lower()
    has_test_shape = any(token in lower for token in ["describe(", "it(", "test(", "def test_"])
    has_assertion = any(pattern.search(text) for pattern in ASSERTION_PATTERNS)
    snapshot_only = "__snapshots__" in path or (
        "snapshot" in lower and not any(pattern.search(text) for pattern in ASSERTION_PATTERNS[:2])
    )
    if has_test_shape and not has_assertion:
        return "test file has scenario shape but no assertions"
    if snapshot_only:
        return "snapshot-only change needs reviewer confirmation"
    return None


def unrelated_change_signal(path: str, text: str, intent_terms: list[str]) -> str | None:
    if not intent_terms:
        return None
    normalized_haystack = expanded_text(" ".join([path, text]))
    if any(term in normalized_haystack for term in intent_terms):
        return None
    haystack_terms = set(expanded_terms(" ".join([path, text])))
    shared = sorted(haystack_terms & set(intent_terms))
    if shared:
        return None
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico"}:
        return "asset change has no overlap with PR intent terms"
    return "changed file has no overlap with PR intent terms"


def expanded_text(text: str) -> str:
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return re.sub(r"[^a-zA-Z0-9]+", " ", camel_split).lower()


def expanded_terms(text: str) -> list[str]:
    return important_terms(expanded_text(text))


def is_docs_like(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in {".md", ".mdx", ".txt", ".rst"}


def disposition_for(path: str, categories: list[str], score: int) -> str:
    path_terms = set(important_terms(path.replace("/", " ")))
    remove_categories = {
        "debug_artifact",
        "temporary_file",
        "generated_churn",
        "copy_paste_noise",
        "commented_or_dead_code",
    }
    if path_terms & SLOP_PATH_TOKENS and ("debug_artifact" in categories or "temporary_file" in categories):
        return "remove"
    if set(categories) & remove_categories and score >= 48:
        return "remove"
    return "rework"


def primary_category(categories: list[str]) -> str:
    priority = [
        "debug_artifact",
        "temporary_file",
        "placeholder_work",
        "weak_implementation",
        "weak_test",
        "generated_churn",
        "unrelated_noise",
        "commented_or_dead_code",
        "copy_paste_noise",
        "broad_noise",
    ]
    for item in priority:
        if item in categories:
            return item
    return categories[0]


def confidence_for(score: int, signals: list[str]) -> float:
    return round(min(0.94, 0.54 + score / 220 + min(0.12, len(signals) * 0.025)), 2)


def message_for(path: str, disposition: str, categories: list[str]) -> str:
    action = "Remove" if disposition == "remove" else "Rework"
    category = primary_category(categories).replace("_", " ")
    return f"{action} or justify `{path}`: {category} detected."


def suggested_action_for(disposition: str, categories: list[str]) -> str:
    if disposition == "remove":
        return "Remove this file/change from the PR or add a reviewer-visible justification for why it belongs."
    if "weak_test" in categories:
        return "Add assertions that prove the intended behavior or move the test out of this PR."
    if "placeholder_work" in categories or "weak_implementation" in categories:
        return "Replace placeholder behavior with production logic before merge."
    return "Narrow the change, explain why it belongs to this PR, or split it into a follow-up."


def slop_score(findings: list[dict[str, Any]]) -> int:
    if not findings:
        return 0
    top = sorted((int(item["score"]) for item in findings), reverse=True)[:4]
    return min(100, int(max(top) * 0.72 + (sum(top) / len(top)) * 0.28))


def message_summary(output: dict[str, Any]) -> str:
    findings = output["slop_findings"]
    if not findings:
        return "no review slop detected"
    return (
        f"detected {len(findings)} review slop findings "
        f"({len(output['remove_candidates'])} remove, {len(output['rework_candidates'])} rework)"
    )


def dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output


register_entrypoint(app, run)


def main() -> None:
    """Run the Magenta agent service when executed by agentic dev/deploy."""
    if not hasattr(app, "run"):
        raise RuntimeError(
            "Magenta SDK is required to run this agent service. "
            "Use the local orchestrator for demo mode."
        )
    app.run()


if __name__ == "__main__":
    main()
