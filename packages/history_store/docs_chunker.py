"""Doc chunker used by the ``docs-indexer`` agent before embedding.

Markdown files are split on ATX headings (``#``, ``##``, ``###``) so each
chunk carries semantic context for downstream retrieval. Non-markdown
files fall back to overlapping character windows.

Every chunk record is what eventually goes to:

- ``app.memory.save_semantic(...)`` (text + label + user_id=repo_key),
- ``store.save_doc_chunk_metadata(...)`` (audit metadata).
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_MAX_CHUNK_CHARS = 1200
DEFAULT_CHUNK_OVERLAP_CHARS = 200
DEFAULT_MAX_CHUNKS_PER_DOC = 25

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", text.strip().lower()).strip("-")
    return cleaned or "section"


def _label_for(repo_key: str, path: str, chunk_index: int, heading_slug: str) -> str:
    return f"mergeguard:doc:{repo_key}::{path}#chunk{chunk_index}-{heading_slug}"


def _markdown_sections(content: str) -> list[tuple[str, str]]:
    """Return ``[(heading, body), ...]`` for a markdown document.

    The body is the text between this heading and the next heading (or
    end of file). Documents without a leading heading get a synthetic
    ``"_preamble"`` entry so any pre-heading content is still indexed.
    """
    sections: list[tuple[str, str]] = []
    matches = list(_HEADING_RE.finditer(content))
    if not matches:
        return [("_preamble", content)]
    if matches[0].start() > 0:
        sections.append(("_preamble", content[: matches[0].start()].strip()))
    for index, match in enumerate(matches):
        heading_text = match.group(2).strip()
        body_start = match.end()
        body_end = (
            matches[index + 1].start() if index + 1 < len(matches) else len(content)
        )
        body = content[body_start:body_end].strip()
        sections.append((heading_text, body))
    return [(heading, body) for heading, body in sections if heading or body]


def _split_window(text: str, *, max_chars: int, overlap: int) -> list[str]:
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    step = max(max_chars - overlap, 1)
    windows: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        windows.append(text[start:end])
        if end == len(text):
            break
        start += step
    return windows


def chunk_doc(
    *,
    repo_key: str,
    path: str,
    language: str,
    content: str,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    chunk_overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS,
    max_chunks: int = DEFAULT_MAX_CHUNKS_PER_DOC,
) -> list[dict[str, Any]]:
    """Return chunk records ready for embedding + metadata persistence.

    Markdown-heading splitting is used when ``language == "markdown"``;
    everything else falls back to overlapping character windows so
    plain-text README files (no headings) still produce usable chunks.
    """
    if not content:
        return []

    chunks: list[dict[str, Any]] = []
    if language == "markdown":
        sections = _markdown_sections(content)
        for heading, body in sections:
            if not body.strip():
                continue
            for window in _split_window(
                body, max_chars=max_chunk_chars, overlap=chunk_overlap_chars
            ):
                chunks.append({"heading": heading, "text": window})
                if len(chunks) >= max_chunks:
                    break
            if len(chunks) >= max_chunks:
                break
    else:
        for window in _split_window(
            content, max_chars=max_chunk_chars, overlap=chunk_overlap_chars
        ):
            chunks.append({"heading": "", "text": window})
            if len(chunks) >= max_chunks:
                break

    out: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        heading = chunk["heading"]
        slug = _slugify(heading) if heading else "chunk"
        out.append(
            {
                "repo_key": repo_key,
                "path": path,
                "chunk_index": index,
                "heading": _display_heading(heading),
                "heading_slug": slug,
                "text": chunk["text"],
                "label": _label_for(repo_key, path, index, slug),
            }
        )
    return out


def _display_heading(heading: str) -> str:
    """Hide the synthetic ``_preamble`` marker from external metadata."""
    if heading == "_preamble":
        return ""
    return heading
