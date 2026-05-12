from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

for _repo_root in [*Path(__file__).resolve().parents, Path("/app")]:
    if (_repo_root / "packages").is_dir():
        _repo_root_str = str(_repo_root)
        if _repo_root_str not in sys.path:
            sys.path.insert(0, _repo_root_str)
        break

from packages.agent_runtime import create_app, make_agent_result, register_entrypoint  # noqa: E402
from packages.core.analysis_utils import important_terms  # noqa: E402

AGENT_ID = "evidence-mapper"

app = create_app(AGENT_ID, "Map intent and findings to tests, canaries, contracts, traces, or HITL questions.")


@app.tool()
def map_intent(
    item: dict[str, Any],
    files: list[dict[str, Any]],
    memory_item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map an intent item to changed files, tests, or HITL questions that cover it."""
    terms = item.get("terms") or important_terms(item.get("text", ""))
    matched_files = [
        file
        for file in files
        if any(term in file.get("path", "").lower() or term in " ".join(file.get("risk_reasons", [])).lower() for term in terms)
    ]
    tests = [file for file in files if file.get("classification") == "test"]
    memory_item = memory_item or {}
    memory_tests = memory_item.get("test_candidates", [])
    memory_matches = memory_item.get("matches", [])
    if matched_files and tests:
        status = "proven"
    elif matched_files and memory_tests:
        status = "partial"
    elif memory_tests and memory_item.get("status") == "found":
        status = "partial"
    elif memory_matches:
        status = "partial"
    elif matched_files:
        status = "partial"
    else:
        status = "missing"
    return {
        "intent_id": item["id"],
        "intent_text": item["text"],
        "evidence_status": status,
        "mapped_paths": [file["path"] for file in matched_files[:8]],
        "evidence_paths": [file["path"] for file in tests[:8]],
        "memory_status": memory_item.get("status", "not_found"),
        "memory_evidence_paths": [item.get("path") or item.get("title") for item in memory_matches[:6]],
        "memory_test_candidates": [
            item.get("path") or item.get("title") for item in memory_tests[:6]
        ],
        "memory_match_count": len(memory_matches),
        "confidence": 0.84 if status == "proven" else 0.66 if status == "partial" else 0.55,
        "suggested_action": suggested_action(item, matched_files, tests, memory_item),
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    prior = payload.get("prior_results", {})
    compression = prior.get("review-compression", {}).get("output", {})
    intent = prior.get("intent-extractor", {}).get("output", {})
    memory = prior.get("semantic-evidence-agent", {}).get("output", {})
    files = compression.get("files", [])
    intent_items = intent.get("intent_items", [])
    memory_by_intent = {
        item.get("intent_id"): item
        for item in memory.get("requirement_evidence", [])
        if item.get("intent_id")
    }
    links = [map_intent(item, files, memory_by_intent.get(item.get("id"))) for item in intent_items]
    missing_source_evidence = [
        {
            "type": "missing-test",
            "path": file["path"],
            "severity": "review_required" if file["risk_score"] >= 45 else "warn",
            "message": f"{file['classification']} change has no changed test evidence.",
            "suggested_action": missing_test_action(file, memory),
        }
        for file in files
        if file.get("classification") in {"logic", "security-sensitive", "prompt"}
        and not any(test.get("classification") == "test" for test in files)
    ]
    suspend_payloads = [
        {
            "kind": "author-preview",
            "question": f"Confirm intended behavior: {item['text']}",
            "intent_id": item["id"],
        }
        for item, link in zip(intent_items, links, strict=False)
        if link["evidence_status"] == "missing" and item.get("confidence", 1) < 0.7
    ]
    output = {
        "evidence_links": links,
        "missing_evidence_findings": missing_source_evidence,
        "suspend_payloads": suspend_payloads,
        "semantic_memory": {
            "provider": memory.get("memory_provider"),
            "related_tests": memory.get("related_tests", []),
            "requirement_evidence": memory.get("requirement_evidence", []),
        },
    }
    return make_agent_result(
        AGENT_ID,
        output,
        confidence=0.76,
        messages=[f"mapped evidence for {len(intent_items)} intent items"],
        trace=[{"step": "map_evidence", "links": len(links), "missing": len(missing_source_evidence)}],
    )


def suggested_action(
    item: dict[str, Any],
    matched_files: list[dict[str, Any]],
    tests: list[dict[str, Any]],
    memory_item: dict[str, Any] | None = None,
) -> str:
    if matched_files and tests:
        return "Verify changed tests exercise this intent."
    memory_item = memory_item or {}
    memory_tests = memory_item.get("test_candidates", [])
    if memory_tests:
        test_path = memory_tests[0].get("path") or memory_tests[0].get("title")
        return f"Extend or link existing repository evidence in {test_path}."
    if matched_files:
        return f"Add tests or reviewer acceptance for {matched_files[0]['path']}."
    if memory_item.get("matches"):
        return memory_item.get("suggested_action") or "Review related repository memory before approval."
    return f"Clarify or implement intent: {item['text']}"


def missing_test_action(file: dict[str, Any], memory: dict[str, Any]) -> str:
    related = [
        item
        for item in memory.get("related_tests", [])
        if related_text(file.get("path", ""), " ".join([item.get("path", ""), item.get("text", "")]))
    ]
    if related:
        path = related[0].get("path") or related[0].get("title")
        return f"Update or link existing repository test evidence in {path} for {file['path']}."
    return f"Add or link tests for {file['path']}."


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
