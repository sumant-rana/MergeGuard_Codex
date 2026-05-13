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
    """Tag a changed file with concept findings (auth, PII, billing, SQL, prompt, etc.)."""
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
    for finding in findings:
        finding.setdefault("source", "rules")

    # LLM augmentation: for high-risk files (>=40 risk_score) ask an LLM to
    # spot concepts the rule glossary missed. Augments — never overrides —
    # the deterministic findings so the policy-gate path stays auditable.
    llm_findings: list[dict[str, Any]] = []
    if llm_available():
        high_risk_targets = [
            f for f in files
            if f.get("risk_score", 0) >= 40
            and f.get("classification") not in {"docs", "generated", "test"}
        ]
        if high_risk_targets:
            existing_by_path: dict[str, set[str]] = {}
            for finding in findings:
                existing_by_path.setdefault(finding["path"], set()).add(finding["concept"])
            llm_findings = _augment_via_llm(
                high_risk_targets,
                raw_by_path,
                already_found=existing_by_path,
            )
    findings.extend(llm_findings)

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
        messages=[
            f"classified {len(findings)} concept findings "
            f"({len(llm_findings)} from LLM augment)"
        ],
        trace=[
            {
                "step": "classify_concepts",
                "rules_findings": len(findings) - len(llm_findings),
                "llm_findings": len(llm_findings),
            }
        ],
    )


# ── LLM augmentation ───────────────────────────────────────────────────────


_CONCEPT_GLOSSARY = (
    "auth_check, pii_read, pii_write, billing_side_effect, idempotency_check, "
    "external_http_call, timeout_configured, retry, raw_sql, cache_invalidation, "
    "feature_flag, prompt, agent_tool_call"
)

_CONCEPT_SYSTEM_PROMPT = (
    "You are scanning a changed file's diff to identify SUBTLE review-relevant "
    "concepts that a keyword-based rule layer might miss.\n\n"
    f"Valid concepts (use these labels exactly): {_CONCEPT_GLOSSARY}.\n\n"
    "Rules:\n"
    "- Only report a concept if you can quote a specific token / identifier /\n"
    "  function call from the diff that supports it. Put that quote in 'evidence'.\n"
    "- Skip concepts that the rule layer ALREADY found (provided in 'rules_already_found').\n"
    "- Be conservative — only report concepts whose presence would change a reviewer's mind.\n"
    "- confidence: 0-1; use <0.5 for inferred / ambiguous matches.\n\n"
    "Output a single JSON object:\n"
    '{"new_concept_findings": [\n'
    '  {"concept": str, "path": str, "evidence": [str], "confidence": float, "reasoning": str}\n'
    "]}\n"
    "If there's nothing new, return {\"new_concept_findings\": []}."
)


def _augment_via_llm(
    high_risk_targets: list[dict[str, Any]],
    raw_by_path: dict[str, dict[str, Any]],
    already_found: dict[str, set[str]],
) -> list[dict[str, Any]]:
    files_payload = []
    for file in high_risk_targets[:8]:
        path = file.get("path")
        raw = raw_by_path.get(path, {})
        files_payload.append(
            {
                "path": path,
                "classification": file.get("classification"),
                "risk_score": file.get("risk_score"),
                "patch_excerpt": (raw.get("patch") or "")[:2000],
                "rules_already_found": sorted(already_found.get(path, [])),
            }
        )
    user_prompt = (
        "Identify any review concepts the rule layer missed. Use the system-prompt schema.\n\n"
        f"```json\n{json.dumps({'files': files_payload}, indent=2)}\n```"
    )
    result = call_llm_json(
        system=_CONCEPT_SYSTEM_PROMPT,
        user=user_prompt,
        temperature=0.0,
        max_tokens=1000,
    )
    if not result:
        return []
    new_findings = result.get("new_concept_findings")
    if not isinstance(new_findings, list):
        return []

    valid_concepts = set(_CONCEPT_GLOSSARY.replace(",", "").split())
    cleaned: list[dict[str, Any]] = []
    for raw in new_findings:
        if not isinstance(raw, dict):
            continue
        concept = raw.get("concept")
        path = raw.get("path")
        if concept not in valid_concepts or not path:
            continue
        # Skip if rules already found this concept for the path.
        if concept in already_found.get(path, set()):
            continue
        cleaned.append(
            {
                "concept": concept,
                "path": path,
                "symbol": None,
                "confidence": float(raw.get("confidence") or 0.6),
                "relation": "introduced-or-modified",
                "evidence": [str(e) for e in (raw.get("evidence") or [])][:5],
                "reasoning": str(raw.get("reasoning") or ""),
                "source": "llm-augment",
            }
        )
    return cleaned


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
