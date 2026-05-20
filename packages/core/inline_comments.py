"""Compose line-anchored inline-comment payloads from MergeGuard findings.

This is the producer that feeds the GitHub *pull-request review* endpoint
(``POST /pulls/{n}/reviews``) — it does NOT call GitHub itself, just builds
the data the poster needs:

    {
        "path": str,         # repo-relative file path
        "line": int,         # 1-indexed line on the indicated side
        "side": "LEFT" | "RIGHT",
        "severity": "block" | "review_required" | "warn",
        "title": str,        # short Copilot-style heading
        "body": str,         # full Markdown body, links back to the sticky
        "source": str,       # which analyzer this came from (for telemetry)
    }

Sources are walked in priority order (semantic-diff first, then blocker
findings, then top hotspots). Within each source we dedupe by path so we
don't spam the same file. The final list is capped by ``MAX_INLINE_PER_PR``.
"""

from __future__ import annotations

from typing import Any, Iterable

from .inline_anchors import (
    Anchor,
    anchor_for_finding,
    build_line_index,
)

# Tuning knobs. Kept small to start — easy to bump once we see how the
# comments land on real PRs.
MAX_INLINE_PER_PR = 5
MAX_INLINE_PER_FILE = 1

_SEVERITY_ORDER = {"block": 0, "review_required": 1, "warn": 2}
_SEVERITY_ICON = {"block": "🔴", "review_required": "🟡", "warn": "⚠️"}


def build_inline_comments(
    *,
    changed_files: list[dict[str, Any]],
    behavioral_deltas: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
    hotspots: list[dict[str, Any]],
    sticky_anchor: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve, dedupe, and order inline comments for the current run.

    ``sticky_anchor``, when provided, is appended to every comment body as a
    "↳ See full Truth Report" link so reviewers can jump from inline back
    to the executive summary.
    """
    line_index = build_line_index(changed_files)
    if not line_index:
        return []

    candidates: list[dict[str, Any]] = []
    candidates.extend(_candidates_from_deltas(behavioral_deltas))
    candidates.extend(_candidates_from_blockers(blockers))
    candidates.extend(_candidates_from_hotspots(hotspots))

    seen_keys: set[str] = set()
    per_file_count: dict[str, int] = {}
    composed: list[dict[str, Any]] = []

    for candidate in sorted(candidates, key=_priority_key):
        anchor = anchor_for_finding(candidate["finding"], line_index)
        if anchor is None:
            continue
        if per_file_count.get(anchor.path, 0) >= MAX_INLINE_PER_FILE:
            continue
        dedup_key = f"{anchor.path}:{anchor.line}:{candidate['source']}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        per_file_count[anchor.path] = per_file_count.get(anchor.path, 0) + 1

        body = _compose_body(candidate, anchor, sticky_anchor)
        composed.append({
            "path": anchor.path,
            "line": anchor.line,
            "side": anchor.side,
            "severity": candidate["severity"],
            "title": candidate["title"],
            "body": body,
            "source": candidate["source"],
            "anchor_strategy": anchor.strategy,
        })
        if len(composed) >= MAX_INLINE_PER_PR:
            break
    return composed


# ---------------------------------------------------------------------------
# Source-specific candidate extraction. Each returns a list of
# {finding, source, severity, title, body_lead}.
# ---------------------------------------------------------------------------


def _candidates_from_deltas(deltas: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Behavioral deltas come from semantic-diff-explainer — best signal.

    They already describe *what* the diff does in human terms (e.g. "adds a
    new authenticated route"), so they're the highest-quality inline source.
    """
    out: list[dict[str, Any]] = []
    for delta in deltas or []:
        # Behavioral deltas often list multiple paths; we attach to the first
        # one that exists, leaving the others mentioned in the body.
        paths = [
            p for p in (delta.get("paths") or [delta.get("path")]) if p
        ]
        if not paths:
            continue
        primary = paths[0]
        text = delta.get("description") or delta.get("summary") or delta.get("title") or ""
        if not text:
            continue
        out.append({
            "source": "semantic-diff-explainer",
            "severity": delta.get("severity") or "review_required",
            "title": _shorten(delta.get("title") or text, 80),
            "body_lead": text,
            "finding": {
                "path": primary,
                "symbol": delta.get("symbol"),
                "snippet": delta.get("snippet"),
                "keyword": delta.get("keyword"),
            },
        })
    return out


def _candidates_from_blockers(blockers: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same blocker list the sticky already shows — promoted to inline.

    Only ``block`` and ``review_required`` severities go inline; ``warn``
    stays in the sticky to avoid noise.
    """
    out: list[dict[str, Any]] = []
    for blocker in blockers or []:
        severity = blocker.get("severity") or "review_required"
        if severity not in {"block", "review_required"}:
            continue
        path = blocker.get("path")
        # Skip "path-less" blockers (intent IDs like "intent-3") — they
        # can't be anchored to a line.
        if not path or str(path).startswith("intent-"):
            continue
        msg = blocker.get("message") or ""
        if not msg:
            continue
        out.append({
            "source": str(blocker.get("source_agent") or "policy-gate"),
            "severity": severity,
            "title": _shorten(msg, 80),
            "body_lead": msg,
            "suggested_action": blocker.get("suggested_action"),
            "finding": {
                "path": path,
                "line": blocker.get("line"),
                "side": blocker.get("side"),
                "snippet": blocker.get("snippet"),
                "symbol": blocker.get("symbol"),
                "keyword": blocker.get("keyword") or _first_keyword(blocker.get("keywords")),
            },
        })
    return out


def _candidates_from_hotspots(hotspots: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Last-resort fallback — flag high-risk files that no other source covered.

    Severity is ``warn`` so these never out-prioritize a real blocker.
    """
    out: list[dict[str, Any]] = []
    for hotspot in hotspots or []:
        path = hotspot.get("path")
        score = int(hotspot.get("risk_score") or 0)
        if not path or score < 45:
            continue
        reason = hotspot.get("reason") or "high risk score"
        action = hotspot.get("required_action") or "Inspect changed behavior."
        out.append({
            "source": "review-compression",
            "severity": "warn",
            "title": _shorten(f"High-risk file (score {score})", 60),
            "body_lead": f"{reason}.",
            "suggested_action": action,
            "finding": {"path": path},
        })
    return out


# ---------------------------------------------------------------------------
# Ordering + body composition.
# ---------------------------------------------------------------------------


def _priority_key(candidate: dict[str, Any]) -> tuple[int, int]:
    """Sort by severity (block first), then by source priority."""
    severity_rank = _SEVERITY_ORDER.get(candidate["severity"], 9)
    source_rank = {
        "semantic-diff-explainer": 0,
        "concept-classifier": 1,
        "policy-gate": 2,
        "slop-detector": 3,
        "contract-comparator": 4,
        "prompt-canary": 5,
        "evidence-mapper": 6,
        "review-compression": 9,
    }.get(candidate["source"], 7)
    return (severity_rank, source_rank)


def _compose_body(
    candidate: dict[str, Any],
    anchor: Anchor,
    sticky_anchor: str | None,
) -> str:
    """Render the Markdown body for a single inline comment.

    Format (Copilot-style):
        **{icon} {title}**

        {explanation}

        > Suggested action: ...

        ↳ [See full Truth Report]({sticky_anchor}) · MergeGuard
    """
    icon = _SEVERITY_ICON.get(candidate["severity"], "ℹ️")
    parts: list[str] = [f"**{icon} {candidate['title']}**", ""]
    lead = candidate.get("body_lead") or ""
    if lead and lead.strip() != candidate["title"]:
        parts.extend([lead.strip(), ""])
    action = candidate.get("suggested_action")
    if action:
        parts.extend([f"> Suggested action: {action.strip()}", ""])
    footer_bits = [f"_anchored via {anchor.strategy}_", "MergeGuard"]
    if sticky_anchor:
        footer_bits.insert(0, f"[See full Truth Report]({sticky_anchor})")
    parts.append("↳ " + " · ".join(footer_bits))
    return "\n".join(parts).rstrip() + "\n"


def _first_keyword(value: Any) -> str | None:
    if isinstance(value, list) and value:
        return str(value[0])
    if isinstance(value, str) and value:
        return value
    return None


def _shorten(text: str, limit: int) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 1)].rstrip() + "…"
