"""Unit tests for the GitHub REST-based docs collector."""

from __future__ import annotations

import base64
import unittest

from packages.github_pr.docs_collector import (
    collect_docs,
    is_doc_path,
    resolve_paths,
)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class FakeTransport:
    """Routes GET URLs to scripted JSON responses by prefix."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str, token: str) -> object:
        self.calls.append(url)
        for prefix, response in self.routes.items():
            if url.startswith(prefix):
                return response
        return None


REPO_PREFIX = "https://api.github.com/repos/mongodb/example-service"


class DocsCollectorTest(unittest.TestCase):
    def test_is_doc_path_accepts_known_extensions(self) -> None:
        self.assertTrue(is_doc_path("README.md"))
        self.assertTrue(is_doc_path("docs/setup.md"))
        self.assertTrue(is_doc_path("docs/setup.rst"))
        self.assertTrue(is_doc_path("README"))  # plain README is allowed
        self.assertFalse(is_doc_path("src/server.py"))
        self.assertFalse(is_doc_path("image.png"))

    def test_resolve_paths_defaults_to_readme_and_docs(self) -> None:
        resolved = resolve_paths(None)
        self.assertIn("README.md", resolved)
        self.assertIn("docs/", resolved)

    def test_resolve_paths_keeps_user_additions(self) -> None:
        resolved = resolve_paths(["README.md", "wiki/", "ARCHITECTURE.md"])
        self.assertIn("ARCHITECTURE.md", resolved)
        self.assertIn("wiki/", resolved)

    def test_collect_docs_walks_folder_and_fetches_files(self) -> None:
        # /docs/ is requested as a folder → walk via git/trees, then fetch
        # each file via contents API.
        routes = {
            f"{REPO_PREFIX}/contents/README.md": {
                "type": "file",
                "path": "README.md",
                "sha": "rs",
                "size": 7,
                "encoding": "base64",
                "content": _b64("# Hello"),
            },
            f"{REPO_PREFIX}/contents/docs?ref=main": [
                {"type": "file", "path": "docs/setup.md", "sha": "ds", "size": 5},
                {"type": "file", "path": "docs/image.png", "sha": "img", "size": 999},
                {"type": "dir", "path": "docs/api", "sha": "dapi"},
            ],
            f"{REPO_PREFIX}/contents/docs/api?ref=main": [
                {"type": "file", "path": "docs/api/index.md", "sha": "ai", "size": 8},
            ],
            f"{REPO_PREFIX}/contents/docs/setup.md": {
                "type": "file",
                "path": "docs/setup.md",
                "sha": "ds",
                "size": 5,
                "encoding": "base64",
                "content": _b64("setup"),
            },
            f"{REPO_PREFIX}/contents/docs/api/index.md": {
                "type": "file",
                "path": "docs/api/index.md",
                "sha": "ai",
                "size": 8,
                "encoding": "base64",
                "content": _b64("api docs"),
            },
        }
        transport = FakeTransport(routes)
        result = collect_docs(
            repo_full_name="mongodb/example-service",
            token="t",
            transport=transport,
            paths=["README.md", "docs/"],
            default_branch="main",
        )

        paths = sorted(doc["path"] for doc in result["docs"])
        self.assertEqual(paths, ["README.md", "docs/api/index.md", "docs/setup.md"])
        readme = next(d for d in result["docs"] if d["path"] == "README.md")
        self.assertEqual(readme["content"], "# Hello")
        self.assertEqual(readme["repo_key"], "mongodb/example-service")
        # The image is non-doc and should not appear, but the walker still
        # reported it as seen for the scan summary.
        self.assertGreaterEqual(result["scan_summary"]["files_seen"], 4)
        self.assertEqual(result["scan_summary"]["docs_indexed"], 3)
        self.assertGreaterEqual(result["scan_summary"]["files_skipped"], 1)

    def test_collect_docs_respects_max_bytes_per_file(self) -> None:
        big_content = "A" * 10_000
        routes = {
            f"{REPO_PREFIX}/contents/README.md": {
                "type": "file",
                "path": "README.md",
                "sha": "rs",
                "size": len(big_content),
                "encoding": "base64",
                "content": _b64(big_content),
            },
        }
        transport = FakeTransport(routes)
        result = collect_docs(
            repo_full_name="mongodb/example-service",
            token="t",
            transport=transport,
            paths=["README.md"],
            default_branch="main",
            max_bytes_per_file=1024,
        )
        readme = result["docs"][0]
        self.assertEqual(len(readme["content"]), 1024)
        self.assertTrue(readme.get("truncated"))

    def test_collect_docs_handles_missing_paths(self) -> None:
        routes = {
            f"{REPO_PREFIX}/contents/README.md": None,
            f"{REPO_PREFIX}/contents/docs?ref=main": None,
        }
        transport = FakeTransport(routes)
        result = collect_docs(
            repo_full_name="mongodb/example-service",
            token="t",
            transport=transport,
            paths=["README.md", "docs/"],
            default_branch="main",
        )
        self.assertEqual(result["docs"], [])
        self.assertEqual(result["scan_summary"]["docs_indexed"], 0)
        # The collector should surface the misses as warnings, not crash.
        self.assertTrue(result["warnings"])


if __name__ == "__main__":
    unittest.main()
