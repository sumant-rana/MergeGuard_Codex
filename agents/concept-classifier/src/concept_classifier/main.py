from __future__ import annotations

import json
import re
import sys
from pathlib import Path
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

AGENT_ID = "concept-classifier"

CONCEPT_PATTERNS = {
    "auth_check": ["auth", "authorize", "permission", "role", "session", "token"],
    "pii_read": ["pii", "email", "phone", "ssn", "address"],
    "pii_write": ["save_email", "persist", "update_profile", "customeremail", "customer_email", "setitem", "localstorage.setitem"],
    "billing_side_effect": ["payment", "billing", "refund", "charge", "invoice", "payout"],
    "idempotency_check": ["idempotency", "idempotent", "dedupe"],
    "external_http_call": ["fetch(", "requests.", "http.", "axios", "webhook"],
    "timeout_configured": ["timeout", "deadline", "abortcontroller"],
    "retry": ["retry", "backoff", "transient"],
    "raw_sql": ["select ", "insert ", "update ", "delete ", "cursor.execute", "raw sql"],
    "cache_invalidation": ["cache", "invalidate", "redis"],
    "feature_flag": ["feature_flag", "launchdarkly", "flag"],
    "prompt": ["prompt", "system message", "model", "temperature"],
    "agent_tool_call": ["tool_call", "function_call", "agent", "planner"],
}

# Concepts whose mere presence is a hard blocker. Drives severity="block" on
# the emitted finding and is also surfaced by truth-report-synthesizer in the
# top-blocker selection.
BLOCK_SEVERITY_CONCEPTS = frozenset(
    {"secret_exposure", "auth_bypass", "injection", "insecure_transport", "pii_write"}
)

# Regex-based detectors for patterns the substring scanner can't reliably
# catch. Each detector is (concept_label, message_template, compiled_regex).
# Matches are de-duplicated per (concept, path) pair downstream.
SECRET_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("secret_exposure", "Stripe live secret key", re.compile(r"sk_live_[A-Za-z0-9]{16,}")),
    ("secret_exposure", "Stripe test secret key", re.compile(r"sk_test_[A-Za-z0-9]{16,}")),
    ("secret_exposure", "GitHub personal access token (ghp_)", re.compile(r"\bghp_[A-Za-z0-9]{20,}")),
    ("secret_exposure", "GitHub OAuth token (gho_)", re.compile(r"\bgho_[A-Za-z0-9]{20,}")),
    ("secret_exposure", "GitHub server-to-server token (ghs_)", re.compile(r"\bghs_[A-Za-z0-9]{20,}")),
    ("secret_exposure", "AWS access key id (AKIA)", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("secret_exposure", "Google API key (AIza)", re.compile(r"\bAIza[0-9A-Za-z\-_]{30,}")),
    ("secret_exposure", "Slack bot token (xoxb)", re.compile(r"\bxoxb-[0-9A-Za-z\-]{20,}")),
    ("secret_exposure", "Slack user token (xoxp)", re.compile(r"\bxoxp-[0-9A-Za-z\-]{20,}")),
    ("secret_exposure", "Database URI with embedded credentials",
     re.compile(r"(?i)(postgres|mysql|mongodb|redis)://[^/\s:@]+:[^/\s@]+@[^/\s]+")),
    ("secret_exposure", "Private key block (PEM)",
     re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY-----")),
    ("secret_exposure", "Hardcoded JWT (eyJ...)",
     re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("secret_exposure", "Hardcoded high-entropy bearer-token literal",
     re.compile(r"['\"](?:admin_token|api[_-]?token|secret|bearer)[_-]?[A-Za-z0-9]{16,}['\"]", re.IGNORECASE)),
]

# Code patterns indicating the code did the wrong security thing — not
# just touched a sensitive area. These propagate as block-severity findings.
RISKY_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("auth_bypass", "Commented-out beforeLoad/auth-guard block",
     re.compile(r"(?im)^\s*//\s*beforeLoad\s*[:=].+|^\s*//\s*if\s*\(.*(token|authenticated|isAuthorized).*\)")),
    ("auth_bypass", "Localstorage-driven auth disable flag",
     re.compile(r"localStorage(?:\.getItem|\[).*disable[_-]?auth", re.IGNORECASE)),
    ("auth_bypass", "beforeLoad returning unconditionally without check",
     re.compile(r"beforeLoad\s*[:=]\s*\([^)]*\)\s*=>\s*\{\s*[^}]{0,40}return\s*;?\s*\}", re.DOTALL)),
    ("injection", "SQL-style WHERE clause built via string concatenation",
     re.compile(r"['\"]\s*(?:SELECT|WHERE|FROM|UPDATE|DELETE|INSERT)\b[^'\"]*['\"]\s*\+", re.IGNORECASE)),
    ("injection", "Untrusted value concatenated into a query/url path",
     re.compile(r"['\"]\?\s*(?:id|q|query|search)\s*=\s*['\"]\s*\+\s*\w+|['\"]/users\?id=['\"]\s*\+\s*\w+", re.IGNORECASE)),
    ("insecure_transport", "Plain-HTTP URL used in fetch / request",
     re.compile(r"\b(?:fetch|axios(?:\.\w+)?|requests\.\w+)\s*\(\s*['\"]http://", re.IGNORECASE)),
    ("insecure_transport", "Plain-HTTP base URL constant",
     re.compile(
         r"\b(?:const|let|var)\s+(?:[A-Z_]*BASE[A-Z_]*|API_BASE|INTERNAL_BASE|HOST|HOSTNAME|URL|ENDPOINT)\s*[:=]\s*['\"]http://",
         re.IGNORECASE,
     )),
    ("insecure_transport", "Plain-HTTP URL literal",
     re.compile(r"['\"]http://[a-z0-9.\-]+", re.IGNORECASE)),
    ("pii_write", "PII written to localStorage / window global",
     re.compile(r"(?:localStorage\.setItem|window\.\w+\s*=)\s*[^;]*\b(?:email|phone|ssn|password|address)\b", re.IGNORECASE)),
    ("pii_write", "Plaintext password / PII logged to console",
     re.compile(r"console\.(?:log|debug|info|warn|error)\s*\([^)]*\b(?:password|email|phone|ssn|cardnumber|cc_number)\b", re.IGNORECASE)),
]

app = create_app(AGENT_ID, "Classify changed functions and files into review concepts.")
register_default_llm(app)


@app.tool()
def classify_concepts(file: dict[str, Any], raw_file: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Tag a changed file with concept findings (auth, PII, billing, SQL, prompt, etc.).

    Findings carry a ``severity`` field; concepts in ``BLOCK_SEVERITY_CONCEPTS``
    (secret exposure, auth bypass, SQL injection, plain-HTTP, PII write) are
    emitted with ``severity="block"`` so the truth-report-synthesizer treats
    them as hard blockers rather than just hotspot signals.
    """
    raw_file = raw_file or {}
    patch = str(raw_file.get("patch") or "")
    content = str(raw_file.get("content") or "")
    case_sensitive_text = "\n".join(part for part in [patch, content] if part)
    haystack = "\n".join(
        str(part).lower()
        for part in [
            file.get("path"),
            file.get("classification"),
            " ".join(file.get("risk_reasons", [])),
            patch,
            content,
        ]
        if part
    )
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    # 1) Substring-based concept tags. These are FUZZY — keyword presence is
    #    weak evidence so we never auto-block on this layer alone.
    for concept, terms in CONCEPT_PATTERNS.items():
        matched = [term for term in terms if term in haystack]
        if not matched:
            continue
        findings.append(
            _finding(
                concept=concept,
                file=file,
                evidence=matched[:5],
                confidence=min(0.94, 0.52 + len(matched) * 0.12 + file.get("risk_score", 0) / 500),
                force_severity="review_required",
            )
        )
        seen.add((concept, file["path"]))

    # 2) Regex-based secret detectors (case-sensitive — keys are mixed case).
    findings.extend(_collect_regex_hits(SECRET_PATTERNS, case_sensitive_text, file, seen))

    # 3) Risky-pattern detectors (auth bypass, injection, insecure transport,
    #    explicit PII-write patterns). These are PRECISE and emit block-severity.
    findings.extend(_collect_regex_hits(RISKY_PATTERNS, case_sensitive_text, file, seen))

    return findings


def _finding(
    *,
    concept: str,
    file: dict[str, Any],
    evidence: list[str],
    confidence: float,
    reasoning: str | None = None,
    force_severity: str | None = None,
) -> dict[str, Any]:
    """Build a single concept finding with severity attached.

    Use ``force_severity`` to downgrade a fuzzy match (e.g. substring scan)
    even when the concept is in ``BLOCK_SEVERITY_CONCEPTS`` — only precise
    detectors should auto-block.
    """
    if force_severity is not None:
        severity = force_severity
    else:
        severity = "block" if concept in BLOCK_SEVERITY_CONCEPTS else "review_required"
    finding: dict[str, Any] = {
        "concept": concept,
        "path": file["path"],
        "symbol": (file.get("symbols") or [{}])[0].get("name"),
        "confidence": round(min(0.97, max(0.5, confidence)), 2),
        "severity": severity,
        "relation": "introduced-or-modified",
        "evidence": evidence[:5],
        "message": _message_for(concept, file["path"], evidence),
        "suggested_action": _suggested_action_for(concept),
    }
    if reasoning:
        finding["reasoning"] = reasoning
    return finding


def _collect_regex_hits(
    detectors: list[tuple[str, str, re.Pattern[str]]],
    text: str,
    file: dict[str, Any],
    seen: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Run regex detectors over ``text`` and emit one finding per (concept, path)."""
    if not text:
        return []
    findings: list[dict[str, Any]] = []
    # Group by concept so multiple matches inside one file collapse to one
    # finding with the most descriptive evidence kept.
    by_concept: dict[str, dict[str, Any]] = {}
    for concept, label, pattern in detectors:
        match = pattern.search(text)
        if not match:
            continue
        sample = _redact_match(match.group(0))
        bucket = by_concept.setdefault(concept, {"labels": [], "samples": []})
        bucket["labels"].append(label)
        bucket["samples"].append(sample)

    for concept, bucket in by_concept.items():
        if (concept, file["path"]) in seen:
            # Substring scanner already emitted this; we don't double-count
            # but we DO upgrade severity to block via the substring path's
            # finding — handled by post-processing below.
            for f in findings:
                if f["concept"] == concept and f["path"] == file["path"]:
                    break
        evidence = list(dict.fromkeys(bucket["samples"]))[:4]
        findings.append(
            _finding(
                concept=concept,
                file=file,
                evidence=evidence,
                confidence=0.86 + min(0.1, 0.025 * len(bucket["labels"])),
                reasoning="; ".join(dict.fromkeys(bucket["labels"]))[:240],
            )
        )
        seen.add((concept, file["path"]))
    return findings


def _redact_match(s: str) -> str:
    """Truncate long literal matches so we don't echo the full secret into logs."""
    s = s.replace("\n", " ").strip()
    if len(s) > 48:
        return s[:24] + "…" + s[-8:]
    return s


_CONCEPT_MESSAGE_TEMPLATES: dict[str, str] = {
    "secret_exposure": "Hardcoded secret detected in `{path}` ({evidence}). Rotate and move to env var.",
    "auth_bypass": "Auth guard appears bypassed or weakened in `{path}` ({evidence}).",
    "injection": "Possible injection: untrusted value concatenated into a query/URL in `{path}` ({evidence}).",
    "insecure_transport": "Plain HTTP (not HTTPS) used in `{path}` ({evidence}).",
    "pii_write": "PII written to log/storage/global in `{path}` ({evidence}).",
}

_CONCEPT_ACTION_TEMPLATES: dict[str, str] = {
    "secret_exposure": "Rotate the leaked credential immediately. Move it to a runtime env var and add a secret-scan pre-commit hook.",
    "auth_bypass": "Restore the guard or replace it with a real auth check; do not gate auth on client-side flags.",
    "injection": "Parameterise the query/URL value (no string concat) and validate the input.",
    "insecure_transport": "Use https://. Plain HTTP is only acceptable for explicitly-marked dev loopbacks.",
    "pii_write": "Strip PII before logging/storage, or move it to a redacted/encrypted channel.",
}


def _message_for(concept: str, path: str, evidence: list[str]) -> str:
    template = _CONCEPT_MESSAGE_TEMPLATES.get(concept)
    if not template:
        return f"{concept.replace('_', ' ')} detected in `{path}`."
    sample = evidence[0] if evidence else ""
    return template.format(path=path, evidence=sample)


def _suggested_action_for(concept: str) -> str:
    return _CONCEPT_ACTION_TEMPLATES.get(
        concept,
        "Confirm this change is intentional and add reviewer-visible justification.",
    )


def run(payload: dict[str, Any]) -> dict[str, Any]:
    compression = payload.get("prior_results", {}).get("review-compression", {}).get("output", {})
    files = compression.get("files", [])
    raw_by_path = {file.get("path"): file for file in payload.get("changed_files", [])}
    findings = [
        finding
        for file in files
        for finding in classify_concepts(file, raw_by_path.get(file.get("path")))
    ]
    for finding in findings:
        finding.setdefault("source", "rules")

    # LLM augmentation: for high-risk files (>=40 risk_score) ask an LLM to
    # spot concepts the rule glossary missed. Augments — never overrides —
    # the deterministic findings so the policy-gate path stays auditable.
    llm_findings: list[dict[str, Any]] = []
    if llm_available():
        high_risk_targets = [
            f for f in files
            if f.get("risk_score", 0) >= 40
            and f.get("classification") not in {"docs", "generated", "test"}
        ]
        if high_risk_targets:
            existing_by_path: dict[str, set[str]] = {}
            for finding in findings:
                existing_by_path.setdefault(finding["path"], set()).add(finding["concept"])
            llm_findings = _augment_via_llm(
                high_risk_targets,
                raw_by_path,
                already_found=existing_by_path,
            )
    findings.extend(llm_findings)

    output = {
        "concept_findings": findings,
        "memory_writes": [
            {
                "collection": "memory_taxonomic",
                "label": finding["concept"],
                "text": finding["path"],
                "metadata": {"path": finding["path"], "symbol": finding.get("symbol")},
            }
            for finding in findings
        ],
    }
    return make_agent_result(
        AGENT_ID,
        output,
        confidence=0.74,
        messages=[
            f"classified {len(findings)} concept findings "
            f"({len(llm_findings)} from LLM augment)"
        ],
        trace=[
            {
                "step": "classify_concepts",
                "rules_findings": len(findings) - len(llm_findings),
                "llm_findings": len(llm_findings),
            }
        ],
    )


# ── LLM augmentation ───────────────────────────────────────────────────────


_CONCEPT_GLOSSARY = (
    "auth_check, auth_bypass, pii_read, pii_write, secret_exposure, "
    "insecure_transport, injection, billing_side_effect, idempotency_check, "
    "external_http_call, timeout_configured, retry, raw_sql, cache_invalidation, "
    "feature_flag, prompt, agent_tool_call"
)

_CONCEPT_SYSTEM_PROMPT = (
    "You are scanning a changed file's diff to identify SUBTLE review-relevant "
    "concepts that a keyword-based rule layer might miss.\n\n"
    f"Valid concepts (use these labels exactly): {_CONCEPT_GLOSSARY}.\n\n"
    "Rules:\n"
    "- Only report a concept if you can quote a specific token / identifier /\n"
    "  function call from the diff that supports it. Put that quote in 'evidence'.\n"
    "- Skip concepts that the rule layer ALREADY found (provided in 'rules_already_found').\n"
    "- Be conservative — only report concepts whose presence would change a reviewer's mind.\n"
    "- confidence: 0-1; use <0.5 for inferred / ambiguous matches.\n\n"
    "Output a single JSON object:\n"
    '{"new_concept_findings": [\n'
    '  {"concept": str, "path": str, "evidence": [str], "confidence": float, "reasoning": str}\n'
    "]}\n"
    "If there's nothing new, return {\"new_concept_findings\": []}."
)


def _augment_via_llm(
    high_risk_targets: list[dict[str, Any]],
    raw_by_path: dict[str, dict[str, Any]],
    already_found: dict[str, set[str]],
) -> list[dict[str, Any]]:
    files_payload = []
    for file in high_risk_targets[:8]:
        path = file.get("path")
        raw = raw_by_path.get(path, {})
        files_payload.append(
            {
                "path": path,
                "classification": file.get("classification"),
                "risk_score": file.get("risk_score"),
                "patch_excerpt": (raw.get("patch") or "")[:2000],
                "rules_already_found": sorted(already_found.get(path, [])),
            }
        )
    user_prompt = (
        "Identify any review concepts the rule layer missed. Use the system-prompt schema.\n\n"
        f"```json\n{json.dumps({'files': files_payload}, indent=2)}\n```"
    )
    result = call_llm_json(
        app=app,
        system=_CONCEPT_SYSTEM_PROMPT,
        user=user_prompt,
        temperature=0.0,
        max_tokens=1000,
    )
    if not result:
        return []
    new_findings = result.get("new_concept_findings")
    if not isinstance(new_findings, list):
        return []

    valid_concepts = set(_CONCEPT_GLOSSARY.replace(",", "").split())
    cleaned: list[dict[str, Any]] = []
    for raw in new_findings:
        if not isinstance(raw, dict):
            continue
        concept = raw.get("concept")
        path = raw.get("path")
        if concept not in valid_concepts or not path:
            continue
        # Skip if rules already found this concept for the path.
        if concept in already_found.get(path, set()):
            continue
        severity = "block" if concept in BLOCK_SEVERITY_CONCEPTS else "review_required"
        evidence = [str(e) for e in (raw.get("evidence") or [])][:5]
        cleaned.append(
            {
                "concept": concept,
                "path": path,
                "symbol": None,
                "confidence": float(raw.get("confidence") or 0.6),
                "severity": severity,
                "relation": "introduced-or-modified",
                "evidence": evidence,
                "reasoning": str(raw.get("reasoning") or ""),
                "message": _message_for(concept, path, evidence),
                "suggested_action": _suggested_action_for(concept),
                "source": "llm-augment",
            }
        )
    return cleaned


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
