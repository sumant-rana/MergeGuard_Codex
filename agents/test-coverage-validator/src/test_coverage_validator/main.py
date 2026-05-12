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
from packages.core.analysis_utils import important_terms, is_test, normalize_path  # noqa: E402

AGENT_ID = "test-coverage-validator"

app = create_app(
    AGENT_ID,
    "Validate whether changed tests cover changed functionality, PR intent, and behavior deltas.",
)


@app.tool()
def evaluate_target_coverage(
    target: dict[str, Any],
    tests: list[dict[str, Any]],
    intent_items: list[dict[str, Any]],
    behavior_deltas: list[dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate which tests cover a given target's intent items and behavior deltas."""
    target_terms = target_terms_for(target, intent_items, behavior_deltas)
    matches = sorted(
        [test_match(target, test, target_terms) for test in tests],
        key=lambda item: item["coverage"],
        reverse=True,
    )
    useful_matches = [match for match in matches if match["coverage"] >= 0.18]
    best = useful_matches[0]["coverage"] if useful_matches else 0.0
    status = "covered" if best >= 0.72 else "partial" if best >= 0.38 else "missing"
    missing_terms = sorted(set(target_terms) - set().union(*(match["matched_terms"] for match in useful_matches)))[:8]
    return {
        "path": target["path"],
        "classification": target.get("classification", "logic"),
        "risk_score": int(target.get("risk_score") or 0),
        "status": status,
        "coverage": round(best, 2),
        "confidence": confidence_for(status, useful_matches),
        "matched_tests": useful_matches[:5],
        "missing_terms": missing_terms,
        "reason": coverage_reason(status, target, useful_matches),
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    prior = payload.get("prior_results", {})
    compression = prior.get("review-compression", {}).get("output", {})
    intent = prior.get("intent-extractor", {}).get("output", {})
    semantic = prior.get("semantic-diff-explainer", {}).get("output", {})
    evidence = prior.get("evidence-mapper", {}).get("output", {})
    memory = prior.get("semantic-evidence-agent", {}).get("output", {})
    files = classified_files(payload, compression)
    tests = [file for file in files if file.get("classification") == "test" or is_test(file["path"])]
    targets = [
        file
        for file in files
        if file.get("classification") not in {"docs", "generated", "test"}
        and file.get("status") != "removed"
    ]
    intent_items = intent.get("intent_items", [])
    behavior_deltas = semantic.get("behavioral_deltas", [])
    coverage_matrix = [
        evaluate_target_coverage(target, tests, intent_items, behavior_deltas)
        for target in targets
    ]
    intent_coverage = [
        intent_coverage_item(item, tests, evidence.get("evidence_links", []))
        for item in intent_items
        if item.get("category") != "out_of_scope"
    ]
    behavior_coverage = [behavior_coverage_item(item, tests) for item in behavior_deltas]
    findings = coverage_findings(coverage_matrix, intent_coverage, behavior_coverage, tests)
    score = coverage_score(coverage_matrix, intent_coverage, behavior_coverage, tests, targets)
    status = coverage_status(score, findings, targets)
    recommendations = [
        *[recommendation_for(item) for item in findings[:8]],
        *memory_recommendations(memory, targets),
    ][:10]
    output_confidence = output_confidence_for(score, coverage_matrix, tests, targets)
    output = {
        "coverage_score": score,
        "coverage_status": status,
        "confidence": output_confidence,
        "test_files": [test_file_summary(test) for test in tests],
        "source_targets": [source_target_summary(target) for target in targets],
        "coverage_matrix": coverage_matrix,
        "intent_coverage": intent_coverage,
        "behavior_coverage": behavior_coverage,
        "coverage_findings": findings,
        "recommendations": recommendations,
        "repository_memory": {
            "provider": memory.get("memory_provider"),
            "related_tests": memory.get("related_tests", []),
            "requirement_evidence": memory.get("requirement_evidence", []),
            "recommended_test_updates": memory.get("recommended_test_updates", []),
        },
    }
    return make_agent_result(
        AGENT_ID,
        output,
        status="failed" if status == "blocked" else "completed",
        confidence=output_confidence,
        messages=[f"test coverage {score}% with {len(findings)} gaps"],
        trace=[
            {
                "step": "validate_test_coverage",
                "targets": len(targets),
                "tests": len(tests),
                "coverage_score": score,
                "findings": len(findings),
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
                "classification": "test" if is_test(path) else "logic",
                "risk_score": 24,
                "risk_reasons": [],
                "symbols": [],
            }
        )
    return files


def target_terms_for(
    target: dict[str, Any],
    intent_items: list[dict[str, Any]],
    behavior_deltas: list[dict[str, Any]],
) -> list[str]:
    text_parts = [
        target.get("path", ""),
        " ".join(target.get("risk_reasons", [])),
        " ".join(symbol.get("name", "") for symbol in target.get("symbols", [])),
    ]
    for item in intent_items:
        if related_text(target["path"], item.get("text", "")):
            text_parts.append(item.get("text", ""))
    for delta in behavior_deltas:
        if delta.get("path") == target.get("path"):
            text_parts.extend(
                [
                    delta.get("symbol", ""),
                    delta.get("new_behavior", ""),
                    delta.get("divergent_input", ""),
                ]
            )
    return important_terms(" ".join(str(part) for part in text_parts if part))[:14]


def test_match(target: dict[str, Any], test: dict[str, Any], terms: list[str]) -> dict[str, Any]:
    test_text = test_haystack(test)
    target_stem = PurePosixPath(target["path"]).stem.lower()
    test_path = test["path"].lower()
    matched_terms = [term for term in terms if term in test_text]
    score = 0.0
    if target_stem and target_stem in test_path:
        score += 0.34
    if shared_directory_score(target["path"], test["path"]):
        score += 0.1
    if terms:
        score += min(0.34, len(matched_terms) / len(terms) * 0.5)
    if assertion_count(test) > 0:
        score += 0.12
    if any(token in test_text for token in ["boundary", "failure", "retry", "unauthorized"]):
        score += 0.1
    coverage = round(min(score, 1.0), 2)
    return {
        "path": test["path"],
        "coverage": coverage,
        "assertions": assertion_count(test),
        "matched_terms": matched_terms[:8],
    }


def intent_coverage_item(
    item: dict[str, Any],
    tests: list[dict[str, Any]],
    evidence_links: list[dict[str, Any]],
) -> dict[str, Any]:
    terms = item.get("terms") or important_terms(item.get("text", ""))
    matches = sorted(
        [
            {
                "path": test["path"],
                "matched_terms": [term for term in terms if term in test_haystack(test)],
            }
            for test in tests
        ],
        key=lambda match: len(match["matched_terms"]),
        reverse=True,
    )
    matches = [match for match in matches if match["matched_terms"]]
    linked = next(
        (link for link in evidence_links if link.get("intent_id") == item.get("id")),
        {},
    )
    evidence_status = linked.get("evidence_status", "missing")
    coverage = min(1.0, (len(matches[0]["matched_terms"]) / max(len(terms), 1)) if matches else 0.0)
    if evidence_status == "proven":
        coverage = max(coverage, 0.78)
    elif evidence_status == "partial":
        coverage = max(coverage, 0.45)
    status = "covered" if coverage >= 0.7 else "partial" if coverage >= 0.35 else "missing"
    return {
        "intent_id": item.get("id"),
        "intent_text": item.get("text", ""),
        "category": item.get("category", "should"),
        "status": status,
        "coverage": round(coverage, 2),
        "matched_tests": matches[:4],
        "evidence_status": evidence_status,
    }


def behavior_coverage_item(item: dict[str, Any], tests: list[dict[str, Any]]) -> dict[str, Any]:
    terms = important_terms(
        " ".join(
            str(part)
            for part in [
                item.get("path", ""),
                item.get("symbol", ""),
                item.get("new_behavior", ""),
                item.get("divergent_input", ""),
            ]
            if part
        )
    )
    matches = [
        {
            "path": test["path"],
            "matched_terms": [term for term in terms if term in test_haystack(test)],
        }
        for test in tests
    ]
    matches = [match for match in matches if match["matched_terms"]]
    best = max((len(match["matched_terms"]) / max(len(terms), 1) for match in matches), default=0.0)
    status = "covered" if best >= 0.65 else "partial" if best >= 0.3 else "missing"
    return {
        "path": item.get("path", ""),
        "symbol": item.get("symbol", ""),
        "severity": item.get("severity", "warn"),
        "status": status,
        "coverage": round(best, 2),
        "matched_tests": matches[:4],
    }


def coverage_findings(
    coverage_matrix: list[dict[str, Any]],
    intent_coverage: list[dict[str, Any]],
    behavior_coverage: list[dict[str, Any]],
    tests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not tests and coverage_matrix:
        findings.append(
            {
                "type": "no-tests",
                "path": coverage_matrix[0]["path"],
                "severity": "block",
                "message": "PR changes functionality but does not include changed test evidence.",
                "suggested_action": "Add tests that exercise the changed behavior before merge.",
            }
        )
    for item in coverage_matrix:
        if item["status"] == "covered":
            continue
        severity = "block" if item["risk_score"] >= 60 and item["status"] == "missing" else "review_required"
        findings.append(
            {
                "type": "source-coverage-gap",
                "path": item["path"],
                "severity": severity,
                "message": f"{item['path']} has {item['status']} changed-test coverage.",
                "suggested_action": f"Add or link tests for {item['path']}.",
            }
        )
    for item in intent_coverage:
        if item["status"] == "missing" and item.get("category") in {"should", "must_not"}:
            findings.append(
                {
                    "type": "intent-coverage-gap",
                    "path": item.get("intent_id") or "pr-intent",
                    "severity": "review_required",
                    "message": f"Intent is not covered by changed tests: {item['intent_text']}",
                    "suggested_action": "Add an assertion for this PR intent or link existing evidence.",
                }
            )
    for item in behavior_coverage:
        if item["status"] == "missing" and item.get("severity") == "review_required":
            findings.append(
                {
                    "type": "behavior-coverage-gap",
                    "path": item["path"],
                    "severity": "review_required",
                    "message": f"Behavioral delta for {item['path']} lacks test coverage.",
                    "suggested_action": "Add a behavior-level test for the divergent input.",
                }
            )
    return findings[:20]


def coverage_score(
    coverage_matrix: list[dict[str, Any]],
    intent_coverage: list[dict[str, Any]],
    behavior_coverage: list[dict[str, Any]],
    tests: list[dict[str, Any]],
    targets: list[dict[str, Any]],
) -> int:
    if not targets and not intent_coverage and not behavior_coverage:
        return 100
    if targets and not tests:
        return 0
    scores = [
        *(item["coverage"] for item in coverage_matrix),
        *(item["coverage"] for item in intent_coverage),
        *(item["coverage"] for item in behavior_coverage),
    ]
    return int(round((sum(scores) / len(scores)) * 100)) if scores else 100


def coverage_status(score: int, findings: list[dict[str, Any]], targets: list[dict[str, Any]]) -> str:
    if not targets:
        return "pass"
    if any(item.get("severity") == "block" for item in findings) or score < 45:
        return "blocked"
    if findings or score < 80:
        return "review"
    return "pass"


def output_confidence_for(
    score: int,
    coverage_matrix: list[dict[str, Any]],
    tests: list[dict[str, Any]],
    targets: list[dict[str, Any]],
) -> float:
    if not targets:
        return 0.9
    if not tests:
        return 0.82
    matrix_confidence = sum(item["confidence"] for item in coverage_matrix) / max(len(coverage_matrix), 1)
    certainty = 0.42 + matrix_confidence * 0.46 + (0.12 if score >= 80 else 0)
    return round(min(certainty, 0.94), 2)


def confidence_for(status: str, matches: list[dict[str, Any]]) -> float:
    if status == "missing":
        return 0.78
    best = matches[0]["coverage"] if matches else 0
    return round(min(0.94, 0.55 + best * 0.38), 2)


def coverage_reason(
    status: str,
    target: dict[str, Any],
    matches: list[dict[str, Any]],
) -> str:
    if status == "covered":
        return f"Changed tests strongly match {target['path']}."
    if status == "partial":
        return f"Changed tests partially match {target['path']} but miss important behavior terms."
    if matches:
        return f"Only weak test signals were found for {target['path']}."
    return f"No changed test evidence was found for {target['path']}."


def recommendation_for(finding: dict[str, Any]) -> dict[str, Any]:
    path = str(finding.get("path") or "changed-behavior")
    stem = PurePosixPath(path).stem.replace("-", "_")
    suggested_path = f"tests/{stem}_test.py" if not path.endswith((".ts", ".tsx", ".js", ".jsx")) else f"{stem}.test.ts"
    return {
        "path": suggested_path,
        "framework": "repo-default",
        "intent": finding.get("suggested_action", "Add changed-behavior coverage."),
    }


def memory_recommendations(memory: dict[str, Any], targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    target_paths = [target["path"] for target in targets[:4]]
    for item in memory.get("recommended_test_updates", [])[:6]:
        recommendations.append(
            {
                "path": item.get("path", "tests/repository-memory.test"),
                "framework": item.get("framework", "repo-memory"),
                "intent": item.get("intent")
                or f"Use repository memory to validate {', '.join(target_paths)}.",
                "source": "semantic-evidence-agent",
                "memory_score": item.get("memory_score"),
            }
        )
    if recommendations:
        return recommendations
    for test in memory.get("related_tests", [])[:3]:
        path = test.get("path") or test.get("title") or "tests/repository-memory.test"
        recommendations.append(
            {
                "path": path,
                "framework": "repo-memory",
                "intent": (
                    f"Review existing test evidence against "
                    f"{', '.join(target_paths) or 'the PR intent'}."
                ),
                "source": "semantic-evidence-agent",
                "memory_score": test.get("score"),
            }
        )
    return recommendations


def test_file_summary(test: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": test["path"],
        "assertions": assertion_count(test),
        "changed_lines": int(test.get("changes") or 0),
    }


def source_target_summary(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": target["path"],
        "classification": target.get("classification"),
        "risk_score": target.get("risk_score", 0),
    }


def shared_directory_score(left: str, right: str) -> bool:
    left_parts = PurePosixPath(left).parts[:-1]
    right_parts = PurePosixPath(right).parts[:-1]
    return bool(set(left_parts) & set(right_parts))


def assertion_count(test: dict[str, Any]) -> int:
    text = test_haystack(test)
    return len(
        re.findall(
            r"\b(assert|expect|should|it\(|test\(|describe\(|to_equal|toEqual|equals?)\b",
            text,
        )
    )


def test_haystack(test: dict[str, Any]) -> str:
    return "\n".join(
        str(part).lower()
        for part in [test.get("path", ""), test.get("patch", ""), test.get("content", "")]
        if part
    )


def related_text(path: str, text: str) -> bool:
    path_terms = set(important_terms(path))
    text_terms = set(important_terms(text))
    return bool(path_terms & text_terms)


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
