"""Parse unified-diff ``patch`` strings into per-line edit records.

Used by the inline-comment pipeline to map findings back to actual file lines
on GitHub. The GitHub review-comments endpoint requires a ``path`` + ``line``
+ ``side`` (LEFT/RIGHT); this module provides those coordinates from the
``patch`` text that already rides along with every changed file.

Nothing in here talks to GitHub — pure string parsing so we can unit-test it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal

# Hunk header: ``@@ -A,B +C,D @@`` or ``@@ -A +C @@`` (count defaults to 1).
_HUNK_RE = re.compile(
    r"^@@\s*-(?P<base_start>\d+)(?:,(?P<base_count>\d+))?"
    r"\s*\+(?P<head_start>\d+)(?:,(?P<head_count>\d+))?\s*@@",
)

Side = Literal["LEFT", "RIGHT"]
Kind = Literal["added", "removed", "context"]


@dataclass(frozen=True)
class LineEdit:
    """A single line emitted by the diff parser.

    Fields:
        side: ``"RIGHT"`` for the head/new file, ``"LEFT"`` for the base/old.
            Context lines are reported on the RIGHT side (head file) only —
            see ``parse_patch`` notes.
        line: 1-indexed line number on the indicated ``side``.
        kind: ``"added"`` / ``"removed"`` / ``"context"``.
        text: the line content, with the leading ``+``/``-``/`` `` stripped.
        hunk_start_head: head_line where the enclosing hunk begins (handy
            for picking a "representative" line for a hunk-scoped finding).
    """

    path: str
    side: Side
    line: int
    kind: Kind
    text: str
    hunk_start_head: int


def parse_patch(path: str, patch: str) -> list[LineEdit]:
    """Convert a unified-diff ``patch`` string into a flat list of ``LineEdit``.

    The GitHub review-comments API anchors comments to a single ``side``/``line``
    pair on either the base or the head file. We emit:

      * one ``LineEdit`` per added line (``side="RIGHT"``)
      * one ``LineEdit`` per removed line (``side="LEFT"``)
      * one ``LineEdit`` per context line (``side="RIGHT"``) — context lines
        are equally addressable on both sides, but inline comments almost
        always want to land on the head file, so we default to RIGHT and
        leave dual-side queries to callers via ``find_by_text``.

    Quirks handled:
      * ``\\ No newline at end of file`` markers are skipped.
      * Missing or empty ``patch`` yields an empty list (some GitHub diffs
        for binary or huge files come back without a patch body).
      * Hunks without an opening ``@@`` header are tolerated — those lines
        are dropped rather than crashing the pipeline.
    """
    if not patch:
        return []
    edits: list[LineEdit] = []
    base_line = 0
    head_line = 0
    hunk_start_head = 0
    in_hunk = False
    for raw in patch.splitlines():
        if raw.startswith("@@"):
            match = _HUNK_RE.match(raw)
            if not match:
                in_hunk = False
                continue
            base_line = int(match.group("base_start"))
            head_line = int(match.group("head_start"))
            hunk_start_head = head_line
            in_hunk = True
            continue
        if not in_hunk:
            # File header lines (``+++``, ``---``, ``diff --git``, …) before the
            # first hunk header. Ignore them.
            continue
        if raw.startswith(("+++", "---")):
            continue
        if raw.startswith("\\"):
            # `\ No newline at end of file`
            continue
        if raw.startswith("+"):
            edits.append(LineEdit(
                path=path,
                side="RIGHT",
                line=head_line,
                kind="added",
                text=raw[1:],
                hunk_start_head=hunk_start_head,
            ))
            head_line += 1
        elif raw.startswith("-"):
            edits.append(LineEdit(
                path=path,
                side="LEFT",
                line=base_line,
                kind="removed",
                text=raw[1:],
                hunk_start_head=hunk_start_head,
            ))
            base_line += 1
        else:
            # Context line (starts with a single space, or is empty).
            text = raw[1:] if raw.startswith(" ") else raw
            edits.append(LineEdit(
                path=path,
                side="RIGHT",
                line=head_line,
                kind="context",
                text=text,
                hunk_start_head=hunk_start_head,
            ))
            base_line += 1
            head_line += 1
    return edits


# ---------------------------------------------------------------------------
# Convenience queries over the parsed line set.
# ---------------------------------------------------------------------------


def added_lines(edits: Iterable[LineEdit]) -> list[LineEdit]:
    """Just the added (``+``) lines, in order."""
    return [e for e in edits if e.kind == "added"]


def first_signal_line(edits: Iterable[LineEdit]) -> LineEdit | None:
    """Pick the first "interesting" added line for a path-only finding.

    Skips imports, blank lines, and single-character lines so the comment
    lands where the reviewer's eye actually goes. Falls back to the first
    added line if every added line looks boilerplate.
    """
    addeds = added_lines(edits)
    if not addeds:
        return None
    for edit in addeds:
        stripped = edit.text.strip()
        if not stripped:
            continue
        if len(stripped) <= 1:
            continue
        if stripped.startswith(("import ", "from ", "//", "#", "/*", "*", "}", ")", "{")):
            continue
        return edit
    return addeds[0]


def find_by_text(
    edits: Iterable[LineEdit],
    needle: str,
    *,
    only_added: bool = True,
    case_insensitive: bool = True,
) -> LineEdit | None:
    """Return the first edit whose text contains ``needle``, or None.

    Used by the snippet/symbol/keyword anchor strategies: caller supplies the
    text (or regex-matched substring), we return the line coordinates.
    """
    if not needle:
        return None
    target = needle.lower() if case_insensitive else needle
    for edit in edits:
        if only_added and edit.kind != "added":
            continue
        haystack = edit.text.lower() if case_insensitive else edit.text
        if target in haystack:
            return edit
    return None


def find_by_pattern(
    edits: Iterable[LineEdit],
    pattern: re.Pattern[str],
    *,
    only_added: bool = True,
) -> LineEdit | None:
    """Return the first edit whose text matches ``pattern``, or None."""
    for edit in edits:
        if only_added and edit.kind != "added":
            continue
        if pattern.search(edit.text):
            return edit
    return None
