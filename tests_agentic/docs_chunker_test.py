"""Tests for the markdown-aware doc chunker."""

from __future__ import annotations

import unittest

from packages.history_store.docs_chunker import chunk_doc


SAMPLE_MD = """# Title

Intro paragraph.

## Section A

Content A1. Content A1. Content A1.

Content A2.

## Section B

Content B1.

### Subsection

Deeper content.
"""


PLAIN_TEXT = "Line. " * 400  # ~2400 chars


class DocsChunkerTest(unittest.TestCase):
    def test_chunk_doc_splits_markdown_by_headings(self) -> None:
        chunks = chunk_doc(
            repo_key="org/repo",
            path="docs/sample.md",
            language="markdown",
            content=SAMPLE_MD,
        )
        headings = [c["heading"] for c in chunks]
        # The title plus each H2/H3 should each produce at least one chunk.
        self.assertIn("Title", headings)
        self.assertIn("Section A", headings)
        self.assertIn("Section B", headings)
        self.assertIn("Subsection", headings)

    def test_chunk_doc_emits_metadata_fields(self) -> None:
        chunks = chunk_doc(
            repo_key="org/repo",
            path="docs/sample.md",
            language="markdown",
            content=SAMPLE_MD,
        )
        first = chunks[0]
        for key in ("repo_key", "path", "chunk_index", "heading", "text", "label"):
            self.assertIn(key, first)
        self.assertEqual(first["repo_key"], "org/repo")
        self.assertEqual(first["path"], "docs/sample.md")
        self.assertTrue(first["label"].startswith("mergeguard:doc:org/repo::docs/sample.md#chunk"))
        self.assertEqual(first["chunk_index"], 0)

    def test_chunk_doc_falls_back_to_character_windows_for_plain_text(self) -> None:
        chunks = chunk_doc(
            repo_key="org/repo",
            path="notes.txt",
            language="text",
            content=PLAIN_TEXT,
            max_chunk_chars=600,
            chunk_overlap_chars=100,
        )
        self.assertGreaterEqual(len(chunks), 3)
        # Adjacent windows must overlap.
        self.assertTrue(chunks[1]["text"].startswith(chunks[0]["text"][-100:]))

    def test_chunk_doc_respects_max_chunks(self) -> None:
        chunks = chunk_doc(
            repo_key="org/repo",
            path="huge.md",
            language="markdown",
            content="# H\n\n" + ("body " * 5000),
            max_chunk_chars=400,
            chunk_overlap_chars=0,
            max_chunks=4,
        )
        self.assertLessEqual(len(chunks), 4)


if __name__ == "__main__":
    unittest.main()
