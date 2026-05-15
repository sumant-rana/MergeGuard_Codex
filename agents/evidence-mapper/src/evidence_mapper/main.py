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
from packages.core.analysis_utils import important_terms  # noqa: E402

AGENT_ID = "evidence-mapper"

app = create_app(AGENT_ID, "Map intent and findings to tests, canaries, contracts, traces, or HITL questions.")
register_default_llm(app)


@app.tool()
def map_intent(
    item: dict[str, Any],
    files: list[dict[str, Any]],
    memory_item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map an intent item to changed files, tests, or HITL questions that cover it.

    Behavior is **category-aware**:

    - ``should``        → look for changed implementation AND a changed test.
                          ``proven`` if both, ``partial`` if implementation only,
                          ``missing`` if neither.
    - ``must_not``      → check the diff for the prohibited pattern. If the
                          term doesn't appear in any changed file, the PR is
                          **compliant** (``proven``). If it does, the PR has
                          potentially violated the constraint → ``violated``.
    - ``out_of_scope``  → if the diff touches the named area, flag as
                          ``in_scope_creep``. Otherwise, ``respected`` — this
                          is the desired outcome and never a finding.
    """
    category = (item.get("category") or "should").lower()
    terms = [t.lower() for t in (item.get("terms") or important_terms(item.get("text", "")))]
    matched_files = [
        file
        for file in files
        if any(
            term in file.get("path", "").lower()
            or term in " ".join(file.get("risk_reasons", [])).lower()
            for term in terms
        )
    ]
    tests = [file for file in files if file.get("classification") == "test"]
    matched_tests = [file for file in matched_files if file.get("classification") == "test"]
    memory_item = memory_item or {}
    memory_tests = memory_item.get("test_candidates", [])
    memory_matches = memory_item.get("matches", [])

    if category == "must_not":
        # The intent describes something that must NOT happen. If the diff
        # doesn't touch terms from the constraint, the PR is compliant.
        if matched_files:
            status = "violated"
            suggested = (
                f"The diff touches `{matched_files[0]['path']}` which mentions "
                f"terms ({', '.join(terms[:3])}) the issue says must not change. "
                "Confirm intent or back the change out."
            )
        else:
            status = "compliant"
            suggested = "Constraint upheld — no changed file touches this area."
        confidence = 0.86 if matched_files else 0.78
    elif category == "out_of_scope":
        if matched_files:
            status = "in_scope_creep"
            suggested = (
                f"The diff appears to touch `{matched_files[0]['path']}` which the "
                "issue says is out of scope. Split into a follow-up PR or justify."
            )
            confidence = 0.74
        else:
            status = "respected"
            suggested = "Out-of-scope area not touched — no action needed."
            confidence = 0.86
    else:
        # category == "should" (or unknown → default to should semantics)
        if matched_files and matched_tests:
            status = "proven"
        elif matched_files and tests:
            # Changed tests exist, even if not matched directly — give partial.
            status = "partial"
        elif matched_files and memory_tests:
            status = "partial"
        elif memory_tests and memory_item.get("status") == "found":
            status = "partial"
        elif memory_matches:
            status = "partial"
        elif matched_files:
            status = "partial"
        else:
            status = "missing"
        suggested = suggested_action(item, matched_files, tests, memory_item)
        confidence = 0.84 if status == "proven" else 0.66 if status == "partial" else 0.55

    return {
        "intent_id": item["id"],
        "intent_text": item["text"],
        "intent_category": category,
        "evidence_status": status,
        "mapped_paths": [file["path"] for file in matched_files[:8]],
        "evidence_paths": [file["path"] for file in tests[:8]],
        "memory_status": memory_item.get("status", "not_found"),
        "memory_evidence_paths": [_memory_display_label(item) for item in memory_matches[:6]],
        "memory_test_candidates": [
            _memory_display_label(item) for item in memory_tests[:6]
        ],
        "memory_match_count": len(memory_matches),
        "confidence": confidence,
        "suggested_action": suggested,
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    prior = payload.get("prior_results", {})
    compression = prior.get("review-compression", {}).get("output", {})
    intent = prior.get("intent-extractor", {}).get("output", {})
    memory = prior.get("semantic-evidence-agent", {}).get("output", {})
    files = compression.get("files", [])
    intent_items = intent.get("intent_items", [])
    memory_by_intent = {
        item.get("intent_id"): item
        for item in memory.get("requirement_evidence", [])
        if item.get("intent_id")
    }
    # Build a patch lookup so the LLM-path can actually READ the changed
    # code, not just file paths. Without this the LLM has to guess whether
    # the implementation "really" covers each intent and tends to default
    # to "partial" out of caution.
    raw_changed_files = payload.get("changed_files", [])
    patches_by_path = {
        f.get("path"): str(f.get("patch") or "")
        for f in raw_changed_files
        if isinstance(f, dict) and f.get("path")
    }

    mode = "fallback"
    links: list[dict[str, Any]] = []
    if llm_available() and intent_items:
        llm_links = _map_via_llm(
            intent_items, files, memory_by_intent, patches_by_path
        )
        if llm_links is not None:
            links = llm_links
            mode = "llm"
    if not links:
        links = [
            map_intent(item, files, memory_by_intent.get(item.get("id")))
            for item in intent_items
        ]
        mode = "fallback" if mode != "llm" else mode
    missing_source_evidence = [
        {
            "type": "missing-test",
            "path": file["path"],
            "severity": "review_required" if file["risk_score"] >= 45 else "warn",
            "message": f"{file['classification']} change has no changed test evidence.",
            "suggested_action": missing_test_action(file, memory),
        }
        for file in files
        if file.get("classification") in {"logic", "security-sensitive", "prompt"}
        and not any(test.get("classification") == "test" for test in files)
    ]
    # Only ``should`` intents that the implementation skipped warrant an
    # author-preview prompt. ``must_not`` / ``out_of_scope`` shouldn't suspend.
    suspend_payloads = [
        {
            "kind": "author-preview",
            "question": f"Confirm intended behavior: {item['text']}",
            "intent_id": item["id"],
        }
        for item, link in zip(intent_items, links, strict=False)
        if link["evidence_status"] == "missing"
        and (item.get("category") or "should") == "should"
        and item.get("confidence", 1) < 0.7
    ]
    output = {
        "evidence_links": links,
        "missing_evidence_findings": missing_source_evidence,
        "suspend_payloads": suspend_payloads,
        "semantic_memory": {
            "provider": memory.get("memory_provider"),
            "related_tests": memory.get("related_tests", []),
            "requirement_evidence": memory.get("requirement_evidence", []),
        },
    }
    return make_agent_result(
        AGENT_ID,
        output,
        confidence=0.86 if mode == "llm" else 0.76,
        messages=[f"mapped evidence for {len(intent_items)} intent items via {mode}"],
        trace=[
            {
                "step": "map_evidence",
                "mode": mode,
                "links": len(links),
                "missing": len(missing_source_evidence),
            }
        ],
    )


# ── LLM path ────────────────────────────────────────────────────────────────


_EVIDENCE_SYSTEM_PROMPT = (
    "You are a senior reviewer mapping pull-request INTENT items to the "
    "CHANGED FILES that prove or fail to prove each intent.\n\n"
    "Each changed file comes with a ``patch_excerpt`` (first ~1500 chars of "
    "the unified diff, with ``+`` and ``-`` line prefixes preserved). USE THIS "
    "to verify whether each intent's claim is actually implemented — do NOT "
    "guess from the file name alone. Examples:\n"
    "  • Intent: 'Accept HTMLAttributes<HTMLSpanElement>' — look for a type "
    "    signature like ``React.HTMLAttributes<HTMLSpanElement>`` AND a "
    "    ``{...props}`` spread on the returned element. If both present in "
    "    the patch: proven. If only the type but no spread: partial. If neither: "
    "    missing.\n"
    "  • Intent: 'Use semantic Tailwind classes border-muted, animate-spin' — "
    "    search the patch for each named class. If all named classes appear in "
    "    a ``className`` string: proven. If some are absent: partial. Don't "
    "    mark missing if at least one is present.\n"
    "  • Intent: 'T4 — custom aria-label is preserved' — look for a test in "
    "    the *.test.* patch that renders the component with an explicit "
    "    ``aria-label`` prop AND asserts the rendered attribute equals it. "
    "    If both present: proven.\n\n"
    "Bias toward 'proven' when the patch contains direct, literal evidence; "
    "reserve 'partial' for genuinely ambiguous cases. The reviewer reads this "
    "table — calling a covered claim 'partial' is a false positive that "
    "actively misleads them.\n\n"
    "IMPORTANT — the meaning of evidence_status depends on the intent's category:\n\n"
    "  category == 'should' (something the PR should do):\n"
    "    - 'proven'   — a changed test or impl file directly exercises this intent.\n"
    "    - 'partial'  — relevant code changed but test evidence is weak.\n"
    "    - 'missing'  — no changed file exercises this intent at all.\n\n"
    "  category == 'must_not' (an explicit prohibition):\n"
    "    - 'compliant' — NO changed file touches the prohibited area. ✅ DEFAULT.\n"
    "    - 'violated'  — a changed file appears to touch / modify the prohibited\n"
    "                    area. Flag the file in mapped_paths.\n"
    "    Never use 'missing' for must_not intents — absence IS the desired outcome.\n\n"
    "  category == 'out_of_scope' (explicit non-goal):\n"
    "    - 'respected'      — no changed file touches the out-of-scope area. ✅ DEFAULT.\n"
    "    - 'in_scope_creep' — diff touches an out-of-scope area; consider splitting.\n"
    "    Never use 'missing' for out_of_scope intents — they don't need tests.\n\n"
    "Rules:\n"
    "- Use file paths from the provided list ONLY. Do not invent paths.\n"
    "- mapped_paths: changed implementation files that touch the intent's domain.\n"
    "- evidence_paths: changed *test* files that exercise the intent (should only).\n"
    "- suggested_action: one concrete next step the author should take. For\n"
    "  'compliant' / 'respected' rows, set this to a short reassurance like\n"
    "  'Constraint upheld — no action needed.'\n"
    "- confidence: 0-1; lower if the match is loose / inferred.\n\n"
    "Output a single JSON object:\n"
    '{"evidence_links": [\n'
    '  {"intent_id": str, "intent_text": str, "intent_category": "should"|"must_not"|"out_of_scope",\n'
    '   "evidence_status": "proven"|"partial"|"missing"|"compliant"|"violated"|"respected"|"in_scope_creep",\n'
    '   "mapped_paths": [str], "evidence_paths": [str], "confidence": float,\n'
    '   "suggested_action": str}\n'
    "]}"
)


_PATCH_EXCERPT_CHARS = 1500


def _map_via_llm(
    intent_items: list[dict[str, Any]],
    files: list[dict[str, Any]],
    memory_by_intent: dict[str, dict[str, Any]],
    patches_by_path: dict[str, str] | None = None,
) -> list[dict[str, Any]] | None:
    patches_by_path = patches_by_path or {}
    file_summaries = []
    for f in files:
        if not isinstance(f, dict) or not f.get("path"):
            continue
        path = f["path"]
        patch_excerpt = (patches_by_path.get(path) or "")[:_PATCH_EXCERPT_CHARS]
        file_summaries.append(
            {
                "path": path,
                "classification": f.get("classification"),
                "risk_score": f.get("risk_score"),
                "risk_reasons": (f.get("risk_reasons") or [])[:4],
                # The actual added/removed code so the LLM can verify
                # whether an intent's claim is implemented (not just guess
                # from the file name).
                "patch_excerpt": patch_excerpt,
            }
        )
    intent_summaries = [
        {
            "id": item.get("id"),
            "text": item.get("text"),
            "category": item.get("category"),
            "terms": item.get("terms", [])[:6],
            "memory_hint": _memory_summary_for(memory_by_intent.get(item.get("id"))),
        }
        for item in intent_items
        if isinstance(item, dict)
    ]
    user_prompt = (
        "Map each intent item to changed-file evidence. Use the system-prompt schema.\n\n"
        f"```json\n{json.dumps({'intents': intent_summaries, 'changed_files': file_summaries}, indent=2)}\n```"
    )
    result = call_llm_json(
        app=app,
        system=_EVIDENCE_SYSTEM_PROMPT,
        user=user_prompt,
        temperature=0.0,
        # Each evidence-link carries intent_text + path lists + memory
        # summaries, so ~16 links comfortably exceeds the old 1600-token
        # cap — the JSON gets truncated mid-string and the agent silently
        # falls back to the rule path. Bumped to 3500 with headroom.
        max_tokens=3500,
    )
    if not result:
        return None
    links = result.get("evidence_links")
    if not isinstance(links, list):
        return None

    valid_statuses = {
        "proven", "partial", "missing",
        "compliant", "violated",
        "respected", "in_scope_creep",
    }
    file_paths = {f.get("path") for f in file_summaries}
    intent_category_by_id = {
        i.get("id"): (i.get("category") or "should") for i in intent_items
    }
    cleaned: list[dict[str, Any]] = []
    for raw in links:
        if not isinstance(raw, dict):
            continue
        intent_id = raw.get("intent_id")
        if not intent_id:
            continue
        category = intent_category_by_id.get(intent_id, "should")
        status = raw.get("evidence_status")
        if status not in valid_statuses:
            status = "partial" if category == "should" else "compliant"
        # Guard against the LLM applying a coverage-style status to a
        # constraint-style intent (or vice versa).
        if category == "should" and status in {"compliant", "violated", "respected", "in_scope_creep"}:
            status = "missing"
        if category == "must_not" and status in {"proven", "partial", "missing", "respected", "in_scope_creep"}:
            status = "compliant" if status in {"missing", "respected"} else "violated"
        if category == "out_of_scope" and status in {"proven", "partial", "missing", "compliant", "violated"}:
            status = "respected" if status in {"missing", "compliant"} else "in_scope_creep"
        # Keep only paths that actually exist in the changed file list.
        mapped = [p for p in (raw.get("mapped_paths") or []) if p in file_paths]
        evidence = [p for p in (raw.get("evidence_paths") or []) if p in file_paths]
        memory_item = memory_by_intent.get(intent_id) or {}
        memory_matches = memory_item.get("matches", [])
        memory_tests = memory_item.get("test_candidates", [])
        cleaned.append(
            {
                "intent_id": intent_id,
                "intent_text": raw.get("intent_text", ""),
                "intent_category": category,
                "evidence_status": status,
                "mapped_paths": mapped[:8],
                "evidence_paths": evidence[:8],
                "memory_status": memory_item.get("status", "not_found"),
                "memory_evidence_paths": [
                    _memory_display_label(m) for m in memory_matches[:6]
                ],
                "memory_test_candidates": [
                    _memory_display_label(t) for t in memory_tests[:6]
                ],
                "memory_match_count": len(memory_matches),
                "confidence": float(raw.get("confidence") or (0.85 if status in {"proven", "compliant", "respected"} else 0.65)),
                "suggested_action": str(raw.get("suggested_action") or ""),
            }
        )
    return cleaned


def _memory_summary_for(memory_item: dict[str, Any] | None) -> dict[str, Any]:
    if not memory_item:
        return {"status": "not_found"}
    return {
        "status": memory_item.get("status", "not_found"),
        "test_candidates": [
            _memory_display_label(t)
            for t in (memory_item.get("test_candidates") or [])[:3]
        ],
        "matches": [
            _memory_display_label(m)
            for m in (memory_item.get("matches") or [])[:3]
        ],
    }


def _memory_display_label(hit: dict[str, Any]) -> str:
    """Pick a human-readable label for a memory hit.

    Memory hits round-tripping through the memory server lose their original
    ``path`` / ``title`` metadata; ``normalize_memory_hit`` then fills both
    with ``"."``. Prefer real-looking paths; fall back to title, then to a
    short snippet of the indexed text so the dashboard's Evidence panel can
    actually show something useful in the Memory sector.
    """
    if not isinstance(hit, dict):
        return ""
    path = (hit.get("path") or "").strip()
    if path and path != ".":
        return path
    title = (hit.get("title") or "").strip()
    if title and title != ".":
        return title
    text = (hit.get("text") or "").strip()
    if text:
        # Strip the ``[kind]`` prefix from prefix-tagged records so the snippet
        # is purely about content.
        if text.startswith("["):
            close = text.find("]")
            if 0 < close < 32:
                text = text[close + 1 :].strip()
        # Compact to a single short line.
        first_line = text.splitlines()[0] if text else ""
        snippet = first_line[:80].strip()
        if snippet:
            return snippet
    label = (hit.get("label") or "").strip()
    return label or "memory hit"


def suggested_action(
    item: dict[str, Any],
    matched_files: list[dict[str, Any]],
    tests: list[dict[str, Any]],
    memory_item: dict[str, Any] | None = None,
) -> str:
    if matched_files and tests:
        return "Verify changed tests exercise this intent."
    memory_item = memory_item or {}
    memory_tests = memory_item.get("test_candidates", [])
    if memory_tests:
        test_path = memory_tests[0].get("path") or memory_tests[0].get("title")
        return f"Extend or link existing repository evidence in {test_path}."
    if matched_files:
        return f"Add tests or reviewer acceptance for {matched_files[0]['path']}."
    if memory_item.get("matches"):
        return memory_item.get("suggested_action") or "Review related repository memory before approval."
    return f"Clarify or implement intent: {item['text']}"


def missing_test_action(file: dict[str, Any], memory: dict[str, Any]) -> str:
    related = [
        item
        for item in memory.get("related_tests", [])
        if related_text(file.get("path", ""), " ".join([item.get("path", ""), item.get("text", "")]))
    ]
    if related:
        path = related[0].get("path") or related[0].get("title")
        return f"Update or link existing repository test evidence in {path} for {file['path']}."
    return f"Add or link tests for {file['path']}."


def related_text(path: str, text: str) -> bool:
    path_terms = set(important_terms(path))
    text_terms = set(important_terms(text))
    return bool(path_terms & text_terms)


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
