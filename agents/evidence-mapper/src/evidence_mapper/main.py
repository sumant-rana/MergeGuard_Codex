from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

for _repo_root in [*Path(__file__).resolve().parents, Path("/app")]:
    if (_repo_root / "packages").is_dir():
        _repo_root_str = str(_repo_root)
        if _repo_root_str not in sys.path:
            sys.path.insert(0, _repo_root_str)
        break

from packages.agent_runtime import (  # noqa: E402
    call_llm_json,
    create_app,
    llm_available,
    make_agent_result,
    register_entrypoint,
)
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
    mode = "fallback"
    links: list[dict[str, Any]] = []
    if llm_available() and intent_items:
        llm_links = _map_via_llm(intent_items, files, memory_by_intent)
        if llm_links is not None:
            links = llm_links
            mode = "llm"
    if not links:
        links = [
            map_intent(item, files, memory_by_intent.get(item.get("id")))
            for item in intent_items
        ]
        mode = "fallback" if mode != "llm" else mode
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
        confidence=0.86 if mode == "llm" else 0.76,
        messages=[f"mapped evidence for {len(intent_items)} intent items via {mode}"],
        trace=[
            {
                "step": "map_evidence",
                "mode": mode,
                "links": len(links),
                "missing": len(missing_source_evidence),
            }
        ],
    )


# ── LLM path ────────────────────────────────────────────────────────────────


_EVIDENCE_SYSTEM_PROMPT = (
    "You are a senior reviewer mapping pull-request INTENT items to the "
    "CHANGED FILES that prove or fail to prove each intent.\n\n"
    "For each intent, classify evidence_status as one of:\n"
    "  - 'proven'  — at least one changed test or implementation file directly\n"
    "                exercises this intent.\n"
    "  - 'partial' — relevant code changed but the changed-test evidence is\n"
    "                weak (no test file, only stub assertions, unrelated paths).\n"
    "  - 'missing' — no changed file exercises this intent at all.\n\n"
    "Rules:\n"
    "- Use file paths from the provided list ONLY. Do not invent paths.\n"
    "- mapped_paths: changed implementation files that touch the intent's domain.\n"
    "- evidence_paths: changed *test* files that exercise the intent.\n"
    "- suggested_action: one concrete next step the author should take.\n"
    "- confidence: 0-1; lower if the match is loose / inferred.\n\n"
    "Output a single JSON object:\n"
    '{"evidence_links": [\n'
    '  {"intent_id": str, "intent_text": str, "evidence_status": "proven"|"partial"|"missing",\n'
    '   "mapped_paths": [str], "evidence_paths": [str], "confidence": float,\n'
    '   "suggested_action": str}\n'
    "]}"
)


def _map_via_llm(
    intent_items: list[dict[str, Any]],
    files: list[dict[str, Any]],
    memory_by_intent: dict[str, dict[str, Any]],
) -> list[dict[str, Any]] | None:
    file_summaries = [
        {
            "path": f.get("path"),
            "classification": f.get("classification"),
            "risk_score": f.get("risk_score"),
            "risk_reasons": (f.get("risk_reasons") or [])[:4],
        }
        for f in files
        if isinstance(f, dict) and f.get("path")
    ]
    intent_summaries = [
        {
            "id": item.get("id"),
            "text": item.get("text"),
            "category": item.get("category"),
            "terms": item.get("terms", [])[:6],
            "memory_hint": _memory_summary_for(memory_by_intent.get(item.get("id"))),
        }
        for item in intent_items
        if isinstance(item, dict)
    ]
    user_prompt = (
        "Map each intent item to changed-file evidence. Use the system-prompt schema.\n\n"
        f"```json\n{json.dumps({'intents': intent_summaries, 'changed_files': file_summaries}, indent=2)}\n```"
    )
    result = call_llm_json(
        system=_EVIDENCE_SYSTEM_PROMPT,
        user=user_prompt,
        temperature=0.0,
        max_tokens=1600,
    )
    if not result:
        return None
    links = result.get("evidence_links")
    if not isinstance(links, list):
        return None

    valid_statuses = {"proven", "partial", "missing"}
    file_paths = {f.get("path") for f in file_summaries}
    cleaned: list[dict[str, Any]] = []
    for raw in links:
        if not isinstance(raw, dict):
            continue
        intent_id = raw.get("intent_id")
        if not intent_id:
            continue
        status = raw.get("evidence_status")
        if status not in valid_statuses:
            status = "partial"
        # Keep only paths that actually exist in the changed file list.
        mapped = [p for p in (raw.get("mapped_paths") or []) if p in file_paths]
        evidence = [p for p in (raw.get("evidence_paths") or []) if p in file_paths]
        memory_item = memory_by_intent.get(intent_id) or {}
        memory_matches = memory_item.get("matches", [])
        memory_tests = memory_item.get("test_candidates", [])
        cleaned.append(
            {
                "intent_id": intent_id,
                "intent_text": raw.get("intent_text", ""),
                "evidence_status": status,
                "mapped_paths": mapped[:8],
                "evidence_paths": evidence[:8],
                "memory_status": memory_item.get("status", "not_found"),
                "memory_evidence_paths": [
                    m.get("path") or m.get("title") for m in memory_matches[:6]
                ],
                "memory_test_candidates": [
                    t.get("path") or t.get("title") for t in memory_tests[:6]
                ],
                "memory_match_count": len(memory_matches),
                "confidence": float(raw.get("confidence") or (0.85 if status == "proven" else 0.65)),
                "suggested_action": str(raw.get("suggested_action") or ""),
            }
        )
    return cleaned


def _memory_summary_for(memory_item: dict[str, Any] | None) -> dict[str, Any]:
    if not memory_item:
        return {"status": "not_found"}
    return {
        "status": memory_item.get("status", "not_found"),
        "test_candidates": [
            t.get("path") or t.get("title")
            for t in (memory_item.get("test_candidates") or [])[:3]
        ],
        "matches": [
            m.get("path") or m.get("title")
            for m in (memory_item.get("matches") or [])[:3]
        ],
    }


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
