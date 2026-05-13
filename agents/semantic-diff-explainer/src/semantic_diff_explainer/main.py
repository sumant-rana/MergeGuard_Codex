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
from packages.core.analysis_utils import risk_hits  # noqa: E402

AGENT_ID = "semantic-diff-explainer"

app = create_app(AGENT_ID, "Explain changed behavior and blast radius.")


@app.tool(is_local=False)
def extract_function_deltas(file: dict[str, Any]) -> list[dict[str, Any]]:
    """Fallback deterministic delta extractor (used when LLM is unavailable)."""
    symbols = file.get("symbols") or [{"name": file["path"], "kind": "file", "confidence": 0.4}]
    hits = risk_hits(file["path"], " ".join(file.get("risk_reasons", [])))
    deltas = []
    for symbol in symbols[:4]:
        deltas.append(
            {
                "path": file["path"],
                "symbol": symbol.get("name"),
                "old_behavior": _old_behavior_fallback(file),
                "new_behavior": _new_behavior_fallback(file, hits),
                "divergent_input": _divergent_input_fallback(hits),
                "severity": "review_required" if file.get("risk_score", 0) >= 60 else "warn",
                "confidence": min(0.9, symbol.get("confidence", 0.5) + 0.08),
                "line_citations": [{"path": file["path"], "range": "changed hunk"}],
            }
        )
    return deltas


def run(payload: dict[str, Any]) -> dict[str, Any]:
    compression = payload.get("prior_results", {}).get("review-compression", {}).get("output", {})
    classified_files = compression.get("files", [])
    raw_changed_files = payload.get("changed_files", [])
    patches_by_path = {
        f.get("path"): f.get("patch", "")
        for f in raw_changed_files
        if isinstance(f, dict) and f.get("path")
    }

    target_files = [
        file
        for file in classified_files
        if file.get("classification") not in {"docs", "generated", "test"}
        and file.get("risk_score", 0) >= 20
    ]

    deltas: list[dict[str, Any]] = []
    blast_radius: list[dict[str, Any]] = []
    mode = "fallback"

    if llm_available() and target_files:
        llm_result = _analyze_via_llm(target_files, patches_by_path)
        if llm_result is not None:
            deltas = llm_result["behavioral_deltas"]
            blast_radius = llm_result["blast_radius"]
            mode = "llm"

    if mode == "fallback":
        deltas = [delta for file in target_files for delta in extract_function_deltas(file)]
        blast_radius = [_blast_radius_fallback(file) for file in target_files]

    output = {
        "behavioral_deltas": sorted(
            deltas, key=lambda item: item.get("severity") != "review_required"
        ),
        "blast_radius": blast_radius,
    }
    return make_agent_result(
        AGENT_ID,
        output,
        confidence=0.85 if mode == "llm" else 0.72,
        messages=[
            f"explained {len(deltas)} behavioral deltas via {mode}",
        ],
        trace=[
            {
                "step": "semantic_diff",
                "mode": mode,
                "files": len(target_files),
                "deltas": len(deltas),
            }
        ],
    )


# ── LLM path ────────────────────────────────────────────────────────────────


_SEMANTIC_DIFF_SYSTEM_PROMPT = (
    "You are a senior code reviewer analyzing the BEHAVIORAL impact of a pull request. "
    "For each changed file given, infer what the code did BEFORE and what it does NOW, "
    "based on the diff hunks. Then estimate downstream impact.\n\n"
    "Rules:\n"
    "- Ground every claim in the diff text — do NOT invent functions, services, or callers.\n"
    "- Be specific and concrete; avoid generic 'business logic changed' filler.\n"
    "- If the diff is too small or unclear, say so plainly in old_behavior/new_behavior.\n"
    "- severity is 'review_required' for changes touching auth / payments / data writes /\n"
    "  schema-breaking edits / prompts; 'warn' otherwise.\n"
    "- divergent_input is the single most likely input that would behave differently\n"
    "  between old and new code (e.g., 'duplicate refund request with reused idempotency key').\n"
    "- downstream_services / direct_callers / impacted_tests: at most 4 each. Only include\n"
    "  names you can justify from the diff or the path. Empty arrays are fine.\n\n"
    "Output a single JSON object:\n"
    "{\n"
    '  "behavioral_deltas": [\n'
    '    {"path": str, "symbol": str, "old_behavior": str, "new_behavior": str,\n'
    '     "divergent_input": str | null, "severity": "warn"|"review_required",\n'
    '     "confidence": float, "line_citations": [{"path": str, "range": str}]}\n'
    "  ],\n"
    '  "blast_radius": [\n'
    '    {"path": str, "owners": [str], "downstream_services": [str],\n'
    '     "direct_callers": [str], "impacted_tests": [str], "confidence": float}\n'
    "  ]\n"
    "}"
)


def _analyze_via_llm(
    target_files: list[dict[str, Any]],
    patches_by_path: dict[str, str],
) -> dict[str, list[dict[str, Any]]] | None:
    """Run one LLM call over all target files; return parsed deltas+blast radius."""
    payload_files = []
    for file in target_files:
        path = file.get("path")
        patch = patches_by_path.get(path, "")
        # Cap patch text — diffs can be huge, the model doesn't need full file.
        patch_excerpt = patch[:2500]
        payload_files.append(
            {
                "path": path,
                "classification": file.get("classification"),
                "risk_score": file.get("risk_score"),
                "owners": file.get("owners", []),
                "patch_excerpt": patch_excerpt,
            }
        )

    user_prompt = (
        "Analyze the behavioral impact of the following changed files. Use the "
        "system-prompt schema for the output.\n\n"
        f"```json\n{json.dumps({'files': payload_files}, indent=2)}\n```"
    )

    result = call_llm_json(
        system=_SEMANTIC_DIFF_SYSTEM_PROMPT,
        user=user_prompt,
        temperature=0.0,
        max_tokens=1800,
    )
    if not result:
        return None

    deltas = result.get("behavioral_deltas") or []
    blast = result.get("blast_radius") or []
    if not isinstance(deltas, list) or not isinstance(blast, list):
        return None

    # Light validation: ensure each delta has required fields.
    cleaned_deltas: list[dict[str, Any]] = []
    for d in deltas:
        if not isinstance(d, dict) or not d.get("path"):
            continue
        cleaned_deltas.append(
            {
                "path": str(d.get("path", "")),
                "symbol": str(d.get("symbol") or d.get("path", "")),
                "old_behavior": str(d.get("old_behavior") or ""),
                "new_behavior": str(d.get("new_behavior") or ""),
                "divergent_input": d.get("divergent_input"),
                "severity": d.get("severity") if d.get("severity") in {"warn", "review_required"} else "warn",
                "confidence": float(d.get("confidence") or 0.7),
                "line_citations": d.get("line_citations") or [
                    {"path": d.get("path"), "range": "changed hunk"}
                ],
            }
        )

    cleaned_blast: list[dict[str, Any]] = []
    for b in blast:
        if not isinstance(b, dict) or not b.get("path"):
            continue
        cleaned_blast.append(
            {
                "path": str(b.get("path", "")),
                "owners": list(b.get("owners") or []),
                "downstream_services": list(b.get("downstream_services") or []),
                "direct_callers": list(b.get("direct_callers") or []),
                "impacted_tests": list(b.get("impacted_tests") or []),
                "confidence": float(b.get("confidence") or 0.6),
            }
        )

    return {"behavioral_deltas": cleaned_deltas, "blast_radius": cleaned_blast}


# ── Deterministic fallback helpers ─────────────────────────────────────────


def _old_behavior_fallback(file: dict[str, Any]) -> str:
    if file.get("deletions", 0) > file.get("additions", 0):
        return "Base behavior included removed branches, guards, or response fields."
    return "Base behavior followed the previous implementation for this symbol."


def _new_behavior_fallback(file: dict[str, Any], hits: list[str]) -> str:
    if {"payment", "billing", "refund", "charge"} & set(hits):
        return "Head behavior can change monetary side effects, refund flow, or billing state."
    if {"auth", "authorize", "token"} & set(hits):
        return "Head behavior can change authorization, session, or token handling."
    if "prompt" in hits or file.get("classification") == "prompt":
        return "Head behavior changes model instructions or agent workflow output."
    if "sql" in hits:
        return "Head behavior changes direct database access."
    return f"Head behavior changes {file.get('classification')} code in {file['path']}."


def _divergent_input_fallback(hits: list[str]) -> str | None:
    if {"payment", "billing", "refund", "charge"} & set(hits):
        return "Duplicate refund request with a missing or reused idempotency key."
    if {"auth", "authorize", "token"} & set(hits):
        return "User without the required role attempts the changed operation."
    if "prompt" in hits:
        return "Golden prompt that previously required strict JSON output."
    if "sql" in hits:
        return "Input missing tenant id or containing boundary characters."
    return "Boundary input around the changed branch."


def _blast_radius_fallback(file: dict[str, Any]) -> dict[str, Any]:
    path = file["path"]
    lower = path.lower()
    if any(token in lower for token in ["payment", "billing", "refund"]):
        services = ["payments", "finance-reporting"]
    elif "auth" in lower:
        services = ["identity", "api-gateway"]
    elif "prompt" in lower or "agent" in lower:
        services = ["ai-workflows"]
    else:
        services = ["application"]
    stem = path.split("/")[-1].split(".")[0]
    return {
        "path": path,
        "owners": file.get("owners", ["unassigned"]),
        "downstream_services": services,
        "direct_callers": [f"{stem} callers", f"{stem} exports"],
        "impacted_tests": [f"{stem}.test", f"{stem}.spec", f"{stem}_test.py"],
        "confidence": 0.62,
    }


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
