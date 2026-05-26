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
    has_paired_test,
    is_docs,
    is_generated,
    is_prompt,
    is_static_data_file,
    is_test,
    language_for,
    normalize_path,
    risk_hits,
    risk_hits_in_added_lines,
    saturate_risk,
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

    if is_generated(path, content):
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
    # `risk_score_raw` is the uncapped sum used for aggregation upstream; the
    # public `risk_score` is the saturated display value. Keeping both means
    # Phase-3 PR-level math can distinguish a "barely high" file from a
    # genuinely catastrophic one instead of treating every >=100 the same.
    risk_score_raw = (
        base_weight
        + min(22, changes // 8)
        + min(24, len(hits) * 6)
        + (10 if deletions > additions and deletions > 15 else 0)
    )
    risk_score = saturate_risk(risk_score_raw)
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
        "risk_score_raw": risk_score_raw,
        "risk_reasons": reasons,
        "owners": owners,
        "symbols": symbols,
        "must_inspect": risk_score >= 45 or classification in {"prompt", "security-sensitive"},
        "safe_to_skim": risk_score < 24 and classification in {"generated", "docs", "test", "wiring"},
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    settings = payload.get("settings", {})
    raw_changed_files = payload.get("changed_files", [])
    files = [classify_file(file, settings) for file in raw_changed_files]
    raw_by_path = {normalize_path(rf.get("path", "")): rf for rf in raw_changed_files}

    # Phase-2 downward credits: a file that already has changed-test coverage
    # or that is a pure static-data export shouldn't carry the same weight as
    # equivalent untested / behavioral code.
    test_paths = [file["path"] for file in files if file["classification"] == "test"]
    for file in files:
        if file["classification"] in {"logic", "security-sensitive", "prompt"} and has_paired_test(file["path"], test_paths):
            file["risk_score_raw"] = max(0, file["risk_score_raw"] - 10)
            file["risk_reasons"].append("paired test changed (-10)")
        raw_patch = str(raw_by_path.get(file["path"], {}).get("patch", ""))
        if is_static_data_file(file["path"], raw_patch):
            file["risk_score_raw"] = max(0, file["risk_score_raw"] - 12)
            file["risk_reasons"].append("static data export (-12)")
        file["risk_score"] = saturate_risk(file["risk_score_raw"])
        # Recompute the inspection flags after adjustment so a credited file
        # can drop out of must_inspect / into safe_to_skim.
        file["must_inspect"] = (
            file["risk_score"] >= 45
            or file["classification"] in {"prompt", "security-sensitive"}
        )
        file["safe_to_skim"] = (
            file["risk_score"] < 24
            and file["classification"] in {"generated", "docs", "test", "wiring"}
        )

    files.sort(key=lambda item: (-item["risk_score"], item["path"]))
    hotspots = [
        {
            "path": file["path"],
            "risk_score": file["risk_score"],
            "risk_score_raw": file["risk_score_raw"],
            "reason": "; ".join(file["risk_reasons"]),
            "owners": file["owners"],
            "required_action": required_action(file),
        }
        for file in files
        if file["must_inspect"]
    ]
    pr_risk = compute_pr_risk(files)
    output = {
        "files": files,
        "must_inspect": [file for file in files if file["must_inspect"]],
        "safe_to_skim": [file for file in files if file["safe_to_skim"]],
        "hotspots": hotspots[:15],
        "risk_score": pr_risk["display"],
        "risk_score_raw": pr_risk["raw"],
        "hotspot_themes": hotspot_themes(files),
        "owner_summary": owner_summary(files),
    }
    return make_agent_result(
        AGENT_ID,
        output,
        messages=[f"classified {len(files)} changed files"],
        trace=[{"step": "classify", "file_count": len(files)}],
    )


def compute_pr_risk(files: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate per-file scores into a PR-level risk number.

    Operates on the uncapped per-file ``risk_score_raw`` values and returns
    BOTH the raw sum (for upstream callers that want headroom above 100) and
    the saturated display value. Callers that only care about the legacy
    integer should use ``["display"]``.
    """
    if not files:
        return {"raw": 0, "display": 0}
    top_raws = sorted(
        (float(file.get("risk_score_raw", file["risk_score"])) for file in files),
        reverse=True,
    )[:5]
    tests_changed = any(file["classification"] == "test" for file in files)
    missing_evidence = (
        any(
            file["classification"] in {"logic", "security-sensitive", "prompt"}
            for file in files
        )
        and not tests_changed
    )
    raw = max(top_raws) * 0.65 + (sum(top_raws) / len(top_raws)) * 0.35
    if missing_evidence:
        raw += 14
    return {"raw": int(round(raw)), "display": saturate_risk(raw)}


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
