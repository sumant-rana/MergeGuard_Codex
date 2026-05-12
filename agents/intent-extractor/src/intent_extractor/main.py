from __future__ import annotations

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

from packages.agent_runtime import create_app, make_agent_result, register_entrypoint  # noqa: E402
from packages.core.analysis_utils import important_terms  # noqa: E402

AGENT_ID = "intent-extractor"

app = create_app(AGENT_ID, "Extract review intent from PR text and linked work items.")


@app.tool()
def extract_items(text: str) -> list[dict[str, Any]]:
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
    items = extract_items(text)
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
        messages=[f"extracted {len(items)} intent items"],
        trace=[{"step": "extract_intent", "item_count": len(items)}],
    )


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
