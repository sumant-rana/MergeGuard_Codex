from __future__ import annotations

import unittest

from packages.core.diff_lines import (
    added_lines,
    find_by_text,
    first_signal_line,
    parse_patch,
)
from packages.core.inline_anchors import (
    anchor_for_finding,
    build_line_index,
)
from packages.core.inline_comments import (
    MAX_INLINE_PER_FILE,
    MAX_INLINE_PER_PR,
    build_inline_comments,
)

# ---------------------------------------------------------------------------
# Diff parser
# ---------------------------------------------------------------------------


SIMPLE_PATCH = """@@ -10,3 +10,5 @@
 context one
-removed one
+added one
+added two
 context two
"""


class DiffParserTest(unittest.TestCase):
    def test_parses_added_removed_and_context(self) -> None:
        edits = parse_patch("src/foo.ts", SIMPLE_PATCH)
        kinds = [e.kind for e in edits]
        self.assertEqual(kinds, ["context", "removed", "added", "added", "context"])

    def test_added_line_numbers_track_head_file(self) -> None:
        edits = parse_patch("src/foo.ts", SIMPLE_PATCH)
        added = added_lines(edits)
        self.assertEqual([e.line for e in added], [11, 12])
        for edit in added:
            self.assertEqual(edit.side, "RIGHT")

    def test_removed_line_uses_base_file_numbering(self) -> None:
        edits = parse_patch("src/foo.ts", SIMPLE_PATCH)
        removed = [e for e in edits if e.kind == "removed"]
        self.assertEqual(len(removed), 1)
        # Hunk starts at base line 10. The first line is context ("context
        # one") → base 10. The removed line is the SECOND base-file line,
        # so its number is 11.
        self.assertEqual(removed[0].line, 11)
        self.assertEqual(removed[0].side, "LEFT")

    def test_empty_patch_returns_empty(self) -> None:
        self.assertEqual(parse_patch("src/foo.ts", ""), [])

    def test_skips_no_newline_marker(self) -> None:
        patch = "@@ -1,1 +1,2 @@\n a\n+b\n\\ No newline at end of file\n"
        edits = parse_patch("src/foo.ts", patch)
        # Just the context + added, marker dropped.
        self.assertEqual([e.kind for e in edits], ["context", "added"])

    def test_multiple_hunks_reset_line_counters(self) -> None:
        patch = (
            "@@ -1,1 +1,2 @@\n"
            "+first hunk added\n"
            " context\n"
            "@@ -100,1 +101,2 @@\n"
            " other context\n"
            "+second hunk added\n"
        )
        added = added_lines(parse_patch("src/foo.ts", patch))
        self.assertEqual([e.line for e in added], [1, 102])


class SignalLineTest(unittest.TestCase):
    def test_skips_imports_to_find_real_code(self) -> None:
        patch = (
            "@@ -1,1 +1,5 @@\n"
            "+import { thing } from 'x'\n"
            "+\n"
            "+export function payRefund() {\n"
            "+  return true\n"
            "+}\n"
        )
        edits = parse_patch("src/refund.ts", patch)
        signal = first_signal_line(edits)
        self.assertIsNotNone(signal)
        self.assertIn("payRefund", signal.text)  # type: ignore[union-attr]

    def test_falls_back_to_first_added_if_only_boilerplate(self) -> None:
        patch = "@@ -1,0 +1,2 @@\n+import x\n+import y\n"
        edits = parse_patch("src/index.ts", patch)
        signal = first_signal_line(edits)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.text, "import x")  # type: ignore[union-attr]

    def test_returns_none_for_no_added_lines(self) -> None:
        patch = "@@ -1,2 +1,1 @@\n-one\n-two\n+merged\n"
        # one added → signal returns that line; verify the empty case separately
        edits = parse_patch("src/foo.ts", patch)
        self.assertIsNotNone(first_signal_line(edits))

    def test_returns_none_for_pure_deletion_diff(self) -> None:
        patch = "@@ -1,2 +1,0 @@\n-one\n-two\n"
        edits = parse_patch("src/foo.ts", patch)
        self.assertIsNone(first_signal_line(edits))


# ---------------------------------------------------------------------------
# Anchor resolver
# ---------------------------------------------------------------------------


class AnchorResolverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.line_index = build_line_index([
            {
                "path": "src/features/release-highlights/components/release-highlights-section.tsx",
                "patch": (
                    "@@ -1,3 +1,8 @@\n"
                    "+import { useState } from 'react'\n"
                    "+import { Button } from '@/components/ui/button'\n"
                    "+\n"
                    "+export function ReleaseHighlightsSection({ items }) {\n"
                    "+  const [filter, setFilter] = useState('All')\n"
                    "+  return <section>...</section>\n"
                    "+}\n"
                ),
            },
        ])

    def test_explicit_line_wins(self) -> None:
        anchor = anchor_for_finding(
            {
                "path": "src/features/release-highlights/components/release-highlights-section.tsx",
                "line": 4,
            },
            self.line_index,
        )
        self.assertIsNotNone(anchor)
        self.assertEqual(anchor.line, 4)  # type: ignore[union-attr]
        self.assertEqual(anchor.strategy, "explicit")  # type: ignore[union-attr]

    def test_symbol_anchors_to_declaration(self) -> None:
        anchor = anchor_for_finding(
            {
                "path": "src/features/release-highlights/components/release-highlights-section.tsx",
                "symbol": "ReleaseHighlightsSection",
            },
            self.line_index,
        )
        self.assertIsNotNone(anchor)
        self.assertEqual(anchor.strategy, "symbol")  # type: ignore[union-attr]
        # Declaration is line 4 in the head file.
        self.assertEqual(anchor.line, 4)  # type: ignore[union-attr]

    def test_snippet_anchors_to_match(self) -> None:
        anchor = anchor_for_finding(
            {
                "path": "src/features/release-highlights/components/release-highlights-section.tsx",
                "snippet": "useState('All')",
            },
            self.line_index,
        )
        self.assertIsNotNone(anchor)
        self.assertEqual(anchor.strategy, "snippet")  # type: ignore[union-attr]
        self.assertEqual(anchor.line, 5)  # type: ignore[union-attr]

    def test_keyword_resolver_does_not_match_underscore_authenticated(self) -> None:
        line_index = build_line_index([
            {
                "path": "src/routes/_authenticated/release-highlights/index.tsx",
                "patch": (
                    "@@ -0,0 +1,3 @@\n"
                    "+import { createFileRoute } from '@tanstack/react-router'\n"
                    "+\n"
                    "+export const Route = createFileRoute('/_authenticated/release-highlights/')\n"
                ),
            },
        ])
        anchor = anchor_for_finding(
            {
                "path": "src/routes/_authenticated/release-highlights/index.tsx",
                "keyword": "auth",
            },
            line_index,
        )
        # `_authenticated` is one token — `auth` should NOT match it. The
        # resolver falls through to the signal-line strategy instead.
        self.assertIsNotNone(anchor)
        self.assertEqual(anchor.strategy, "signal")  # type: ignore[union-attr]

    def test_returns_none_when_path_not_in_diff(self) -> None:
        self.assertIsNone(
            anchor_for_finding(
                {"path": "src/some/file/not/in/diff.ts"},
                self.line_index,
            )
        )

    def test_returns_none_when_finding_has_no_path(self) -> None:
        self.assertIsNone(anchor_for_finding({}, self.line_index))


# ---------------------------------------------------------------------------
# Inline-comment composer
# ---------------------------------------------------------------------------


class InlineCommentComposerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.changed_files = [
            {
                "path": "src/features/release-highlights/index.tsx",
                "patch": (
                    "@@ -0,0 +1,5 @@\n"
                    "+import { ReleaseHighlightsSection } from './components/release-highlights-section'\n"
                    "+\n"
                    "+export function ReleaseHighlights() {\n"
                    "+  return <ReleaseHighlightsSection />\n"
                    "+}\n"
                ),
            },
            {
                "path": "src/features/release-highlights/components/release-highlights-section.tsx",
                "patch": (
                    "@@ -0,0 +1,3 @@\n"
                    "+import { useState } from 'react'\n"
                    "+\n"
                    "+export function ReleaseHighlightsSection() { return null }\n"
                ),
            },
        ]

    def test_behavioral_delta_becomes_an_inline_comment(self) -> None:
        deltas = [
            {
                "title": "New authenticated route",
                "description": "Registers a new authenticated /release-highlights/ route.",
                "severity": "review_required",
                "paths": ["src/features/release-highlights/index.tsx"],
                "symbol": "ReleaseHighlights",
            },
        ]
        comments = build_inline_comments(
            changed_files=self.changed_files,
            behavioral_deltas=deltas,
            blockers=[],
            hotspots=[],
        )
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["path"], "src/features/release-highlights/index.tsx")
        self.assertEqual(comments[0]["severity"], "review_required")
        self.assertIn("New authenticated route", comments[0]["title"])
        self.assertIn("MergeGuard", comments[0]["body"])

    def test_warn_findings_are_filtered_out(self) -> None:
        blockers = [
            {
                "severity": "warn",
                "source_agent": "policy-gate",
                "path": "src/features/release-highlights/index.tsx",
                "message": "Just a warning",
            },
        ]
        comments = build_inline_comments(
            changed_files=self.changed_files,
            behavioral_deltas=[],
            blockers=blockers,
            hotspots=[],
        )
        self.assertEqual(comments, [])

    def test_per_file_cap_enforced(self) -> None:
        blockers = [
            {
                "severity": "review_required",
                "source_agent": "policy-gate",
                "path": "src/features/release-highlights/index.tsx",
                "message": "Finding A",
            },
            {
                "severity": "review_required",
                "source_agent": "slop-detector",
                "path": "src/features/release-highlights/index.tsx",
                "message": "Finding B",
            },
        ]
        comments = build_inline_comments(
            changed_files=self.changed_files,
            behavioral_deltas=[],
            blockers=blockers,
            hotspots=[],
        )
        self.assertEqual(len(comments), MAX_INLINE_PER_FILE)

    def test_intent_pseudo_paths_are_skipped(self) -> None:
        # Path-less / intent-id blockers can't be anchored to a line.
        blockers = [
            {
                "severity": "review_required",
                "source_agent": "intent-extractor",
                "path": "intent-7",
                "message": "Acceptance criterion missing",
            },
        ]
        comments = build_inline_comments(
            changed_files=self.changed_files,
            behavioral_deltas=[],
            blockers=blockers,
            hotspots=[],
        )
        self.assertEqual(comments, [])

    def test_total_pr_cap_enforced(self) -> None:
        files = [
            {
                "path": f"src/file_{i}.ts",
                "patch": f"@@ -0,0 +1,1 @@\n+export const value_{i} = {i}\n",
            }
            for i in range(MAX_INLINE_PER_PR + 3)
        ]
        blockers = [
            {
                "severity": "review_required",
                "source_agent": "policy-gate",
                "path": file["path"],
                "message": f"finding for {file['path']}",
            }
            for file in files
        ]
        comments = build_inline_comments(
            changed_files=files,
            behavioral_deltas=[],
            blockers=blockers,
            hotspots=[],
        )
        self.assertEqual(len(comments), MAX_INLINE_PER_PR)

    def test_block_severity_outranks_review_required(self) -> None:
        # Two findings on different files; the block-severity one must come
        # first regardless of insertion order.
        files = [
            {
                "path": "src/a.ts",
                "patch": "@@ -0,0 +1,1 @@\n+export const a = 1\n",
            },
            {
                "path": "src/b.ts",
                "patch": "@@ -0,0 +1,1 @@\n+export const b = 2\n",
            },
        ]
        blockers = [
            {
                "severity": "review_required",
                "source_agent": "policy-gate",
                "path": "src/a.ts",
                "message": "review thing",
            },
            {
                "severity": "block",
                "source_agent": "concept-classifier",
                "path": "src/b.ts",
                "message": "block thing",
            },
        ]
        comments = build_inline_comments(
            changed_files=files,
            behavioral_deltas=[],
            blockers=blockers,
            hotspots=[],
        )
        self.assertEqual(comments[0]["severity"], "block")
        self.assertEqual(comments[0]["path"], "src/b.ts")

    def test_sticky_anchor_is_linked_in_body(self) -> None:
        comments = build_inline_comments(
            changed_files=self.changed_files,
            behavioral_deltas=[
                {
                    "title": "Behavior added",
                    "description": "Adds a section.",
                    "paths": ["src/features/release-highlights/index.tsx"],
                },
            ],
            blockers=[],
            hotspots=[],
            sticky_anchor="https://github.com/o/r/pull/1#issuecomment-12345",
        )
        self.assertEqual(len(comments), 1)
        self.assertIn(
            "https://github.com/o/r/pull/1#issuecomment-12345",
            comments[0]["body"],
        )


# ---------------------------------------------------------------------------
# Find-by-text (smoke)
# ---------------------------------------------------------------------------


class FindByTextTest(unittest.TestCase):
    def test_finds_added_line_containing_needle(self) -> None:
        edits = parse_patch(
            "src/foo.ts",
            "@@ -0,0 +1,2 @@\n+const refund = doRefund()\n+const idempotency = key()\n",
        )
        hit = find_by_text(edits, "refund")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.kind, "added")  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
