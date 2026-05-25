"""GitHub REST docs collector for the ``docs-indexer`` onboarding agent.

The collector is decoupled from the network: it takes a ``transport``
callable ``(url, token) -> json`` so unit tests can stub it. The default
transport reuses :func:`packages.github_pr.pr_fetcher._get_json`.

Why REST and not the ``gh`` CLI: see ``pr_history_collector`` — the same
reasoning applies. The agent runs in containers without ``gh``.

Two endpoints power the walker:

- ``GET /repos/{repo}/contents/{path}?ref={branch}`` returns either a
  file blob (with ``encoding=base64`` content) or a folder listing.
- We do **not** use the Git Trees API today: ``contents`` for files
  returns the body directly in one call, and recursive folder walks
  through ``contents`` (per directory) keep the implementation small.
  We can switch to Trees if that becomes a hotspot.
"""

from __future__ import annotations

import base64
from typing import Any, Callable

from .pr_fetcher import GITHUB_API, _get_json

Transport = Callable[[str, str], Any]

DEFAULT_DOC_EXTENSIONS: tuple[str, ...] = (".md", ".mdx", ".rst", ".txt", ".adoc")
README_NAMES: frozenset[str] = frozenset({"readme", "readme.md", "readme.mdx", "readme.rst", "readme.txt"})

DEFAULT_PATHS: tuple[str, ...] = ("README.md", "docs/")
DEFAULT_MAX_FILES = 1000
DEFAULT_MAX_BYTES_PER_FILE = 256 * 1024
DEFAULT_MAX_DIR_DEPTH = 5


def _default_transport(url: str, token: str) -> Any:
    return _get_json(url, token)


def is_doc_path(path: str) -> bool:
    """Return True when ``path`` looks like a documentation file.

    We accept the default doc extensions, plus the conventional
    ``README`` filename with or without an extension. Binaries and
    source files are rejected so the walker doesn't waste fetches on
    them.
    """
    if not path:
        return False
    name = path.rsplit("/", 1)[-1].lower()
    if name in README_NAMES:
        return True
    lowered = path.lower()
    return any(lowered.endswith(ext) for ext in DEFAULT_DOC_EXTENSIONS)


def resolve_paths(paths: list[str] | None) -> list[str]:
    """Resolve the path list, prepending the README + /docs defaults.

    The defaults are always added when not already present, so callers
    that pass extra entries (``["ARCHITECTURE.md", "wiki/"]``) still get
    the conventional locations indexed.
    """
    seen: set[str] = set()
    resolved: list[str] = []
    for path in list(DEFAULT_PATHS) + list(paths or []):
        normalized = path.strip()
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        resolved.append(normalized)
    return resolved


def _is_folder_request(path: str) -> bool:
    return path.endswith("/")


def _normalize_folder(path: str) -> str:
    return path.rstrip("/")


def _decode_blob(
    raw: dict[str, Any],
    *,
    max_bytes: int,
) -> tuple[str, bool, str | None]:
    """Return ``(text, truncated, decode_error)``.

    The third element is non-None when a base64 decode failed so the
    caller can surface that as an explicit warning rather than silently
    dropping the file.
    """
    encoding = (raw.get("encoding") or "").lower()
    decode_error: str | None = None
    if encoding != "base64":
        text = str(raw.get("content") or "")
    else:
        payload = (raw.get("content") or "").encode("ascii", "ignore")
        try:
            text = base64.b64decode(payload, validate=False).decode("utf-8", "replace")
        except (ValueError, TypeError) as exc:
            text = ""
            decode_error = f"{type(exc).__name__}: {exc}"
    truncated = False
    if len(text) > max_bytes:
        text = text[:max_bytes]
        truncated = True
    return text, truncated, decode_error


def _file_record(
    *,
    repo_key: str,
    raw: dict[str, Any],
    content: str,
    truncated: bool,
) -> dict[str, Any]:
    path = str(raw.get("path") or "")
    return {
        "repo_key": repo_key,
        "path": path,
        "sha": str(raw.get("sha") or ""),
        "size": int(raw.get("size") or len(content)),
        "content": content,
        "content_size": len(content),
        "language": _language_for(path),
        "truncated": truncated,
        "source": "github",
    }


def _language_for(path: str) -> str:
    lowered = path.lower()
    if lowered.endswith(".md") or lowered.endswith(".mdx"):
        return "markdown"
    if lowered.endswith(".rst"):
        return "rst"
    if lowered.endswith(".adoc"):
        return "asciidoc"
    if lowered.endswith(".txt"):
        return "text"
    return "other"


def _fetch_file(
    *,
    request: Transport,
    api_base_url: str,
    repo_full_name: str,
    path: str,
    token: str,
    ref: str,
    max_bytes: int,
    warnings: list[str],
) -> dict[str, Any] | None:
    url = f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/contents/{path}"
    if ref:
        url = f"{url}?ref={ref}"
    try:
        raw = request(url, token)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"fetch {path} failed: {type(exc).__name__}: {exc}")
        return None
    if not isinstance(raw, dict) or raw.get("type") != "file":
        return None
    content, truncated, decode_error = _decode_blob(raw, max_bytes=max_bytes)
    if decode_error:
        warnings.append(f"decode {path} failed: {decode_error}")
        return None
    if not content:
        return None
    return _file_record(
        repo_key=repo_full_name,
        raw={**raw, "path": raw.get("path") or path},
        content=content,
        truncated=truncated,
    )


def _list_folder(
    *,
    request: Transport,
    api_base_url: str,
    repo_full_name: str,
    folder: str,
    token: str,
    ref: str,
    warnings: list[str],
) -> list[dict[str, Any]] | None:
    url = (
        f"{api_base_url.rstrip('/')}/repos/{repo_full_name}/contents/"
        f"{_normalize_folder(folder)}"
    )
    if ref:
        url = f"{url}?ref={ref}"
    try:
        raw = request(url, token)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"list {folder} failed: {type(exc).__name__}: {exc}")
        return None
    if not isinstance(raw, list):
        return None
    return raw


def collect_docs(
    *,
    repo_full_name: str,
    token: str,
    transport: Transport | None = None,
    paths: list[str] | None = None,
    default_branch: str = "main",
    api_base_url: str = GITHUB_API,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes_per_file: int = DEFAULT_MAX_BYTES_PER_FILE,
    max_dir_depth: int = DEFAULT_MAX_DIR_DEPTH,
) -> dict[str, Any]:
    """Walk the resolved paths and return doc records + scan summary.

    Folder paths (ending in ``/``) are walked recursively up to
    ``max_dir_depth``. Non-doc files are counted in the summary but not
    persisted. Each fetched file is capped at ``max_bytes_per_file``.
    """
    request = transport or _default_transport
    resolved = resolve_paths(paths)

    docs: list[dict[str, Any]] = []
    warnings: list[str] = []
    files_seen = 0
    files_skipped = 0

    def _process_file_entry(entry: dict[str, Any], *, fetched: dict | None = None) -> None:
        nonlocal files_seen, files_skipped
        files_seen += 1
        path = str(entry.get("path") or "")
        if not is_doc_path(path):
            files_skipped += 1
            return
        if len(docs) >= max_files:
            files_skipped += 1
            return
        record = fetched
        if record is None:
            record = _fetch_file(
                request=request,
                api_base_url=api_base_url,
                repo_full_name=repo_full_name,
                path=path,
                token=token,
                ref=default_branch,
                max_bytes=max_bytes_per_file,
                warnings=warnings,
            )
        if record is None:
            files_skipped += 1
            return
        docs.append(record)

    def _walk(folder: str, depth: int) -> None:
        if depth > max_dir_depth:
            warnings.append(f"max_dir_depth reached while walking {folder}")
            return
        listing = _list_folder(
            request=request,
            api_base_url=api_base_url,
            repo_full_name=repo_full_name,
            folder=folder,
            token=token,
            ref=default_branch,
            warnings=warnings,
        )
        if listing is None:
            warnings.append(f"folder {folder} not found or unreadable")
            return
        for entry in listing:
            entry_type = entry.get("type")
            if entry_type == "file":
                _process_file_entry(entry)
                if len(docs) >= max_files:
                    return
            elif entry_type == "dir":
                _walk(f"{entry.get('path')}/", depth + 1)
                if len(docs) >= max_files:
                    return

    for path in resolved:
        if len(docs) >= max_files:
            break
        if _is_folder_request(path):
            _walk(path, 1)
        else:
            record = _fetch_file(
                request=request,
                api_base_url=api_base_url,
                repo_full_name=repo_full_name,
                path=path,
                token=token,
                ref=default_branch,
                max_bytes=max_bytes_per_file,
                warnings=warnings,
            )
            files_seen += 1
            if record is None:
                files_skipped += 1
                warnings.append(f"file {path} not found")
                continue
            if not is_doc_path(record["path"]):
                files_skipped += 1
                continue
            docs.append(record)

    return {
        "docs": docs,
        "warnings": warnings,
        "scan_summary": {
            "paths_requested": resolved,
            "files_seen": files_seen,
            "files_skipped": files_skipped,
            "docs_indexed": len(docs),
        },
    }
