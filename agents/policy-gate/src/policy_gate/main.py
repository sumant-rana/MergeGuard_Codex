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
from packages.policies.engine import default_policy_pack, evaluate_policy_pack  # noqa: E402

AGENT_ID = "policy-gate"

app = create_app(AGENT_ID, "Evaluate policy guardrails against concept findings.")


def run(payload: dict[str, Any]) -> dict[str, Any]:
    concept_result = payload.get("prior_results", {}).get("concept-classifier", {}).get("output", {})
    concepts = concept_result.get("concept_findings", [])
    pack = payload.get("settings", {}).get("policy_pack") or default_policy_pack()
    findings = evaluate_policy_pack(pack, concepts)
    status = "block" if any(item["severity"] == "block" for item in findings) else "warn" if findings else "pass"
    suspend_payloads = [
        {
            "kind": "policy-override",
            "rule_id": finding["rule_id"],
            "question": f"Owner override required for {finding['message']}",
            "owner": finding["owner"],
        }
        for finding in findings
        if finding["severity"] == "block" and finding.get("override_allowed", True)
    ]
    return make_agent_result(
        AGENT_ID,
        {"policy_findings": findings, "policy_status": status, "suspend_payloads": suspend_payloads},
        status="completed",
        confidence=0.9,
        messages=[f"policy status: {status}"],
        trace=[{"step": "evaluate_policy", "findings": len(findings)}],
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
