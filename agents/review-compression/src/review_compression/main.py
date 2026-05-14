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
from packages.core.analysis_utils import (  # noqa: E402
    codeowners_for,
    extract_symbols,
    is_docs,
    is_generated,
    is_prompt,
    is_test,
    language_for,
    normalize_path,
    risk_hits,
    risk_hits_in_added_lines,
)

AGENT_ID = "review-compression"

app = create_app(AGENT_ID, "Triage changed files and route reviewer attention.")


@app.tool()
def classify_file(file: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Classify a changed file into category, risk signals, and required reviewer action."""
    path = normalize_path(file.get("path", ""))
    patch = str(file.get("patch", ""))
    content = str(file.get("content", ""))
    additions = int(file.get("additions") or 0)
    deletions = int(file.get("deletions") or 0)
    changes = int(file.get("changes") or additions + deletions)
    # Strict match: only flag a file as security-sensitive when a risk
    # keyword appears in lines the diff actually adds (or in the changed
    # file's content). Path-only matches gave false positives (e.g. files
    # under ``src/routes/_authenticated/`` getting tagged "auth-touching"
    # just because the directory name contains "auth").
    hits = risk_hits_in_added_lines(patch, content)

    if is_generated(path):
        classification = "generated"
    elif is_test(path):
        classification = "test"
    elif is_prompt(path):
        classification = "prompt"
    elif is_docs(path):
        classification = "docs"
    elif path.startswith((".github/", "infra/", "deploy/")) or path.endswith((".yml", ".yaml", ".json")):
        classification = "wiring"
    elif hits:
        classification = "security-sensitive"
    else:
        classification = "logic"

    base_weight = {
        "generated": 0,
        "docs": 3,
        "test": 6,
        "wiring": 14,
        "logic": 24,
        "prompt": 38,
        "security-sensitive": 44,
    }[classification]
    risk_score = min(
        100,
        base_weight
        + min(22, changes // 8)
        + min(24, len(hits) * 6)
        + (10 if deletions > additions and deletions > 15 else 0),
    )
    owners = codeowners_for(path, settings.get("codeowners", ""))
    symbols = extract_symbols(path, patch, content)
    reasons = []
    if hits:
        reasons.append(f"risk keywords: {', '.join(hits)}")
    reasons.append(f"classified as {classification}")
    if changes > 80:
        reasons.append("large changed-file surface")

    return {
        "path": path,
        "status": file.get("status", "modified"),
        "additions": additions,
        "deletions": deletions,
        "changes": changes,
        "language": language_for(path),
        "classification": classification,
        "risk_score": risk_score,
        "risk_reasons": reasons,
        "owners": owners,
        "symbols": symbols,
        "must_inspect": risk_score >= 45 or classification in {"prompt", "security-sensitive"},
        "safe_to_skim": risk_score < 24 and classification in {"generated", "docs", "test", "wiring"},
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    settings = payload.get("settings", {})
    files = [classify_file(file, settings) for file in payload.get("changed_files", [])]
    files.sort(key=lambda item: (-item["risk_score"], item["path"]))
    hotspots = [
        {
            "path": file["path"],
            "risk_score": file["risk_score"],
            "reason": "; ".join(file["risk_reasons"]),
            "owners": file["owners"],
            "required_action": required_action(file),
        }
        for file in files
        if file["must_inspect"]
    ]
    output = {
        "files": files,
        "must_inspect": [file for file in files if file["must_inspect"]],
        "safe_to_skim": [file for file in files if file["safe_to_skim"]],
        "hotspots": hotspots[:15],
        "risk_score": compute_pr_risk(files),
        "hotspot_themes": hotspot_themes(files),
        "owner_summary": owner_summary(files),
    }
    return make_agent_result(
        AGENT_ID,
        output,
        messages=[f"classified {len(files)} changed files"],
        trace=[{"step": "classify", "file_count": len(files)}],
    )


def compute_pr_risk(files: list[dict[str, Any]]) -> int:
    if not files:
        return 0
    top = sorted((file["risk_score"] for file in files), reverse=True)[:5]
    tests_changed = any(file["classification"] == "test" for file in files)
    missing_evidence = any(file["classification"] in {"logic", "security-sensitive", "prompt"} for file in files) and not tests_changed
    score = int(max(top) * 0.65 + (sum(top) / len(top)) * 0.35)
    if missing_evidence:
        score += 14
    return min(100, score)


def required_action(file: dict[str, Any]) -> str:
    if file["classification"] == "prompt":
        return "Run prompt drift checks and inspect prompt/model drift."
    if file["classification"] == "security-sensitive":
        return "Inspect behavior, authorization, failure modes, and evidence."
    return "Inspect changed behavior and verify tests."


def hotspot_themes(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for file in files:
        counts[file["classification"]] = counts.get(file["classification"], 0) + 1
        # Pull risk keywords from the per-file reasons we already computed
        # (which were derived from the strict added-line scanner). Don't
        # re-scan the path here.
        for hit in risk_hits(" ".join(file.get("risk_reasons", []))):
            counts[hit] = counts.get(hit, 0) + 1
    return [
        {"theme": theme, "count": count}
        for theme, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ][:10]


def owner_summary(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for file in files:
        for owner in file.get("owners", ["unassigned"]):
            counts[owner] = counts.get(owner, 0) + 1
    return [
        {"owner": owner, "file_count": count}
        for owner, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


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
