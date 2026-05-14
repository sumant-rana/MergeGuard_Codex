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
    register_default_llm,
    register_entrypoint,
)

AGENT_ID = "truth-report-synthesizer"

app = create_app(AGENT_ID, "Synthesize analyzer outputs into merge readiness and dashboard view.")
# Register a Grove-pointed LangChain LLM with the Magenta runtime so calls
# show up as traces in the playground. No-op when langchain isn't
# available (in-process shim mode on the host).
register_default_llm(app)


def run(payload: dict[str, Any]) -> dict[str, Any]:
    prior = payload.get("prior_results", {})
    compression = prior.get("review-compression", {}).get("output", {})
    evidence = prior.get("evidence-mapper", {}).get("output", {})
    semantic = prior.get("semantic-diff-explainer", {}).get("output", {})
    concept_classifier = prior.get("concept-classifier", {}).get("output", {})
    policy = prior.get("policy-gate", {}).get("output", {})
    prompt = prior.get("prompt-canary", {}).get("output", {})
    contracts = prior.get("contract-comparator", {}).get("output", {})
    slop = prior.get("slop-detector", {}).get("output", {})
    memory = prior.get("semantic-evidence-agent", {}).get("output", {})
    test_coverage = prior.get("test-coverage-validator", {}).get("output", {})

    concept_findings = concept_classifier.get("concept_findings", [])
    block_concepts = [c for c in concept_findings if c.get("severity") == "block"]

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
        + (10 if test_coverage.get("coverage_status") == "blocked" else 0)
        + len(block_concepts) * 22   # secret/auth/injection findings dominate scoring
        + len([c for c in concept_findings if c.get("severity") == "review_required"]) * 4,
    )
    blockers = collect_blockers(
        evidence, policy, prompt, contracts, slop, memory, test_coverage,
        {"concept_findings": _concept_findings_as_blockers(concept_findings)},
    )
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
        "comment": render_comment(
            status, risk_score, top_blocker, next_action, compression, blockers,
            behavioral_deltas=semantic.get("behavioral_deltas", []),
            intent_items=prior.get("intent-extractor", {}).get("output", {}).get("intent_items", []),
        ),
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
        "concept_findings",
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
    # Sort: block-severity first, then by source priority so secrets/auth
    # bypass beat generic policy or missing-tests when they tie.
    source_priority = {
        "concept-classifier": 0,
        "policy-gate": 1,
        "slop-detector": 2,
        "evidence-mapper": 3,
        "prompt-canary": 4,
        "contract-comparator": 5,
        "test-coverage-validator": 6,
        "semantic-evidence-agent": 7,
    }
    return sorted(
        blockers,
        key=lambda item: (
            0 if item.get("severity") == "block" else 1,
            source_priority.get(item.get("source_agent", ""), 99),
        ),
    )


def _concept_findings_as_blockers(concept_findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape concept-classifier output so it slots into the blocker list cleanly."""
    out: list[dict[str, Any]] = []
    for finding in concept_findings:
        if finding.get("severity") not in {"block", "review_required"}:
            continue
        # Concept-classifier already attaches `message` + `suggested_action`;
        # we just tag the source so downstream renderers can group by agent.
        out.append({**finding, "source_agent": "concept-classifier"})
    return out


def build_checks(status: str, prior: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the per-agent check cards shown on the Overview tab.

    Each check carries a ``conclusion`` (success / neutral / failure → drives
    the pill color) and a one-line ``summary`` that explains the verdict in
    concrete terms the reviewer can act on.
    """
    review = prior.get("review-compression", {}).get("output", {})
    evidence = prior.get("evidence-mapper", {}).get("output", {})
    semantic = prior.get("semantic-diff-explainer", {}).get("output", {})
    policy = prior.get("policy-gate", {}).get("output", {})
    prompt = prior.get("prompt-canary", {}).get("output", {})
    contracts = prior.get("contract-comparator", {}).get("output", {})
    slop = prior.get("slop-detector", {}).get("output", {})
    memory = prior.get("semantic-evidence-agent", {}).get("output", {})
    test_coverage = prior.get("test-coverage-validator", {}).get("output", {})

    hotspots = review.get("hotspots", []) or []
    file_count = len(review.get("files", []) or [])
    missing_evidence_links = [
        link for link in evidence.get("evidence_links", []) or [] if link.get("evidence_status") == "missing"
    ]
    partial_evidence_links = [
        link for link in evidence.get("evidence_links", []) or [] if link.get("evidence_status") == "partial"
    ]
    missing_test_findings = evidence.get("missing_evidence_findings", []) or []
    behavioral_deltas = semantic.get("behavioral_deltas", []) or []
    policy_findings = policy.get("policy_findings", []) or []
    policy_status = policy.get("policy_status") or ("block" if policy_findings else "pass")
    prompt_findings = prompt.get("prompt_findings", []) or []
    prompt_runs = prompt.get("prompt_canary_runs", []) or []
    contract_findings = contracts.get("contract_findings", []) or []
    suggested_test_count = len(contracts.get("suggested_tests", []) or [])
    slop_findings = slop.get("slop_findings", []) or []
    slop_remove = slop.get("remove_candidates", []) or []
    slop_rework = slop.get("rework_candidates", []) or []
    semantic_matches = memory.get("semantic_matches", []) or []
    related_tests = memory.get("related_tests", []) or []
    similar_prs = memory.get("similar_prs", []) or []
    coverage_score = test_coverage.get("coverage_score")
    coverage_findings = test_coverage.get("coverage_findings", []) or []
    coverage_status = test_coverage.get("coverage_status") or "unknown"

    return [
        {
            "name": "MergeGuard / Change Triage",
            "conclusion": "failure" if status == "blocked" else "neutral" if status == "review" else "success",
            "summary": (
                f"{file_count} file(s) classified, "
                f"{len(hotspots)} hotspot(s) — top risk {hotspots[0]['risk_score']} on `{hotspots[0]['path']}`"
                if hotspots else f"{file_count} file(s) classified, no hotspots above threshold"
            ),
        },
        {
            "name": "MergeGuard / Requirement Match",
            "conclusion": "neutral" if missing_evidence_links else "success",
            "summary": (
                f"{len(missing_evidence_links)} intent(s) missing evidence, "
                f"{len(partial_evidence_links)} partial"
                if missing_evidence_links or partial_evidence_links
                else f"All {len(evidence.get('evidence_links', []) or [])} intent(s) backed by changed files"
            ),
        },
        {
            "name": "MergeGuard / Repository Memory",
            "conclusion": memory_conclusion(memory),
            "summary": (
                f"{len(semantic_matches)} match(es): "
                f"{len(related_tests)} test(s), {len(similar_prs)} prior PR(s)"
                if semantic_matches
                else "No repository memory retrieved"
            ),
        },
        {
            "name": "MergeGuard / Verification Evidence",
            "conclusion": "neutral" if missing_test_findings else "success",
            "summary": (
                f"{len(missing_test_findings)} file(s) lack changed-test evidence"
                if missing_test_findings
                else "Changed-test evidence found for all risky files"
            ),
        },
        {
            "name": "MergeGuard / Test Coverage",
            "conclusion": test_coverage_conclusion(test_coverage),
            "summary": (
                f"Coverage {coverage_score}% ({coverage_status}) — {len(coverage_findings)} gap(s) flagged"
                if coverage_score is not None
                else "Coverage not evaluated"
            ),
        },
        {
            "name": "MergeGuard / Behavior Impact",
            "conclusion": "neutral" if behavioral_deltas else "success",
            "summary": (
                f"{len(behavioral_deltas)} behavioral delta(s) detected, "
                f"{sum(1 for d in behavioral_deltas if d.get('severity') == 'review_required')} require review"
                if behavioral_deltas
                else "No behavioral deltas detected"
            ),
        },
        {
            "name": "MergeGuard / Policy Guardrails",
            "conclusion": "failure" if any(item.get("severity") == "block" for item in policy_findings)
                          else "neutral" if policy_findings else "success",
            "summary": (
                f"{len(policy_findings)} policy violation(s): "
                + ", ".join(f.get("rule_id", "rule") for f in policy_findings[:3])
                if policy_findings
                else f"Policy status: {policy_status} — 0 violations"
            ),
        },
        {
            "name": "MergeGuard / Prompt Drift Check",
            "conclusion": "failure" if prompt_findings else "success",
            "summary": (
                f"{len(prompt_findings)} prompt drift finding(s) across {len(prompt_runs)} canary run(s)"
                if prompt_findings
                else f"{len(prompt_runs)} canary run(s) passed — no prompt drift"
                if prompt_runs
                else "No prompt files in this PR"
            ),
        },
        {
            "name": "MergeGuard / Runtime Contracts",
            "conclusion": "neutral" if contract_findings else "success",
            "summary": (
                f"{len(contract_findings)} contract change(s); "
                f"{suggested_test_count} suggested test(s)"
                if contract_findings
                else "No runtime contract changes"
            ),
        },
        {
            "name": "MergeGuard / Slop Detector",
            "conclusion": slop_conclusion(slop),
            "summary": (
                f"{len(slop_findings)} slop finding(s): "
                f"{len(slop_remove)} to remove, {len(slop_rework)} to rework"
                if slop_findings
                else "No slop detected"
            ),
        },
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
    *,
    behavioral_deltas: list[dict[str, Any]] | None = None,
    intent_items: list[dict[str, Any]] | None = None,
) -> str:
    """Produce the reviewer-facing PR comment.

    LLM-first: synthesizes a focused, plain-English summary over the
    structured agent outputs. Falls back to the deterministic template if
    the LLM is unavailable or returns a malformed response — the dashboard
    always renders something useful.
    """
    if llm_available():
        llm_text = _render_comment_via_llm(
            status=status,
            risk_score=risk_score,
            top_blocker=top_blocker,
            next_action=next_action,
            compression=compression,
            blockers=blockers,
            behavioral_deltas=behavioral_deltas or [],
            intent_items=intent_items or [],
        )
        if llm_text:
            return llm_text
    return _render_comment_template(
        status, risk_score, top_blocker, next_action, compression, blockers,
    )


def _render_comment_template(
    status: str,
    risk_score: int,
    top_blocker: str | None,
    next_action: str | None,
    compression: dict[str, Any],
    blockers: list[dict[str, Any]],
) -> str:
    hotspots = "\n".join(
        f"- `{item['path']}` risk {item['risk_score']}: {item['reason']}"
        for item in compression.get("hotspots", [])[:5]
    )
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


_TRUTH_REPORT_SYSTEM_PROMPT = (
    "You are MergeGuard's senior reviewer assistant. You write concise, "
    "actionable pull-request review summaries in GitHub-flavored markdown.\n\n"
    "Rules:\n"
    "- Lead with merge readiness and the single most important blocker. No fluff.\n"
    "- Quote specific file paths in backticks when referencing them.\n"
    "- Do NOT invent issues that aren't in the structured inputs. If a section\n"
    "  has no items, say so or omit it.\n"
    "- Keep the whole comment under ~300 words.\n"
    "- Output a single JSON object: {\"comment_markdown\": \"...\"}.\n"
    "- Inside comment_markdown, start with `<!-- mergeguard:comment -->`\n"
    "  followed by `## MergeGuard Truth Report` then your content.\n"
)


def _render_comment_via_llm(
    *,
    status: str,
    risk_score: int,
    top_blocker: str | None,
    next_action: str | None,
    compression: dict[str, Any],
    blockers: list[dict[str, Any]],
    behavioral_deltas: list[dict[str, Any]],
    intent_items: list[dict[str, Any]],
) -> str | None:
    structured = {
        "readiness": status,
        "risk_score": risk_score,
        "top_blocker": top_blocker,
        "next_action": next_action,
        "hotspots": compression.get("hotspots", [])[:8],
        "blockers": [
            {
                "message": b.get("message"),
                "severity": b.get("severity"),
                "suggested_action": b.get("suggested_action"),
                "source_agent": b.get("source_agent"),
            }
            for b in blockers[:10]
        ],
        "behavioral_deltas": behavioral_deltas[:6],
        "intent_items": [
            {"text": i.get("text"), "category": i.get("category")}
            for i in intent_items[:8]
        ],
    }
    user_prompt = (
        "Synthesize the following MergeGuard analysis into a reviewer-facing "
        "comment. Use the schema in your system prompt.\n\n"
        f"```json\n{json.dumps(structured, indent=2)}\n```"
    )
    result = call_llm_json(
        app=app,
        system=_TRUTH_REPORT_SYSTEM_PROMPT,
        user=user_prompt,
        temperature=0.0,
        max_tokens=900,
    )
    if not result:
        return None
    text = result.get("comment_markdown")
    if not isinstance(text, str) or not text.strip():
        return None
    return text


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
