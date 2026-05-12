from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any


RISK_KEYWORDS = [
    "auth",
    "authorize",
    "token",
    "payment",
    "billing",
    "refund",
    "charge",
    "pii",
    "email",
    "sql",
    "migration",
    "retry",
    "timeout",
    "prompt",
    "secret",
    "agent",
    "webhook",
]

SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
TEST_MARKERS = ("/test/", "/tests/", "/__tests__/", ".test.", ".spec.", "_test.py")
PROMPT_PATTERNS = ("prompts/**", "agents/**", "**/*.prompt.md", "**/*.prompt", "**/*.jinja", "**/*.tmpl")
GENERATED_PATTERNS = ("*.lock", "*.snap", "dist/**", "build/**", "**/__generated__/**", "**/*.min.js")


def normalize_path(path: str) -> str:
    return str(PurePosixPath(str(path).replace("\\", "/")))


def language_for(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    return {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".md": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
    }.get(suffix, "unknown")


def is_generated(path: str) -> bool:
    clean = normalize_path(path)
    return any(fnmatch(clean, pattern) for pattern in GENERATED_PATTERNS)


def is_test(path: str) -> bool:
    clean = f"/{normalize_path(path).lower()}"
    return any(marker in clean for marker in TEST_MARKERS)


def is_prompt(path: str) -> bool:
    clean = normalize_path(path)
    return any(fnmatch(clean, pattern) for pattern in PROMPT_PATTERNS)


def is_docs(path: str) -> bool:
    clean = normalize_path(path).lower()
    return clean.startswith("docs/") or clean.endswith((".md", ".mdx", ".rst", ".txt"))


def risk_hits(*parts: str) -> list[str]:
    haystack = "\n".join(str(part).lower() for part in parts if part)
    return [keyword for keyword in RISK_KEYWORDS if keyword in haystack]


def important_terms(text: str) -> list[str]:
    words = re.sub(r"[^a-zA-Z0-9_/-]+", " ", text).lower().split()
    stop = {"that", "this", "with", "from", "into", "should", "must", "need", "needs", "review"}
    return [word for word in words if len(word) >= 4 and word not in stop][:16]


def extract_symbols(path: str, patch: str = "", content: str = "") -> list[dict[str, Any]]:
    source = "\n".join(part for part in [patch, content] if part)
    patterns = [
        ("class", re.compile(r"^[+\s]*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)", re.M)),
        ("function", re.compile(r"^[+\s]*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.M)),
        ("function", re.compile(r"^[+\s]*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=", re.M)),
        ("function", re.compile(r"^[+\s]*(?:async\s+)?def\s+([A-Za-z_][\w]*)", re.M)),
    ]
    symbols: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind, pattern in patterns:
        for match in pattern.finditer(source):
            name = match.group(1)
            key = f"{kind}:{name}"
            if key in seen:
                continue
            seen.add(key)
            symbols.append({"name": name, "kind": kind, "confidence": 0.82})
    if symbols:
        return symbols
    return [{"name": PurePosixPath(path).stem, "kind": "file", "confidence": 0.42}]


def codeowners_for(path: str, codeowners: str = "") -> list[str]:
    clean = normalize_path(path)
    matches: list[tuple[int, list[str]]] = []
    for raw in codeowners.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern, *owners = line.split()
        normalized_pattern = pattern.lstrip("/")
        if normalized_pattern.endswith("/"):
            hit = clean.startswith(normalized_pattern)
        else:
            hit = fnmatch(clean, normalized_pattern) or clean.startswith(f"{normalized_pattern.rstrip('/')}/")
        if hit and owners:
            matches.append((len(normalized_pattern.replace("*", "")), owners))
    if not matches:
        return ["unassigned"]
    return sorted(matches, key=lambda item: item[0], reverse=True)[0][1]
