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
def map_intent(item: dict[str, Any], files: list[dict[str, Any]]) -> dict[str, Any]:
    """Map an intent item to changed files, tests, or HITL questions that cover it."""
    terms = item.get("terms") or important_terms(item.get("text", ""))
    matched_files = [
        file
        for file in files
        if any(term in file.get("path", "").lower() or term in " ".join(file.get("risk_reasons", [])).lower() for term in terms)
    ]
    tests = [file for file in files if file.get("classification") == "test"]
    if matched_files and tests:
        status = "proven"
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
        "confidence": 0.84 if status == "proven" else 0.66 if status == "partial" else 0.55,
        "suggested_action": suggested_action(item, matched_files, tests),
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    prior = payload.get("prior_results", {})
    compression = prior.get("review-compression", {}).get("output", {})
    intent = prior.get("intent-extractor", {}).get("output", {})
    files = compression.get("files", [])
    intent_items = intent.get("intent_items", [])
    links = [map_intent(item, files) for item in intent_items]
    missing_source_evidence = [
        {
            "type": "missing-test",
            "path": file["path"],
            "severity": "review_required" if file["risk_score"] >= 45 else "warn",
            "message": f"{file['classification']} change has no changed test evidence.",
            "suggested_action": f"Add or link tests for {file['path']}.",
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
    }
    return make_agent_result(
        AGENT_ID,
        output,
        confidence=0.76,
        messages=[f"mapped evidence for {len(intent_items)} intent items"],
        trace=[{"step": "map_evidence", "links": len(links), "missing": len(missing_source_evidence)}],
    )


def suggested_action(item: dict[str, Any], matched_files: list[dict[str, Any]], tests: list[dict[str, Any]]) -> str:
    if matched_files and tests:
        return "Verify changed tests exercise this intent."
    if matched_files:
        return f"Add tests or reviewer acceptance for {matched_files[0]['path']}."
    return f"Clarify or implement intent: {item['text']}"


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
