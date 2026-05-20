from __future__ import annotations

import json
import re
import sys
from pathlib import PurePosixPath, Path
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
from packages.core.analysis_utils import (  # noqa: E402
    important_terms,
    is_generated,
    is_test,
    normalize_path,
)

AGENT_ID = "slop-detector"

app = create_app(
    AGENT_ID,
    "Detect review slop such as debug leftovers, placeholders, noisy files, and weak tests.",
)
register_default_llm(app)


SLOP_PATH_TOKENS = {
    "scratch",
    "tmp",
    "temp",
    "debug",
    "playground",
    "experiment",
    "wip",
    "draft",
    "sample",
    "example",
}

DEBUG_PATTERNS = [
    (re.compile(r"\bconsole\.(log|debug|trace)\s*\(", re.I), "console logging"),
    (re.compile(r"\bdebugger\b", re.I), "debugger statement"),
    (re.compile(r"\bpdb\.set_trace\s*\(", re.I), "python debugger"),
    (re.compile(r"\bbreakpoint\s*\(", re.I), "breakpoint call"),
    (re.compile(r"\bprint\s*\(", re.I), "temporary print"),
    (re.compile(r"\bdump(var)?\s*\(", re.I), "debug dump"),
]

PLACEHOLDER_PATTERNS = [
    (re.compile(r"\bTODO\b.*\b(remove|cleanup|before merge|wire|real|later)\b", re.I), "todo marker"),
    (re.compile(r"\b(FIXME|XXX|HACK|WIP|DO NOT COMMIT)\b", re.I), "work-in-progress marker"),
    (re.compile(r"\b(lorem ipsum|placeholder|stubbed?|fake|dummy)\b", re.I), "placeholder content"),
    (re.compile(r"not implemented", re.I), "not implemented path"),
]

WEAK_IMPLEMENTATION_PATTERNS = [
    (re.compile(r"\breturn\s+true\s*;?", re.I), "always-true return"),
    (re.compile(r"\breturn\s+null\s*;?", re.I), "null placeholder return"),
    (re.compile(r"\breturn\s+undefined\s*;?", re.I), "undefined placeholder return"),
    (re.compile(r"\bpass\s*(#.*)?$", re.I | re.M), "empty python body"),
    (re.compile(r"\bany\b|:\s*any\b", re.I), "broad any typing"),
]

ASSERTION_PATTERNS = [
    # Plain ``expect(...)`` — classic Jest/vitest/Jasmine.
    re.compile(r"\bexpect\s*\(", re.I),
    # Chained ``expect.<thing>(...)`` — vitest-browser-react's
    # ``expect.element(...)``, vitest's ``expect.poll(...)`` /
    # ``expect.soft(...)`` / ``expect.hasAssertions(...)``. Without this
    # rule the slop-detector flags modern Vitest tests as "no assertions"
    # purely because they don't use the legacy ``expect(value)`` form.
    re.compile(r"\bexpect\s*\.", re.I),
    # Python ``assert ...`` / ``assertEqual(...)`` / ``self.assertTrue(...)``.
    re.compile(r"\bassert(That|Equal|True|False|Raises|In|NotIn|IsNone|IsNotNone|Greater|GreaterEqual|Less|LessEqual)?\s*\(", re.I),
    re.compile(r"\bself\.assert", re.I),
    # Chai-style ``thing.should.<…>``.
    re.compile(r"\bshould\.", re.I),
    # Standalone matcher call: ``.toBe(...)``, ``.toEqual(...)``,
    # ``.toBeInTheDocument()``, ``.toHaveBeenCalled(...)``, etc. The earlier
    # ``\bto(Equal|Be|…)\b`` rule had to end on a word boundary, which
    # silently failed on real matcher names like ``toBeInTheDocument``
    # because ``e`` (in ``toBe``) and ``I`` are both word characters.
    re.compile(r"\.\s*(?:toBe|toEqual|toContain|toHave|toThrow|toMatch|toStrictEqual|toBeTruthy|toBeFalsy|toBeDefined|toBeNull|toBeInTheDocument|toHaveTextContent|toHaveBeenCalled)\w*\s*\(", re.I),
    # Sinon / Chai / Jest globals occasionally land in test files.
    re.compile(r"\bsinon\.\w+\s*\(", re.I),
    re.compile(r"\bverify(That)?\s*\(", re.I),
]

# Minimum added lines before the "scenario shape but no assertions" rule is
# allowed to fire. Without this guard, a one-line vitest patch that touches a
# matcher's options object (e.g. ``{ exact: true }``) was being flagged as a
# weak test even though the file already had dozens of real assertions.
_WEAK_TEST_MIN_ADDED_LINES = 6


@app.tool()
def inspect_file(
    file: dict[str, Any],
    raw_file: dict[str, Any] | None = None,
    intent_terms: list[str] | None = None,
) -> dict[str, Any] | None:
    """Return one consolidated slop finding for a changed file when hygiene signals are present."""
    raw_file = raw_file or {}
    path = normalize_path(file.get("path") or raw_file.get("path") or "")
    if not path:
        return None
    patch = str(raw_file.get("patch") or file.get("patch") or "")
    content = str(raw_file.get("content") or file.get("content") or "")
    additions = int(file.get("additions") or raw_file.get("additions") or 0)
    changes = int(file.get("changes") or raw_file.get("changes") or additions)
    classification = str(file.get("classification") or "")
    text = "\n".join(part for part in [path, patch, content] if part)
    added_lines = added_patch_lines(patch)

    signals: list[str] = []
    categories: list[str] = []
    score = 0

    path_tokens = set(important_terms(path.replace("/", " ")))
    if path_tokens & SLOP_PATH_TOKENS:
        signals.append(f"path looks temporary: {', '.join(sorted(path_tokens & SLOP_PATH_TOKENS))}")
        categories.append("temporary_file")
        score += 18

    # Generated files (lockfiles, TanStack/router trees, protobuf bundles,
    # codegen output) shouldn't be subject to "looks weak" / "looks placeholder"
    # / "copy-paste" heuristics — the patterns those heuristics fire on
    # (literal route registrations, repeated table rows, `as any` casts)
    # are *idiomatic* in generated output. Only the volume-based
    # ``generated_churn`` signal applies; everything else short-circuits.
    file_is_generated = is_generated(path, content) or classification == "generated"
    if file_is_generated:
        if changes > 120 or additions > 80:
            signals.append("large generated/lock/snapshot churn")
            categories.append("generated_churn")
            score += 24
    else:
        debug_hits = pattern_hits(DEBUG_PATTERNS, text)
        if debug_hits:
            signals.extend(debug_hits)
            categories.append("debug_artifact")
            score += 28 + min(12, 3 * len(debug_hits))

        placeholder_hits = pattern_hits(PLACEHOLDER_PATTERNS, text)
        if placeholder_hits:
            signals.extend(placeholder_hits)
            categories.append("placeholder_work")
            score += 22 + min(10, 2 * len(placeholder_hits))

        weak_hits = pattern_hits(WEAK_IMPLEMENTATION_PATTERNS, "\n".join(added_lines))
        if weak_hits and not is_docs_like(path):
            signals.extend(weak_hits)
            categories.append("weak_implementation")
            score += 18 + min(10, 2 * len(weak_hits))

        commented_ratio = commented_added_ratio(added_lines)
        if commented_ratio >= 0.42 and len(added_lines) >= 8:
            signals.append(f"{round(commented_ratio * 100)}% of added lines are comments")
            categories.append("commented_or_dead_code")
            score += 18

        duplicate_count = repeated_added_line_count(added_lines)
        if duplicate_count >= 4:
            signals.append(f"{duplicate_count} repeated added lines")
            categories.append("copy_paste_noise")
            score += 14

    if is_test(path):
        test_smell = test_slop_signal(path, text, added_lines)
        if test_smell:
            signals.append(test_smell)
            categories.append("weak_test")
            score += 22

    unrelated = unrelated_change_signal(path, text, intent_terms or [])
    if unrelated and changes >= 18 and classification not in {"docs", "generated"}:
        signals.append(unrelated)
        categories.append("unrelated_noise")
        score += 16

    if additions >= 350 and not categories:
        signals.append("large additive change with no clear review category")
        categories.append("broad_noise")
        score += 14

    if not categories:
        return None

    score = min(100, score + min(12, changes // 35))
    severity = "review_required" if score >= 45 else "warn"
    disposition = disposition_for(path, categories, score)
    return {
        "path": path,
        "category": primary_category(categories),
        "categories": sorted(set(categories)),
        "severity": severity,
        "score": score,
        "confidence": confidence_for(score, signals),
        "message": message_for(path, disposition, categories),
        "signals": dedupe(signals)[:8],
        "suggested_action": suggested_action_for(disposition, categories),
        "disposition": disposition,
        "line_citations": [{"path": path, "range": "changed hunk"}],
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    prior = payload.get("prior_results", {})
    compression = prior.get("review-compression", {}).get("output", {})
    intent = prior.get("intent-extractor", {}).get("output", {})
    files = classified_files(payload, compression)
    raw_by_path = {
        normalize_path(file.get("path", "")): file
        for file in payload.get("changed_files", [])
        if file.get("path")
    }
    intent_terms = intent_terms_for(payload, intent)
    findings = [
        finding
        for file in files
        if (
            finding := inspect_file(
                file,
                raw_by_path.get(normalize_path(file.get("path", ""))),
                intent_terms,
            )
        )
    ]
    findings = sorted(findings, key=lambda item: (-int(item["score"]), item["path"]))
    for index, finding in enumerate(findings, start=1):
        finding["id"] = f"slop-{index}"

    # LLM disambiguator: 'rework' findings are the ambiguous ones (could be
    # genuine slop, could be intentional). Ask LLM to judge them in-context
    # and downgrade obvious-not-slop matches. 'remove' findings (clear
    # debugger/console hits) stay as-is — those are auditable rules wins.
    if llm_available():
        ambiguous = [f for f in findings if f.get("disposition") == "rework"]
        if ambiguous:
            _apply_llm_disambiguation(ambiguous, raw_by_path)

    remove_candidates = [finding for finding in findings if finding["disposition"] == "remove"]
    rework_candidates = [finding for finding in findings if finding["disposition"] == "rework"]
    score = slop_score(findings)
    status = "review" if findings else "pass"
    output = {
        "slop_score": score,
        "slop_status": status,
        "slop_findings": findings,
        "remove_candidates": remove_candidates,
        "rework_candidates": rework_candidates,
        "file_scores": [
            {
                "path": item["path"],
                "score": item["score"],
                "disposition": item["disposition"],
                "category": item["category"],
            }
            for item in findings
        ],
    }
    return make_agent_result(
        AGENT_ID,
        output,
        confidence=0.82 if findings else 0.72,
        messages=[message_summary(output)],
        trace=[
            {
                "step": "detect_review_slop",
                "files": len(files),
                "findings": len(findings),
                "remove_candidates": len(remove_candidates),
                "rework_candidates": len(rework_candidates),
            }
        ],
    )


def classified_files(payload: dict[str, Any], compression: dict[str, Any]) -> list[dict[str, Any]]:
    if compression.get("files"):
        return compression["files"]
    files = []
    for file in payload.get("changed_files", []):
        path = normalize_path(file.get("path", ""))
        files.append(
            {
                **file,
                "path": path,
                "classification": "test" if is_test(path) else "generated" if is_generated(path) else "logic",
                "risk_score": 24,
                "risk_reasons": [],
                "symbols": [],
            }
        )
    return files


def intent_terms_for(payload: dict[str, Any], intent: dict[str, Any]) -> list[str]:
    pr = payload.get("pull_request", {})
    terms = important_terms(" ".join(str(part) for part in [pr.get("title"), pr.get("body")] if part))
    for item in intent.get("intent_items", []):
        terms.extend(item.get("terms") or important_terms(item.get("text", "")))
    return sorted(set(terms))[:24]


def added_patch_lines(patch: str) -> list[str]:
    lines = []
    for raw in patch.splitlines():
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            lines.append(raw[1:].strip())
    return lines


def pattern_hits(patterns: list[tuple[re.Pattern[str], str]], text: str) -> list[str]:
    return [label for pattern, label in patterns if pattern.search(text)]


def commented_added_ratio(lines: list[str]) -> float:
    meaningful = [line for line in lines if line and not line in {"{", "}", ");"}]
    if not meaningful:
        return 0.0
    comments = [
        line
        for line in meaningful
        if line.startswith(("//", "#", "/*", "*", "<!--")) or line.endswith("-->")
    ]
    return len(comments) / len(meaningful)


def repeated_added_line_count(lines: list[str]) -> int:
    seen: dict[str, int] = {}
    for line in lines:
        clean = re.sub(r"\s+", " ", line.strip())
        if len(clean) < 12 or clean in {"};", "});"}:
            continue
        seen[clean] = seen.get(clean, 0) + 1
    return sum(count - 1 for count in seen.values() if count > 1)


_COMMENTED_ASSERTION_RE = re.compile(
    r"^\s*(?://|#)\s*(?:expect|assert(?:That|Equal|True|False|Raises)?|should\.)\s*\(",
    re.IGNORECASE,
)


def test_slop_signal(path: str, text: str, added_lines: list[str]) -> str | None:
    if not added_lines:
        return None
    lower = text.lower()
    has_test_shape = any(token in lower for token in ["describe(", "it(", "test(", "def test_"])
    has_assertion = any(pattern.search(text) for pattern in ASSERTION_PATTERNS)
    snapshot_only = "__snapshots__" in path or (
        "snapshot" in lower and not any(pattern.search(text) for pattern in ASSERTION_PATTERNS[:2])
    )
    # Commented-out assertions are a strong "weakened test" signal — the
    # test still passes but no longer guarantees the original behaviour.
    commented_assertions = sum(1 for line in added_lines if _COMMENTED_ASSERTION_RE.match(line))
    if commented_assertions:
        plural = "s" if commented_assertions > 1 else ""
        return (
            f"{commented_assertions} commented-out assertion{plural} "
            f"in test file — coverage weakened without test removal"
        )
    if has_test_shape and not has_assertion and len(added_lines) >= _WEAK_TEST_MIN_ADDED_LINES:
        return "test file has scenario shape but no assertions"
    if snapshot_only:
        return "snapshot-only change needs reviewer confirmation"
    return None


def unrelated_change_signal(path: str, text: str, intent_terms: list[str]) -> str | None:
    if not intent_terms:
        return None
    normalized_haystack = expanded_text(" ".join([path, text]))
    if any(term in normalized_haystack for term in intent_terms):
        return None
    haystack_terms = set(expanded_terms(" ".join([path, text])))
    shared = sorted(haystack_terms & set(intent_terms))
    if shared:
        return None
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico"}:
        return "asset change has no overlap with PR intent terms"
    return "changed file has no overlap with PR intent terms"


def expanded_text(text: str) -> str:
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return re.sub(r"[^a-zA-Z0-9]+", " ", camel_split).lower()


def expanded_terms(text: str) -> list[str]:
    return important_terms(expanded_text(text))


def is_docs_like(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in {".md", ".mdx", ".txt", ".rst"}


def disposition_for(path: str, categories: list[str], score: int) -> str:
    path_terms = set(important_terms(path.replace("/", " ")))
    remove_categories = {
        "debug_artifact",
        "temporary_file",
        "generated_churn",
        "copy_paste_noise",
        "commented_or_dead_code",
    }
    if path_terms & SLOP_PATH_TOKENS and ("debug_artifact" in categories or "temporary_file" in categories):
        return "remove"
    if set(categories) & remove_categories and score >= 48:
        return "remove"
    return "rework"


def primary_category(categories: list[str]) -> str:
    priority = [
        "debug_artifact",
        "temporary_file",
        "placeholder_work",
        "weak_implementation",
        "weak_test",
        "generated_churn",
        "unrelated_noise",
        "commented_or_dead_code",
        "copy_paste_noise",
        "broad_noise",
    ]
    for item in priority:
        if item in categories:
            return item
    return categories[0]


def confidence_for(score: int, signals: list[str]) -> float:
    return round(min(0.94, 0.54 + score / 220 + min(0.12, len(signals) * 0.025)), 2)


def message_for(path: str, disposition: str, categories: list[str]) -> str:
    action = "Remove" if disposition == "remove" else "Rework"
    category = primary_category(categories).replace("_", " ")
    return f"{action} or justify `{path}`: {category} detected."


def suggested_action_for(disposition: str, categories: list[str]) -> str:
    if disposition == "remove":
        return "Remove this file/change from the PR or add a reviewer-visible justification for why it belongs."
    if "weak_test" in categories:
        return "Add assertions that prove the intended behavior or move the test out of this PR."
    if "placeholder_work" in categories or "weak_implementation" in categories:
        return "Replace placeholder behavior with production logic before merge."
    return "Narrow the change, explain why it belongs to this PR, or split it into a follow-up."


def slop_score(findings: list[dict[str, Any]]) -> int:
    if not findings:
        return 0
    top = sorted((int(item["score"]) for item in findings), reverse=True)[:4]
    return min(100, int(max(top) * 0.72 + (sum(top) / len(top)) * 0.28))


def message_summary(output: dict[str, Any]) -> str:
    findings = output["slop_findings"]
    if not findings:
        return "no review slop detected"
    return (
        f"detected {len(findings)} review slop findings "
        f"({len(output['remove_candidates'])} remove, {len(output['rework_candidates'])} rework)"
    )


def dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output


# ── LLM disambiguator ──────────────────────────────────────────────────────


_SLOP_DISAMBIGUATOR_SYSTEM_PROMPT = (
    "You are reviewing potential 'slop' findings in a pull request — debug "
    "leftovers, placeholders, weak tests, suspicious extras. The rule layer "
    "has already flagged these as AMBIGUOUS (could be slop, could be "
    "intentional). Decide whether each is genuinely slop a reviewer should "
    "remove/rework, or a false positive.\n\n"
    "Rules:\n"
    "- Quote a token / phrase from the patch in 'evidence' to justify your call.\n"
    "- Verdicts: 'slop' (yes, remove/rework), 'intentional' (false positive,\n"
    "  drop the finding), 'unsure' (low signal — keep as-is).\n"
    "- 'reasoning': one short sentence.\n"
    "- 'confidence': 0-1.\n\n"
    "Output a single JSON object:\n"
    '{"verdicts": [\n'
    '  {"finding_id": str, "verdict": "slop"|"intentional"|"unsure",\n'
    '   "reasoning": str, "evidence": str, "confidence": float}\n'
    "]}"
)


def _apply_llm_disambiguation(
    ambiguous: list[dict[str, Any]],
    raw_by_path: dict[str, dict[str, Any]],
) -> None:
    """Mutates `ambiguous` findings in place, adding `llm_verdict` field.

    Findings the LLM marks 'intentional' get their disposition downgraded
    to 'noted' so they don't drive remove/rework recommendations, but stay
    visible in the dashboard for transparency.
    """
    payload_findings = []
    for f in ambiguous[:10]:
        path = f.get("path", "")
        raw = raw_by_path.get(normalize_path(path), {})
        payload_findings.append(
            {
                "finding_id": f.get("id"),
                "path": path,
                "category": f.get("category"),
                "score": f.get("score"),
                "rule_matches": f.get("rule_matches", []),
                "patch_excerpt": (raw.get("patch") or "")[:1200],
            }
        )

    user_prompt = (
        "Judge each ambiguous slop finding. Use the system-prompt schema.\n\n"
        f"```json\n{json.dumps({'findings': payload_findings}, indent=2)}\n```"
    )

    result = call_llm_json(
        app=app,
        system=_SLOP_DISAMBIGUATOR_SYSTEM_PROMPT,
        user=user_prompt,
        temperature=0.0,
        max_tokens=1200,
    )
    if not result:
        return
    verdicts = result.get("verdicts")
    if not isinstance(verdicts, list):
        return

    by_id = {f.get("id"): f for f in ambiguous}
    valid_verdicts = {"slop", "intentional", "unsure"}
    for raw in verdicts:
        if not isinstance(raw, dict):
            continue
        fid = raw.get("finding_id")
        finding = by_id.get(fid)
        if finding is None:
            continue
        verdict = raw.get("verdict")
        if verdict not in valid_verdicts:
            continue
        finding["llm_verdict"] = {
            "verdict": verdict,
            "reasoning": str(raw.get("reasoning") or ""),
            "evidence": str(raw.get("evidence") or ""),
            "confidence": float(raw.get("confidence") or 0.6),
        }
        if verdict == "intentional":
            # Keep the finding visible but stop it from driving remove/rework.
            finding["disposition"] = "noted"


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
