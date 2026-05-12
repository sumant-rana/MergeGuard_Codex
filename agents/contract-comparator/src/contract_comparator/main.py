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

AGENT_ID = "contract-comparator"

app = create_app(AGENT_ID, "Compare shape-only runtime contracts and propose property tests.")


@app.tool(is_local=False)
def diff_contract(contract: dict[str, Any]) -> dict[str, Any] | None:
    """Compare old and new contract shapes and return removed or changed-type fields."""
    old = contract.get("old") or contract.get("before") or {}
    new = contract.get("new") or contract.get("after") or {}
    removed = [key for key in old if key not in new]
    changed_types = [key for key in old if key in new and old[key] != new[key]]
    if not removed and not changed_types:
        return None
    path = contract["path"]
    return {
        "path": path,
        "symbol": contract.get("symbol"),
        "old_contract": old,
        "new_contract": new,
        "violated_assumption": "; ".join(
            part
            for part in [
                f"removed fields: {', '.join(removed)}" if removed else "",
                f"changed field types: {', '.join(changed_types)}" if changed_types else "",
            ]
            if part
        ),
        "severity": "review_required" if removed else "warn",
        "confidence": 0.84,
        "suggested_test": {
            "path": suggested_test_path(path),
            "framework": contract.get("framework", "repo-default"),
            "intent": "Assert backward-compatible runtime contract shape.",
        },
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    contracts = payload.get("settings", {}).get("contracts", [])
    findings = [finding for contract in contracts if (finding := diff_contract(contract))]
    return make_agent_result(
        AGENT_ID,
        {"contract_findings": findings, "suggested_tests": [item["suggested_test"] for item in findings]},
        confidence=0.82,
        messages=[f"compared {len(contracts)} runtime contracts"],
        trace=[{"step": "contract_compare", "contracts": len(contracts), "findings": len(findings)}],
    )


def suggested_test_path(path: str) -> str:
    if path.endswith(".py"):
        return path.replace(".py", "_contract_test.py")
    if path.endswith((".ts", ".tsx", ".js", ".jsx")):
        suffix = path.rsplit(".", 1)[-1]
        return path.rsplit(".", 1)[0] + f".contract.test.{suffix}"
    return f"{path}.contract.test"


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
