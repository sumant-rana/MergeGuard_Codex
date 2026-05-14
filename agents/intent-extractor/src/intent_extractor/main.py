from __future__ import annotations

import json
import re
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
from packages.core.analysis_utils import important_terms  # noqa: E402

AGENT_ID = "intent-extractor"

app = create_app(AGENT_ID, "Extract review intent from PR text and linked work items.")
register_default_llm(app)


@app.tool()
def extract_items(text: str) -> list[dict[str, Any]]:
    """Extract should / must-not / out-of-scope intent items from PR or work-item text."""
    chunks = [
        chunk.strip(" -\t")
        for chunk in re.split(r"\n|[.;]", text)
        if chunk.strip(" -\t")
    ]
    items: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        lower = chunk.lower()
        if any(marker in lower for marker in ["out of scope", "non-goal", "not included"]):
            category = "out_of_scope"
        elif any(marker in lower for marker in ["must not", "should not", "do not", "avoid", "without"]):
            category = "must_not"
        elif any(marker in lower for marker in ["must", "should", "need", "require", "implement", "support", "ensure", "fix", "add"]):
            category = "should"
        elif index == 0:
            category = "should"
        else:
            continue
        items.append(
            {
                "id": f"intent-{index + 1}",
                "text": chunk,
                "category": category,
                "source": "pr_text",
                "terms": important_terms(chunk),
                "confidence": 0.82 if category != "should" or index > 0 else 0.62,
                "severity": "review_required" if category == "must_not" else "warn",
            }
        )
    return items[:16]


def run(payload: dict[str, Any]) -> dict[str, Any]:
    pr = payload.get("pull_request", {})
    linked_docs = "\n".join(payload.get("settings", {}).get("linked_docs", []))
    text = "\n".join(
        part
        for part in [
            pr.get("title", ""),
            pr.get("body", ""),
            pr_context(pr),
            linked_docs,
        ]
        if part
    )

    mode = "fallback"
    items: list[dict[str, Any]] = []
    if llm_available() and text.strip():
        llm_items = _extract_via_llm(pr=pr, text=text)
        if llm_items is not None:
            items = llm_items
            mode = "llm"
    if not items:
        items = extract_items(text)
        mode = "fallback" if mode != "llm" else mode

    output = {
        "intent_items": items,
        "requires_author_preview": any(item["confidence"] < 0.65 for item in items),
        "memory_writes": [
            {"collection": "memory_semantic", "text": item["text"], "metadata": {"intent_id": item["id"]}}
            for item in items
        ],
    }
    return make_agent_result(
        AGENT_ID,
        output,
        confidence=min([item["confidence"] for item in items], default=0.7),
        messages=[f"extracted {len(items)} intent items via {mode}"],
        trace=[{"step": "extract_intent", "mode": mode, "item_count": len(items)}],
    )


# ── LLM path ────────────────────────────────────────────────────────────────


_INTENT_SYSTEM_PROMPT = (
    "You extract reviewer-relevant INTENT from a pull request's text. "
    "Read the PR title, body, linked-issue BODIES (the full text of every issue\n"
    "the PR closes / references), and commit messages, then identify discrete\n"
    "claims the author is making.\n\n"
    "PRIORITY: linked-issue bodies typically contain the canonical acceptance\n"
    "criteria and test cases. When an issue lists 'should' / 'must not' /\n"
    "'out of scope' items or numbered acceptance criteria, extract each one\n"
    "as its own intent item — these are the ground truth the implementation\n"
    "will be checked against.\n\n"
    "Each item must be one of three categories:\n"
    "  - 'should'        — something the PR is trying to do / add / fix / support.\n"
    "  - 'must_not'      — an explicit prohibition / constraint (e.g., 'must not\n"
    "                      expose PII', 'do not change the public API').\n"
    "  - 'out_of_scope'  — explicitly excluded from this PR (e.g., 'not changing\n"
    "                      billing logic', 'follow-up will handle X').\n\n"
    "Rules:\n"
    "- 'text' is the SHORT human-readable form of the claim — aim for 8-15\n"
    "  words. Strip code blocks, file paths, and conditionals like 'when X is Y\n"
    "  AND Z'. The reviewer should read each row in <2s. If the source uses\n"
    "  prose like 'Render an empty-state row spanning all columns when ...',\n"
    "  summarize to 'Show empty state when filters return zero rows'.\n"
    "- Do NOT invent claims. Every item must be traceable to a sentence in the\n"
    "  PR body or a linked issue.\n"
    "- Set 'source' to 'linked_issue' when the claim comes from an issue body,\n"
    "  'pr_text' when it comes from the PR title / body / commits.\n"
    "- Extract at most 16 items. Skip filler like 'opened PR', 'see linked ticket'.\n"
    "- 'severity': 'review_required' for must_not, 'warn' otherwise.\n"
    "- 'confidence': 0-1, higher (>=0.85) for claims pulled from numbered\n"
    "  acceptance criteria in a linked issue.\n"
    "- 'terms': 2-5 lowercase keywords from the claim that a code-search would match.\n\n"
    "Output a single JSON object:\n"
    '{"intent_items": [\n'
    '   {"id": "intent-N", "text": str, "category": "should"|"must_not"|"out_of_scope",\n'
    '    "source": "pr_text"|"linked_issue", "terms": [str], "confidence": float,\n'
    '    "severity": "warn"|"review_required"}\n'
    "]}"
)


def _extract_via_llm(pr: dict[str, Any], text: str) -> list[dict[str, Any]] | None:
    # Trim each issue body so a verbose ticket doesn't blow the LLM budget,
    # but keep enough to capture acceptance criteria + test cases.
    linked_issues_payload: list[dict[str, Any]] = []
    for issue in (pr.get("linked_issues") or [])[:5]:
        if not isinstance(issue, dict):
            continue
        body = str(issue.get("body") or "")
        linked_issues_payload.append(
            {
                "number": issue.get("number"),
                "title": issue.get("title", ""),
                "labels": issue.get("labels", []),
                "body": body[:3500],
            }
        )

    structured_input = {
        "title": pr.get("title", ""),
        "body": pr.get("body", ""),
        "issue_refs": [
            {"number": i.get("number"), "title": i.get("title"), "state": i.get("state")}
            for i in (pr.get("issue_refs") or [])[:10] if isinstance(i, dict)
        ],
        "linked_issues": linked_issues_payload,
        "commits": [
            {"oid": (c.get("oid") or "")[:12], "message": c.get("message", "")}
            for c in (pr.get("commit_history") or [])[:15] if isinstance(c, dict)
        ],
        "combined_text_preview": text[:3500],
    }
    user_prompt = (
        "Extract intent items from the following pull request. Use the system-prompt schema.\n\n"
        f"```json\n{json.dumps(structured_input, indent=2)}\n```"
    )

    result = call_llm_json(
        app=app,
        system=_INTENT_SYSTEM_PROMPT,
        user=user_prompt,
        temperature=0.0,
        # 16 intent items × (text + terms + metadata) routinely produces
        # ~2.5 KB of JSON. Old cap of 1200 truncated mid-string for any PR
        # with a long linked-issue body. Bumped to 3000 so the response
        # always closes the array + outer brace.
        max_tokens=3000,
    )
    if not result:
        return None
    items = result.get("intent_items")
    if not isinstance(items, list):
        return None

    valid_categories = {"should", "must_not", "out_of_scope"}
    valid_severities = {"warn", "review_required"}
    valid_sources = {"pr_text", "linked_issue"}
    cleaned: list[dict[str, Any]] = []
    for idx, raw in enumerate(items):
        if not isinstance(raw, dict):
            continue
        text_field = str(raw.get("text") or "").strip()
        if not text_field:
            continue
        category = raw.get("category")
        if category not in valid_categories:
            continue
        severity = raw.get("severity")
        if severity not in valid_severities:
            severity = "review_required" if category == "must_not" else "warn"
        terms = raw.get("terms")
        if not isinstance(terms, list) or not terms:
            terms = important_terms(text_field)
        source = str(raw.get("source") or "pr_text")
        if source not in valid_sources:
            source = "pr_text"
        cleaned.append(
            {
                "id": str(raw.get("id") or f"intent-{idx + 1}"),
                "text": text_field,
                "category": category,
                "source": source,
                "terms": [str(t).lower() for t in terms][:8],
                "confidence": float(raw.get("confidence") or 0.75),
                "severity": severity,
            }
        )
        if len(cleaned) >= 16:
            break
    return cleaned


def pr_context(pr: dict[str, Any]) -> str:
    parts: list[str] = []
    if pr.get("analysis_context"):
        parts.append(str(pr["analysis_context"]))

    issue_lines = []
    for issue in pr.get("issue_refs", [])[:20]:
        if isinstance(issue, dict):
            issue_lines.append(
                " ".join(
                    str(part)
                    for part in [
                        f"Fixes issue #{issue.get('number')}",
                        issue.get("title", ""),
                        issue.get("state", ""),
                    ]
                    if part
                )
            )
    if issue_lines:
        parts.append("\n".join(issue_lines))

    commit_lines = []
    for commit in pr.get("commit_history", [])[:25]:
        if isinstance(commit, dict):
            commit_lines.append(
                " ".join(
                    str(part)
                    for part in [
                        "Commit",
                        commit.get("oid", "")[:12],
                        commit.get("message", ""),
                        commit.get("body", ""),
                    ]
                    if part
                )
            )
    if commit_lines:
        parts.append("\n".join(commit_lines))

    return "\n".join(parts)


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
