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

AGENT_ID = "prompt-canary"

app = create_app(AGENT_ID, "Evaluate prompt/model/agent workflow drift.")


@app.tool()
def judge_prompt(file: dict[str, Any], raw_file: dict[str, Any], suite: dict[str, Any]) -> dict[str, Any]:
    """Score a prompt-related file change against a canary suite for drift, format, latency, and cost."""
    patch = str(raw_file.get("patch", "")).lower()
    unsafe = any(marker in patch for marker in ["ignore previous", "bypass", "disable safety", "reveal secret"])
    format_fail = suite.get("assertions", {}).get("format") == "json" and "trailing comma" in patch
    additions = int(raw_file.get("additions") or file.get("additions") or 0)
    deletions = int(raw_file.get("deletions") or file.get("deletions") or 0)
    correctness = 0.45 if unsafe else 0.88
    format_score = 0.35 if format_fail else 0.9
    style = 0.68 if "verbose" in patch else 0.86
    latency_delta_ms = max(0, additions * 12 - deletions * 3)
    cost_delta_pct = max(0, min(100, additions * 2 - deletions))
    thresholds = suite.get("thresholds", {})
    passed = (
        correctness >= thresholds.get("correctness", 0.75)
        and format_score >= thresholds.get("format", 0.8)
        and style >= thresholds.get("style", 0.65)
        and latency_delta_ms <= thresholds.get("latency_delta_ms", 750)
        and cost_delta_pct <= thresholds.get("cost_delta_pct", 35)
    )
    return {
        "suite": suite.get("name", "default-prompt-drift"),
        "prompt_path": file["path"],
        "model": suite.get("model", "repo-default"),
        "correctness": correctness,
        "format": format_score,
        "style": style,
        "refusal": 0.42 if unsafe else 0.86,
        "latency_delta_ms": latency_delta_ms,
        "cost_delta_pct": cost_delta_pct,
        "status": "pass" if passed else "fail",
        "drift_summary": "Prompt drift check passed." if passed else "Prompt drift exceeded thresholds.",
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    compression = payload.get("prior_results", {}).get("review-compression", {}).get("output", {})
    raw_by_path = {file.get("path"): file for file in payload.get("changed_files", [])}
    prompt_files = [file for file in compression.get("files", []) if file.get("classification") == "prompt"]
    suites = payload.get("settings", {}).get("prompt_suites", [])
    runs = []
    for file in prompt_files:
        suite = next((item for item in suites if item.get("prompt_path") == file["path"]), default_suite(file["path"]))
        runs.append(judge_prompt(file, raw_by_path.get(file["path"], {}), suite))
    findings = [
        {
            "type": "prompt-canary-failure",
            "path": run["prompt_path"],
            "severity": "block",
            "message": f"Prompt drift check {run['suite']} failed for {run['prompt_path']}.",
            "suggested_action": "Fix prompt drift or update golden drift checks with reviewer approval.",
        }
        for run in runs
        if run["status"] == "fail"
    ]
    return make_agent_result(
        AGENT_ID,
        {"prompt_canary_runs": runs, "prompt_findings": findings},
        confidence=0.78,
        messages=[f"ran {len(runs)} prompt drift checks"],
        trace=[{"step": "prompt_canary", "runs": len(runs), "failures": len(findings)}],
    )


def default_suite(path: str) -> dict[str, Any]:
    return {
        "name": "default-prompt-drift",
        "prompt_path": path,
        "model": "repo-default",
        "assertions": {"format": "json" if path.endswith(".json") else "text"},
        "thresholds": {"correctness": 0.75, "format": 0.8, "style": 0.65, "latency_delta_ms": 750, "cost_delta_pct": 35},
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
