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
    policy = prior.get("policy-gate", {}).get("output", {})
    prompt = prior.get("prompt-canary", {}).get("output", {})
    contracts = prior.get("contract-comparator", {}).get("output", {})
    slop = prior.get("slop-detector", {}).get("output", {})
    memory = prior.get("semantic-evidence-agent", {}).get("output", {})
    test_coverage = prior.get("test-coverage-validator", {}).get("output", {})
    concept_classifier = prior.get("concept-classifier", {}).get("output", {})

    intent_items = prior.get("intent-extractor", {}).get("output", {}).get("intent_items", [])
    evidence_links = evidence.get("evidence_links", [])
    missing_should_intents = _missing_should_intents(intent_items, evidence_links)
    violated_intents = _violated_intents(evidence_links)
    concept_findings = concept_classifier.get("concept_findings", [])
    block_concepts = [c for c in concept_findings if c.get("severity") == "block"]

    # Risk score — re-balanced 2026-Q1: a missed acceptance criterion
    # (`should` intent with status `missing`) is the single most
    # reviewer-relevant signal, so it carries real weight. Test-coverage
    # gaps are noise compared to that.
    risk_score = min(
        100,
        compression.get("risk_score", 0)
        + len(missing_should_intents) * 18              # missed acceptance criteria
        + len(violated_intents) * 22                    # must_not violations
        + len(block_concepts) * 22                      # secret / auth bypass / etc.
        + len(policy.get("policy_findings", [])) * 12
        + len(prompt.get("prompt_findings", [])) * 16
        + len(contracts.get("contract_findings", [])) * 10
        + len(slop.get("slop_findings", [])) * 6
        + len(memory.get("memory_findings", [])) * 4
        + len(test_coverage.get("coverage_findings", [])) * 6
        + len(evidence.get("missing_evidence_findings", [])) * 4
        + (10 if test_coverage.get("coverage_status") == "blocked" else 0)
        + len([c for c in concept_findings if c.get("severity") == "review_required"]) * 4,
    )
    blockers = collect_blockers(evidence, policy, prompt, contracts, slop, memory, test_coverage)
    # Strip out generic per-intent "not covered" findings — the Intent vs
    # Implementation table already shows the same information.
    blockers = [
        b for b in blockers
        if not _is_redundant_intent_finding(b)
    ]
    # Promote missed-acceptance-criteria + must_not violations into the
    # blocker list so they outrank partial-coverage findings.
    blockers = _promote_intent_gaps(missing_should_intents, violated_intents) + blockers

    has_block_severity = any(item.get("severity") == "block" for item in blockers)
    if has_block_severity or risk_score >= 80 or violated_intents:
        status = "blocked"
    elif blockers or missing_should_intents or risk_score >= 40:
        status = "review"
    else:
        status = "pass"

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
            evidence_links=evidence.get("evidence_links", []),
            missing_evidence_findings=evidence.get("missing_evidence_findings", []),
        ),
    }
    return make_agent_result(
        AGENT_ID,
        {"summary": summary},
        confidence=0.86,
        messages=["synthesized truth report"],
        trace=[{"step": "truth_report", "status": status, "risk_score": risk_score}],
    )


def _missing_should_intents(
    intent_items: list[dict[str, Any]],
    evidence_links: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Intent items the diff DIDN'T cover (status='missing', category='should').

    These are the most reviewer-relevant gaps — a stated acceptance criterion
    that the implementation skipped.
    """
    status_by_id = {
        link.get("intent_id"): link.get("evidence_status")
        for link in evidence_links
    }
    out: list[dict[str, Any]] = []
    for item in intent_items:
        if (item.get("category") or "should") != "should":
            continue
        if status_by_id.get(item.get("id")) != "missing":
            continue
        out.append(item)
    return out


def _violated_intents(
    evidence_links: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """must_not constraints the diff appears to have violated."""
    return [
        link for link in evidence_links
        if link.get("evidence_status") == "violated"
    ]


_INTENT_DEDUP_PHRASES = (
    "not covered by changed tests",
    "is only partially supported",
    "is only partial",
    "partial changed-test coverage",
    "has partial changed-test coverage",
    "has missing changed-test coverage",
    "missing changed-test coverage",
    "intent is not covered by changed tests",
)


def _is_redundant_intent_finding(blocker: dict[str, Any]) -> bool:
    """Drop generic 'intent-N not covered / partially supported' findings —
    the Intent vs Implementation table already surfaces the same information.

    Without this filter, the same gap shows up twice: once as a 🟡 Partial / ❌
    Missing row in the Intent table, and again as a 'Review-required finding'
    in the findings section — which then incorrectly becomes the top blocker.
    """
    path = str(blocker.get("path") or "")
    msg = str(blocker.get("message") or "").lower()
    if path.startswith("intent-"):
        return True
    return any(phrase in msg for phrase in _INTENT_DEDUP_PHRASES)


def _promote_intent_gaps(
    missing_should: list[dict[str, Any]],
    violated: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build first-class blocker entries from the most actionable intent gaps.

    Missed `should` items rank ABOVE violated `must_not` items here because a
    missed-acceptance-criterion is what most reviewers actually act on first
    (the must_not violation also gets surfaced via the block-severity path).
    """
    promoted: list[dict[str, Any]] = []
    for item in missing_should[:3]:
        promoted.append({
            "severity": "review_required",
            "source_agent": "intent-extractor",
            "message": (
                f"Acceptance criterion is not implemented: "
                f"\"{_shorten(item.get('text', ''), 110)}\""
            ),
            "suggested_action": (
                f"Implement the missing acceptance criterion or split it into a follow-up "
                f"and update the linked issue to remove it from this PR's scope."
            ),
            "path": item.get("id"),
        })
    for link in violated[:3]:
        promoted.append({
            "severity": "block",
            "source_agent": "intent-extractor",
            "message": (
                f"Constraint appears violated: \"{_shorten(link.get('intent_text', ''), 110)}\""
            ),
            "suggested_action": link.get("suggested_action") or "Confirm intent or back the change out.",
            "path": (link.get("mapped_paths") or [None])[0],
        })
    return promoted


def _shorten(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


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
    evidence_links: list[dict[str, Any]] | None = None,
    missing_evidence_findings: list[dict[str, Any]] | None = None,
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
            evidence_links=evidence_links or [],
            missing_evidence_findings=missing_evidence_findings or [],
        )
        if llm_text:
            return llm_text
    return _render_comment_template(
        status,
        risk_score,
        top_blocker,
        next_action,
        compression,
        blockers,
        intent_items or [],
        evidence_links or [],
        missing_evidence_findings or [],
        behavioral_deltas or [],
    )


# ── Status banner helpers ──────────────────────────────────────────────────

_STATUS_BANNER: dict[str, tuple[str, str]] = {
    "pass":    ("✅", "Ready for merge"),
    "review":  ("🟡", "Review required"),
    "blocked": ("⛔", "Blocked"),
}


def _status_banner(status: str, risk_score: int) -> str:
    icon, label = _STATUS_BANNER.get(status, ("🟡", "Review required"))
    return f"### {icon} {label} · Risk **{risk_score}/100**"


_INTENT_STATUS_EMOJI: dict[str, str] = {
    # `should` category outcomes
    "proven":          "✅ Covered",
    "partial":         "🟡 Partial",
    "missing":         "❌ Missing",
    # `must_not` category outcomes
    "compliant":       "✅ Compliant",
    "violated":        "🚫 Violated",
    # `out_of_scope` category outcomes
    "respected":       "⚪ Respected",
    "in_scope_creep":  "⚠️ Scope creep",
}


def _intent_status(item: dict[str, Any], evidence_links: list[dict[str, Any]]) -> str:
    """Map an intent item to a status pill.

    The pill text depends on the intent's category — a ``must_not`` intent
    that the diff doesn't violate should read "✅ Compliant", not "❌ Missing".
    """
    intent_id = item.get("id")
    link = next(
        (link for link in evidence_links if link.get("intent_id") == intent_id),
        None,
    )
    if not link:
        # No evidence-mapper entry yet — fall back to category-aware default.
        category = (item.get("category") or "should").lower()
        if category == "must_not":
            return "✅ Compliant"
        if category == "out_of_scope":
            return "⚪ Respected"
        return "❓ Not evaluated"
    return _INTENT_STATUS_EMOJI.get(
        link.get("evidence_status", ""),
        "❓ Unknown",
    )


def _render_intent_vs_implementation(
    intent_items: list[dict[str, Any]],
    evidence_links: list[dict[str, Any]],
) -> str:
    """Build the Intent vs Implementation table.

    Surfaces, per intent item:
      - the original "should / must not / out of scope" claim,
      - the source (linked-issue body vs PR text),
      - whether the diff + tests actually cover it.

    Long claims wrap inside the table cell via ``<br>`` rather than being
    truncated — GitHub renders ``<br>`` in markdown tables fine and the
    reviewer wants to read the whole sentence.
    """
    if not intent_items:
        return ""
    rows = []
    for item in intent_items[:12]:
        text = _format_table_claim(item.get("text") or "")
        category = str(item.get("category") or "should").replace("_", " ")
        source_emoji = (
            "📋" if item.get("source") == "linked_issue" else "📝"
        )
        rows.append(
            f"| {source_emoji} | {category} | {text} | {_intent_status(item, evidence_links)} |"
        )
    return (
        "### Intent vs Implementation\n\n"
        "_📋 from linked issue · 📝 from PR text · "
        "✅ Covered / Compliant · ⚪ Respected · 🟡 Partial · "
        "❌ Missing · 🚫 Violated · ⚠️ Scope creep_\n\n"
        "| | Kind | Claim | Evidence |\n"
        "|---|------|-------|----------|\n"
        + "\n".join(rows)
        + "\n"
    )


def _format_table_claim(text: str) -> str:
    """Escape markdown specials + wrap long claims with ``<br>`` inside a cell.

    Hard cap at 240 chars so an essay-length claim doesn't dominate the
    table, but otherwise no `…` truncation — break on sentence boundaries.
    """
    text = str(text or "").strip().replace("|", "\\|").replace("\n", " ")
    if len(text) > 240:
        text = text[:237] + "…"
    if len(text) <= 90:
        return text
    # Wrap to ~90-char lines at the nearest word boundary.
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= 90:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return "<br>".join(lines)


def _render_findings_block(
    title: str,
    items: list[dict[str, Any]],
    *,
    emoji: str,
    limit: int = 8,
) -> str:
    if not items:
        return ""
    bullets = []
    for finding in items[:limit]:
        path = finding.get("path")
        msg = finding.get("message") or finding.get("reason") or ""
        bullets.append(
            f"- {emoji} `{path}` — {msg}" if path else f"- {emoji} {msg}"
        )
    more = ""
    if len(items) > limit:
        more = f"\n\n_…and {len(items) - limit} more._"
    return (
        f"<details open><summary><strong>{title}</strong> ({len(items)})</summary>\n\n"
        + "\n".join(bullets)
        + more
        + "\n\n</details>\n"
    )


def _render_hotspots(compression: dict[str, Any]) -> str:
    hotspots = compression.get("hotspots", [])[:5]
    if not hotspots:
        return ""
    bullets = "\n".join(
        f"- `{item['path']}` — risk **{item['risk_score']}**: {item.get('reason') or ''}"
        for item in hotspots
    )
    return (
        "<details><summary><strong>Hotspots</strong> "
        f"({len(compression.get('hotspots', []))})</summary>\n\n"
        + bullets
        + "\n\n</details>\n"
    )


def _render_comment_template(
    status: str,
    risk_score: int,
    top_blocker: str | None,
    next_action: str | None,
    compression: dict[str, Any],
    blockers: list[dict[str, Any]],
    intent_items: list[dict[str, Any]],
    evidence_links: list[dict[str, Any]],
    missing_evidence_findings: list[dict[str, Any]],
    behavioral_deltas: list[dict[str, Any]],
) -> str:
    block_findings = [b for b in blockers if b.get("severity") == "block"]
    review_findings = [
        b for b in blockers
        if b.get("severity") in {"review_required", "warn"}
    ]
    files_changed = len(compression.get("files", []) or [])
    intent_count = len(intent_items)
    # Count any "good outcome" — proven for should-items, compliant for
    # must_not, respected for out_of_scope.
    _good = {"✅ Covered", "✅ Compliant", "⚪ Respected"}
    covered_intent = sum(
        1
        for item in intent_items
        if _intent_status(item, evidence_links) in _good
    )

    summary_table = (
        "| Status | Risk | Blockers | Review items | Files | Intent covered |\n"
        "|---|---|---|---|---|---|\n"
        f"| `{status}` | **{risk_score}/100** | {len(block_findings)} | "
        f"{len(review_findings)} | {files_changed} | {covered_intent}/{intent_count} |"
    )

    sections: list[str] = [
        "<!-- mergeguard:sticky -->",
        "<!-- mergeguard:comment -->",
        "## MergeGuard Truth Report",
        "",
        _status_banner(status, risk_score),
        "",
        summary_table,
        "",
    ]

    if top_blocker:
        sections += [
            f"> **Top blocker.** {top_blocker}",
            "",
        ]

    intent_block = _render_intent_vs_implementation(intent_items, evidence_links)
    if intent_block:
        sections += [intent_block, ""]

    block_block = _render_findings_block(
        "🚫 Block-severity findings", block_findings, emoji="🚫"
    )
    if block_block:
        sections += [block_block]

    review_block = _render_findings_block(
        "🟡 Review-required findings", review_findings, emoji="🟡"
    )
    if review_block:
        sections += [review_block]

    if missing_evidence_findings:
        evidence_block = _render_findings_block(
            "🔎 Missing test evidence",
            missing_evidence_findings,
            emoji="🔎",
        )
        if evidence_block:
            sections += [evidence_block]

    if behavioral_deltas:
        delta_bullets = []
        for delta in behavioral_deltas[:5]:
            path = delta.get("path") or ""
            symbol = delta.get("symbol") or ""
            new_behavior = delta.get("new_behavior") or delta.get("summary") or ""
            line = f"- `{path}` `{symbol}` — {new_behavior}" if symbol else f"- `{path}` — {new_behavior}"
            delta_bullets.append(line)
        sections.append(
            "<details><summary><strong>Behavioral deltas</strong> "
            f"({len(behavioral_deltas)})</summary>\n\n"
            + "\n".join(delta_bullets)
            + "\n\n</details>\n"
        )

    hotspots_block = _render_hotspots(compression)
    if hotspots_block:
        sections += [hotspots_block]

    sections += [
        "---",
        f"**Next action.** {next_action or 'Proceed with normal review.'}",
        "",
        "_Powered by MergeGuard — agents evaluated this PR against the linked "
        "issue's acceptance criteria. See the dashboard for the full audit trail._",
    ]
    return "\n".join(sections)


_TRUTH_REPORT_SYSTEM_PROMPT = """\
You are MergeGuard's senior-reviewer assistant. You write a structured PR
review comment in GitHub-flavored markdown.

OUTPUT FORMAT (HARD REQUIREMENTS — output is rejected if any are missing):

Return a single JSON object: {"comment_markdown": "<the entire comment as
one string with \\n line breaks>"}.

The comment string MUST start with EXACTLY these two lines, in this order,
nothing before them:

    <!-- mergeguard:sticky -->
    <!-- mergeguard:comment -->

It MUST then contain the H2 title, EXACTLY:

    ## MergeGuard Truth Report

It MUST contain ALL of the following section headers, in this order, even
if a section's body would be empty (in which case put a one-line "_None_"
under the header):

  1. ### {emoji} {label} — Risk **{N}/100**
     Where emoji is one of  ✅ 🟡 ⛔  matching the status field
     (pass / review / blocked), and label is the title-cased status.

  2. A markdown summary table with the EXACT header row:
         | Status | Risk | Blockers | Review | Files | Intent covered |
     and ONE data row underneath. Intent covered = "X/Y" where X is items
     with a ✅ / ⚪ evidence status and Y is the total intent item count.

  3. If top_blocker is set, a blockquote line starting with "> **Top blocker.**"

  4. ### Intent vs Implementation
     Followed by a one-line legend:
         _📋 from linked issue · 📝 from PR text · ✅ Covered / Compliant ·
         ⚪ Respected · 🟡 Partial · ❌ Missing · 🚫 Violated · ⚠️ Scope creep_
     Then a markdown table with this EXACT header row:
         | | Kind | Claim | Evidence |
         |---|------|-------|----------|
     One row PER intent item from `intent_items`. NEVER skip this section.
     If intent_items is empty, write "_No intent items extracted from the
     PR or linked issues._" under the legend instead of a table.

     Column rules:
       • Column 1: 📋 if source == "linked_issue", 📝 otherwise.
       • Column 2: the intent's `category` with underscores → spaces.
       • Column 3: the SHORT claim text (≤15 words). If the input
         `intent_items[i].text` is longer, summarize. Replace any "|"
         characters with "\\|" so the markdown table doesn't break.
       • Column 4: pick the evidence pill based on `evidence_status`:
            should + proven      → ✅ Covered
            should + partial     → 🟡 Partial
            should + missing     → ❌ Missing
            must_not + compliant → ✅ Compliant
            must_not + violated  → 🚫 Violated
            out_of_scope + respected     → ⚪ Respected
            out_of_scope + in_scope_creep → ⚠️ Scope creep
            unknown / absent     → ❓ Not evaluated

  5. <details open><summary><strong>🚫 Block-severity findings</strong> (N)</summary>
     Bullet list, one line per finding, like: - 🚫 `path/to/file.ts` — message
     </details>
     OMIT this whole <details> block if there are zero block-severity findings.

  6. <details open><summary><strong>🟡 Review-required findings</strong> (N)</summary>
     ...
     </details>
     OMIT if empty.

  7. <details><summary><strong>🔎 Missing test evidence</strong> (N)</summary>
     ...
     </details>
     OMIT if empty.

  8. <details><summary><strong>Behavioral deltas</strong> (N)</summary>
     ...
     </details>
     OMIT if empty.

  9. <details><summary><strong>Hotspots</strong> (N)</summary>
     ...
     </details>
     OMIT if empty.

 10. A horizontal rule: ---

 11. A "**Next action.** {next_action}" paragraph.

 12. A single-line italic footer (italics with underscores) about the
     dashboard / audit trail.

PROHIBITED:
- Do NOT add sections that aren't in the list above.
- Do NOT rename sections (e.g., don't say "Blockers" instead of "🚫
  Block-severity findings"; don't say "MergeGuard Review" instead of
  "MergeGuard Truth Report").
- Do NOT invent findings, intent items, or hotspots not in the input JSON.
- Do NOT echo raw IDs like `intent-3` in user-visible prose; quote the
  text instead.
- Do NOT use HTML other than `<details>`, `<summary>`, `<strong>`, `<br>`.

CONTENT RULES:
- File paths inside backticks: `src/foo.ts`.
- When at least one intent item has evidence_status ∈ {missing, violated},
  the top-blocker blockquote MUST quote that item's claim, not a generic
  test-coverage message.
- Keep total length under ~700 words.

JSON ENCODING:
- The whole response is JSON. Escape all double quotes and newlines INSIDE
  comment_markdown ("\\"" for quotes, "\\n" for line breaks). Don't end
  the string before the closing brace.
"""


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
    evidence_links: list[dict[str, Any]] | None = None,
    missing_evidence_findings: list[dict[str, Any]] | None = None,
) -> str | None:
    evidence_links = evidence_links or []
    missing_evidence_findings = missing_evidence_findings or []
    intent_with_evidence = []
    for item in intent_items[:14]:
        intent_id = item.get("id")
        link = next(
            (link for link in evidence_links if link.get("intent_id") == intent_id),
            None,
        )
        intent_with_evidence.append(
            {
                "id": intent_id,
                "text": item.get("text"),
                "category": item.get("category"),
                "source": item.get("source"),
                "evidence_status": (link or {}).get("evidence_status"),
                "mapped_paths": (link or {}).get("mapped_paths", []),
            }
        )
    files_changed = len(compression.get("files", []) or [])
    block_count = sum(1 for b in blockers if b.get("severity") == "block")
    review_count = sum(
        1 for b in blockers if b.get("severity") in {"review_required", "warn"}
    )
    covered = sum(
        1 for item in intent_with_evidence if item.get("evidence_status") == "proven"
    )
    structured = {
        "readiness": status,
        "risk_score": risk_score,
        "top_blocker": top_blocker,
        "next_action": next_action,
        "summary_counts": {
            "blockers": block_count,
            "review_items": review_count,
            "files_changed": files_changed,
            "intent_total": len(intent_with_evidence),
            "intent_covered": covered,
        },
        "hotspots": compression.get("hotspots", [])[:8],
        "blockers": [
            {
                "message": b.get("message"),
                "severity": b.get("severity"),
                "suggested_action": b.get("suggested_action"),
                "source_agent": b.get("source_agent"),
                "path": b.get("path"),
            }
            for b in blockers[:10]
        ],
        "missing_evidence_findings": missing_evidence_findings[:8],
        "behavioral_deltas": behavioral_deltas[:6],
        "intent_items": intent_with_evidence,
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
        # The full comment (status banner + summary table + Intent table +
        # findings + behavioral deltas + hotspots + footer) routinely runs
        # ~1500 chars of markdown = ~500 tokens, but escape characters
        # inside the JSON string roughly double that. Cap at 2200 so we
        # never get truncated mid-string and lose the closing brace.
        max_tokens=2200,
    )
    if not result:
        return None
    text = result.get("comment_markdown")
    if not isinstance(text, str) or not text.strip():
        return None
    # Strict post-validation — if the LLM dropped a required section, reject
    # the response so the caller falls back to the deterministic template
    # (which always emits every section in the right order).
    if not _validates_required_sections(text):
        return None
    return text


_REQUIRED_COMMENT_MARKERS = (
    "<!-- mergeguard:sticky -->",
    "<!-- mergeguard:comment -->",
    "## MergeGuard Truth Report",
    "### Intent vs Implementation",
    "| Status | Risk | Blockers",
)


def _validates_required_sections(comment: str) -> bool:
    """Return True only if the LLM kept every section the prompt requires.

    Catches the most common drift mode: the LLM omits the Intent vs
    Implementation table or the summary table because it thinks they're
    redundant with the Findings list. They're not — they're the whole point.
    """
    for marker in _REQUIRED_COMMENT_MARKERS:
        if marker not in comment:
            return False
    return True


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
