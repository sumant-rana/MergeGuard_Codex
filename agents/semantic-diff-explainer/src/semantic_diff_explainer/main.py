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
from packages.core.analysis_utils import risk_hits  # noqa: E402

AGENT_ID = "semantic-diff-explainer"

app = create_app(AGENT_ID, "Explain changed behavior and blast radius.")


@app.tool(is_local=False)
def extract_function_deltas(file: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract symbol-level behavior deltas and blast-radius signals from a changed file."""
    symbols = file.get("symbols") or [{"name": file["path"], "kind": "file", "confidence": 0.4}]
    hits = risk_hits(file["path"], " ".join(file.get("risk_reasons", [])))
    deltas = []
    for symbol in symbols[:4]:
        deltas.append(
            {
                "path": file["path"],
                "symbol": symbol.get("name"),
                "old_behavior": old_behavior(file),
                "new_behavior": new_behavior(file, hits),
                "divergent_input": divergent_input(hits),
                "severity": "review_required" if file.get("risk_score", 0) >= 60 else "warn",
                "confidence": min(0.9, symbol.get("confidence", 0.5) + 0.08),
                "line_citations": [{"path": file["path"], "range": "changed hunk"}],
            }
        )
    return deltas


def run(payload: dict[str, Any]) -> dict[str, Any]:
    compression = payload.get("prior_results", {}).get("review-compression", {}).get("output", {})
    files = compression.get("files", [])
    target_files = [
        file
        for file in files
        if file.get("classification") not in {"docs", "generated", "test"} and file.get("risk_score", 0) >= 20
    ]
    deltas = [delta for file in target_files for delta in extract_function_deltas(file)]
    blast_radius = [blast_radius_for(file) for file in target_files]
    output = {
        "behavioral_deltas": sorted(deltas, key=lambda item: item["severity"] != "review_required"),
        "blast_radius": blast_radius,
    }
    return make_agent_result(
        AGENT_ID,
        output,
        confidence=0.72,
        messages=[f"explained {len(deltas)} behavioral deltas"],
        trace=[{"step": "sandbox_semantic_diff", "files": len(target_files), "deltas": len(deltas)}],
    )


def old_behavior(file: dict[str, Any]) -> str:
    if file.get("deletions", 0) > file.get("additions", 0):
        return "Base behavior included removed branches, guards, or response fields."
    return "Base behavior followed the previous implementation for this symbol."


def new_behavior(file: dict[str, Any], hits: list[str]) -> str:
    if {"payment", "billing", "refund", "charge"} & set(hits):
        return "Head behavior can change monetary side effects, refund flow, or billing state."
    if {"auth", "authorize", "token"} & set(hits):
        return "Head behavior can change authorization, session, or token handling."
    if "prompt" in hits or file.get("classification") == "prompt":
        return "Head behavior changes model instructions or agent workflow output."
    if "sql" in hits:
        return "Head behavior changes direct database access."
    return f"Head behavior changes {file.get('classification')} code in {file['path']}."


def divergent_input(hits: list[str]) -> str | None:
    if {"payment", "billing", "refund", "charge"} & set(hits):
        return "Duplicate refund request with a missing or reused idempotency key."
    if {"auth", "authorize", "token"} & set(hits):
        return "User without the required role attempts the changed operation."
    if "prompt" in hits:
        return "Golden prompt that previously required strict JSON output."
    if "sql" in hits:
        return "Input missing tenant id or containing boundary characters."
    return "Boundary input around the changed branch."


def blast_radius_for(file: dict[str, Any]) -> dict[str, Any]:
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
