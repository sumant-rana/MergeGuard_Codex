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

AGENT_ID = "concept-classifier"

CONCEPT_PATTERNS = {
    "auth_check": ["auth", "authorize", "permission", "role", "session", "token"],
    "pii_read": ["pii", "email", "phone", "ssn", "address"],
    "pii_write": ["save_email", "persist", "update_profile", "customeremail", "customer_email"],
    "billing_side_effect": ["payment", "billing", "refund", "charge", "invoice", "payout"],
    "idempotency_check": ["idempotency", "idempotent", "dedupe"],
    "external_http_call": ["fetch(", "requests.", "http.", "axios", "webhook"],
    "timeout_configured": ["timeout", "deadline", "abortcontroller"],
    "retry": ["retry", "backoff", "transient"],
    "raw_sql": ["select ", "insert ", "update ", "delete ", "cursor.execute", "raw sql"],
    "cache_invalidation": ["cache", "invalidate", "redis"],
    "feature_flag": ["feature_flag", "launchdarkly", "flag"],
    "prompt": ["prompt", "system message", "model", "temperature"],
    "agent_tool_call": ["tool_call", "function_call", "agent", "planner"],
}

app = create_app(AGENT_ID, "Classify changed functions and files into review concepts.")


@app.tool()
def classify_concepts(file: dict[str, Any], raw_file: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    raw_file = raw_file or {}
    haystack = "\n".join(
        str(part).lower()
        for part in [
            file.get("path"),
            file.get("classification"),
            " ".join(file.get("risk_reasons", [])),
            raw_file.get("patch", ""),
            raw_file.get("content", ""),
        ]
        if part
    )
    findings = []
    for concept, terms in CONCEPT_PATTERNS.items():
        matched = [term for term in terms if term in haystack]
        if not matched:
            continue
        findings.append(
            {
                "concept": concept,
                "path": file["path"],
                "symbol": (file.get("symbols") or [{}])[0].get("name"),
                "confidence": min(0.94, 0.52 + len(matched) * 0.12 + file.get("risk_score", 0) / 500),
                "relation": "introduced-or-modified",
                "evidence": matched[:5],
            }
        )
    return findings


def run(payload: dict[str, Any]) -> dict[str, Any]:
    compression = payload.get("prior_results", {}).get("review-compression", {}).get("output", {})
    files = compression.get("files", [])
    raw_by_path = {file.get("path"): file for file in payload.get("changed_files", [])}
    findings = [
        finding
        for file in files
        for finding in classify_concepts(file, raw_by_path.get(file.get("path")))
    ]
    output = {
        "concept_findings": findings,
        "memory_writes": [
            {
                "collection": "memory_taxonomic",
                "label": finding["concept"],
                "text": finding["path"],
                "metadata": {"path": finding["path"], "symbol": finding.get("symbol")},
            }
            for finding in findings
        ],
    }
    return make_agent_result(
        AGENT_ID,
        output,
        confidence=0.74,
        messages=[f"classified {len(findings)} concept findings"],
        trace=[{"step": "classify_concepts", "findings": len(findings)}],
    )


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
