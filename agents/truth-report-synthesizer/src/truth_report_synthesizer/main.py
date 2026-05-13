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

AGENT_ID = "truth-report-synthesizer"

app = create_app(AGENT_ID, "Synthesize analyzer outputs into merge readiness and dashboard view.")


def run(payload: dict[str, Any]) -> dict[str, Any]:
    prior = payload.get("prior_results", {})
    compression = prior.get("review-compression", {}).get("output", {})
    evidence = prior.get("evidence-mapper", {}).get("output", {})
    semantic = prior.get("semantic-diff-explainer", {}).get("output", {})
    policy = prior.get("policy-gate", {}).get("output", {})
    prompt = prior.get("prompt-canary", {}).get("output", {})
    contracts = prior.get("contract-comparator", {}).get("output", {})
    slop = prior.get("slop-detector", {}).get("output", {})
    memory = prior.get("semantic-evidence-agent", {}).get("output", {})
    test_coverage = prior.get("test-coverage-validator", {}).get("output", {})

    risk_score = min(
        100,
        compression.get("risk_score", 0)
        + len(evidence.get("missing_evidence_findings", [])) * 8
        + len(policy.get("policy_findings", [])) * 12
        + len(prompt.get("prompt_findings", [])) * 16
        + len(contracts.get("contract_findings", [])) * 10
        + len(slop.get("slop_findings", [])) * 6
        + len(memory.get("memory_findings", [])) * 4
        + len(test_coverage.get("coverage_findings", [])) * 9
        + (10 if test_coverage.get("coverage_status") == "blocked" else 0),
    )
    blockers = collect_blockers(evidence, policy, prompt, contracts, slop, memory, test_coverage)
    status = "blocked" if any(item.get("severity") == "block" for item in blockers) else "review" if blockers or risk_score >= 45 else "pass"
    top_blocker = blockers[0]["message"] if blockers else None
    next_action = blockers[0].get("suggested_action") if blockers else "Proceed with normal review."
    summary = {
        "risk_score": risk_score,
        "status": status,
        "top_blocker": top_blocker,
        "next_action": next_action,
        "hotspots": compression.get("hotspots", []),
        "must_inspect": compression.get("must_inspect", []),
        "safe_to_skim": compression.get("safe_to_skim", []),
        "intent_items": prior.get("intent-extractor", {}).get("output", {}).get("intent_items", []),
        "evidence_links": evidence.get("evidence_links", []),
        "missing_evidence_findings": evidence.get("missing_evidence_findings", []),
        "behavioral_deltas": semantic.get("behavioral_deltas", []),
        "blast_radius": semantic.get("blast_radius", []),
        "concept_findings": prior.get("concept-classifier", {}).get("output", {}).get("concept_findings", []),
        "policy_findings": policy.get("policy_findings", []),
        "prompt_canary_runs": prompt.get("prompt_canary_runs", []),
        "prompt_findings": prompt.get("prompt_findings", []),
        "contract_findings": contracts.get("contract_findings", []),
        "slop": slop,
        "slop_score": slop.get("slop_score"),
        "slop_findings": slop.get("slop_findings", []),
        "remove_candidates": slop.get("remove_candidates", []),
        "rework_candidates": slop.get("rework_candidates", []),
        "semantic_memory": memory,
        "memory_matches": memory.get("semantic_matches", []),
        "memory_evidence": memory.get("requirement_evidence", []),
        "related_tests": memory.get("related_tests", []),
        "similar_prs": memory.get("similar_prs", []),
        "memory_findings": memory.get("memory_findings", []),
        "test_coverage": test_coverage,
        "test_coverage_score": test_coverage.get("coverage_score"),
        "test_coverage_findings": test_coverage.get("coverage_findings", []),
        "test_coverage_matrix": test_coverage.get("coverage_matrix", []),
        "suggested_tests": [
            *contracts.get("suggested_tests", []),
            *memory.get("recommended_test_updates", []),
            *[
                {"path": item["path"], "framework": "repo-default", "intent": item["suggested_action"]}
                for item in evidence.get("missing_evidence_findings", [])
            ],
            *test_coverage.get("recommendations", []),
        ],
        "owner_summary": compression.get("owner_summary", []),
        "hotspot_themes": compression.get("hotspot_themes", []),
        "checks": build_checks(status, prior),
        "comment": render_comment(status, risk_score, top_blocker, next_action, compression, blockers),
    }
    return make_agent_result(
        AGENT_ID,
        {"summary": summary},
        confidence=0.86,
        messages=["synthesized truth report"],
        trace=[{"step": "truth_report", "status": status, "risk_score": risk_score}],
    )


def collect_blockers(*sections: dict[str, Any]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    keys = [
        "missing_evidence_findings",
        "policy_findings",
        "prompt_findings",
        "contract_findings",
        "slop_findings",
        "memory_findings",
        "coverage_findings",
    ]
    for section in sections:
        for key in keys:
            blockers.extend(section.get(key, []))
    return sorted(blockers, key=lambda item: 0 if item.get("severity") == "block" else 1)


def build_checks(status: str, prior: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = prior.get("evidence-mapper", {}).get("output", {})
    semantic = prior.get("semantic-diff-explainer", {}).get("output", {})
    policy = prior.get("policy-gate", {}).get("output", {})
    prompt = prior.get("prompt-canary", {}).get("output", {})
    contracts = prior.get("contract-comparator", {}).get("output", {})
    slop = prior.get("slop-detector", {}).get("output", {})
    memory = prior.get("semantic-evidence-agent", {}).get("output", {})
    test_coverage = prior.get("test-coverage-validator", {}).get("output", {})
    return [
        {"name": "MergeGuard / Change Triage", "conclusion": "failure" if status == "blocked" else "neutral" if status == "review" else "success"},
        {"name": "MergeGuard / Requirement Match", "conclusion": "neutral" if any(link["evidence_status"] == "missing" for link in evidence.get("evidence_links", [])) else "success"},
        {"name": "MergeGuard / Repository Memory", "conclusion": memory_conclusion(memory)},
        {"name": "MergeGuard / Verification Evidence", "conclusion": "neutral" if evidence.get("missing_evidence_findings") else "success"},
        {"name": "MergeGuard / Test Coverage", "conclusion": test_coverage_conclusion(test_coverage)},
        {"name": "MergeGuard / Behavior Impact", "conclusion": "neutral" if semantic.get("behavioral_deltas") else "success"},
        {"name": "MergeGuard / Policy Guardrails", "conclusion": "failure" if any(item["severity"] == "block" for item in policy.get("policy_findings", [])) else "neutral" if policy.get("policy_findings") else "success"},
        {"name": "MergeGuard / Prompt Drift Check", "conclusion": "failure" if prompt.get("prompt_findings") else "success"},
        {"name": "MergeGuard / Runtime Contracts", "conclusion": "neutral" if contracts.get("contract_findings") else "success"},
        {"name": "MergeGuard / Slop Detector", "conclusion": slop_conclusion(slop)},
    ]


def test_coverage_conclusion(test_coverage: dict[str, Any]) -> str:
    status = test_coverage.get("coverage_status")
    if status == "blocked":
        return "failure"
    if status == "review" or test_coverage.get("coverage_findings"):
        return "neutral"
    return "success"


def memory_conclusion(memory: dict[str, Any]) -> str:
    if not memory:
        return "neutral"
    if memory.get("memory_findings"):
        return "neutral"
    if memory.get("related_tests") or memory.get("semantic_matches"):
        return "success"
    return "neutral"


def slop_conclusion(slop: dict[str, Any]) -> str:
    if not slop:
        return "neutral"
    if any(item.get("severity") == "block" for item in slop.get("slop_findings", [])):
        return "failure"
    if slop.get("slop_findings"):
        return "neutral"
    return "success"


def render_comment(
    status: str,
    risk_score: int,
    top_blocker: str | None,
    next_action: str | None,
    compression: dict[str, Any],
    blockers: list[dict[str, Any]],
) -> str:
    hotspots = "\n".join(f"- `{item['path']}` risk {item['risk_score']}: {item['reason']}" for item in compression.get("hotspots", [])[:5])
    blockers_text = "\n".join(f"- {item['message']}" for item in blockers[:5]) or "- None"
    return (
        "<!-- mergeguard:comment -->\n"
        "## MergeGuard Truth Report\n\n"
        f"**Readiness:** {status.upper()} · **Risk:** {risk_score}/100\n\n"
        f"**Top blocker:** {top_blocker or 'None'}\n\n"
        f"**Next action:** {next_action or 'Proceed with normal review.'}\n\n"
        "### Hotspots\n"
        f"{hotspots or '- No hotspots'}\n\n"
        "### Blockers And Evidence Gaps\n"
        f"{blockers_text}\n"
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
