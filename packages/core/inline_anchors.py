"""Resolve a MergeGuard finding to a concrete ``(path, line, side)`` anchor.

Findings flow through the analyzer pipeline in many shapes — some carry an
explicit line, some only a path, some a snippet or a symbol name. The
GitHub review-comments API wants exactly one anchor per inline comment, so
this module is the deterministic bridge between the two.

Resolution order (first hit wins):

  1. Explicit ``line`` (and optional ``side``) on the finding.
  2. ``snippet`` substring match against added lines.
  3. ``symbol`` regex match against added lines (declaration / call site).
  4. ``keyword`` token match — uses the same Phase-1 tokenizer so we don't
     re-introduce the ``_authenticated -> auth`` false positive.
  5. Path-only fallback to ``first_signal_line`` (first interesting added
     line, skipping imports/blanks).

A finding that can't be anchored to a line in the *changed* set is returned
as ``None`` — callers keep the information in the sticky comment instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .analysis_utils import tokenize
from .diff_lines import (
    LineEdit,
    Side,
    find_by_pattern,
    find_by_text,
    first_signal_line,
    parse_patch,
)


@dataclass(frozen=True)
class Anchor:
    """The resolved ``(path, line, side)`` triple plus a strategy tag."""

    path: str
    line: int
    side: Side
    strategy: str  # "explicit" | "snippet" | "symbol" | "keyword" | "signal"


def build_line_index(changed_files: Iterable[dict[str, Any]]) -> dict[str, list[LineEdit]]:
    """Index every changed file's patch by path, ready for repeated lookups."""
    index: dict[str, list[LineEdit]] = {}
    for entry in changed_files:
        path = str(entry.get("path") or "")
        if not path:
            continue
        patch = str(entry.get("patch") or "")
        edits = parse_patch(path, patch)
        if edits:
            index[path] = edits
    return index


def anchor_for_finding(
    finding: dict[str, Any],
    line_index: dict[str, list[LineEdit]],
) -> Anchor | None:
    """Resolve a single finding to an ``Anchor``, or ``None`` if it can't be placed.

    See module docstring for the strategy precedence. The finding can carry
    any combination of: ``path``, ``line``, ``start_line``, ``side``,
    ``snippet``, ``symbol``, ``keyword``.
    """
    path = str(finding.get("path") or "").strip()
    if not path:
        return None
    edits = line_index.get(path)
    if not edits:
        return None

    # 1) Explicit line — trust if it actually appears in our edit list.
    explicit_line = _first_int(finding.get("line"), finding.get("start_line"))
    if explicit_line is not None:
        side = _side(finding.get("side")) or "RIGHT"
        for edit in edits:
            if edit.line == explicit_line and edit.side == side:
                return Anchor(path, edit.line, edit.side, "explicit")
        # The line wasn't in the diff — fall through to heuristic resolution
        # rather than refusing to anchor (the explicit line was likely from
        # an analyzer that doesn't know about the diff window).

    # 2) Snippet — substring match against added lines.
    snippet = (finding.get("snippet") or "").strip()
    if snippet:
        hit = find_by_text(edits, snippet)
        if hit:
            return Anchor(path, hit.line, hit.side, "snippet")

    # 3) Symbol — regex match against added lines, preferring declaration
    #    patterns (``function foo``, ``def foo``, ``class foo``, ``foo =``).
    symbol = (finding.get("symbol") or "").strip()
    if symbol:
        safe = re.escape(symbol)
        decl_re = re.compile(
            rf"(?:function|class|def|async\s+def)\s+{safe}\b|\b{safe}\s*[:=]",
        )
        hit = find_by_pattern(edits, decl_re)
        if hit:
            return Anchor(path, hit.line, hit.side, "symbol")
        # Fall back to any mention.
        ref_re = re.compile(rf"\b{safe}\b")
        hit = find_by_pattern(edits, ref_re)
        if hit:
            return Anchor(path, hit.line, hit.side, "symbol")

    # 4) Keyword — token-aware lookup so we don't re-introduce false-positive
    #    substring matches like `_authenticated -> auth`.
    keyword = (finding.get("keyword") or "").strip().lower()
    if keyword:
        hit = _find_added_line_with_keyword_token(edits, keyword)
        if hit:
            return Anchor(path, hit.line, hit.side, "keyword")

    # 5) Path-only fallback — the first "interesting" added line.
    signal = first_signal_line(edits)
    if signal is not None:
        return Anchor(path, signal.line, signal.side, "signal")
    return None


def anchors_for_findings(
    findings: Iterable[dict[str, Any]],
    line_index: dict[str, list[LineEdit]],
) -> list[tuple[dict[str, Any], Anchor]]:
    """Bulk resolve; drops findings that can't be anchored to a changed line."""
    out: list[tuple[dict[str, Any], Anchor]] = []
    for finding in findings:
        anchor = anchor_for_finding(finding, line_index)
        if anchor is not None:
            out.append((finding, anchor))
    return out


def _find_added_line_with_keyword_token(
    edits: Iterable[LineEdit],
    keyword: str,
) -> LineEdit | None:
    """First added line whose tokenized content contains ``keyword``."""
    for edit in edits:
        if edit.kind != "added":
            continue
        if keyword in tokenize(edit.text):
            return edit
    return None


def _first_int(*values: Any) -> int | None:
    for value in values:
        if value is None:
            continue
        try:
            n = int(value)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return None


def _side(value: Any) -> Side | None:
    if not value:
        return None
    upper = str(value).upper()
    if upper in {"LEFT", "RIGHT"}:
        return upper  # type: ignore[return-value]
    return None
