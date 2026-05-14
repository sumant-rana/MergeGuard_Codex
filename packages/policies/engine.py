from __future__ import annotations

from typing import Any


def default_policy_pack() -> dict[str, Any]:
    return {
        "name": "MergeGuard Demo Policy",
        "version": 1,
        "rules": [
            # Hard blockers — no requirement field means "this concept alone is the violation".
            {
                "id": "no-hardcoded-secrets",
                "when": "secret_exposure",
                "severity": "block",
                "owner": "@security",
                "override_allowed": False,
                "message_override": "Hardcoded secret detected — rotate and move to env var.",
                "suggested_action_override": "Rotate the leaked credential immediately. Move it to a runtime env var; add a secret-scan pre-commit hook.",
            },
            {
                "id": "no-auth-bypass",
                "when": "auth_bypass",
                "severity": "block",
                "owner": "@security",
                "override_allowed": False,
                "message_override": "Auth guard appears bypassed or weakened — block until restored.",
                "suggested_action_override": "Restore the real auth check; do not gate auth on client-controlled flags.",
            },
            {
                "id": "no-injection-pattern",
                "when": "injection",
                "severity": "block",
                "owner": "@security",
                "override_allowed": True,
                "message_override": "Possible injection: untrusted value concatenated into a query/URL.",
                "suggested_action_override": "Parameterise the query and validate the input before use.",
            },
            {
                "id": "no-plain-http",
                "when": "insecure_transport",
                "severity": "block",
                "owner": "@security",
                "override_allowed": True,
                "message_override": "Plain HTTP (not HTTPS) detected for an outbound request.",
                "suggested_action_override": "Use https:// — plain HTTP is only acceptable for explicitly-marked dev loopbacks.",
            },
            # PII write requires explicit auth/encryption context.
            {
                "id": "pii-write-requires-auth",
                "when": "pii_write",
                "require": "auth_check",
                "severity": "block",
                "owner": "@security",
                "override_allowed": True,
            },
            # Soft rules — review required, not auto-blocked.
            {
                "id": "billing-side-effect-needs-idempotency",
                "when": "billing_side_effect",
                "require_any": ["idempotency_check", "feature_flag"],
                "severity": "review_required",
                "owner": "@payments",
                "override_allowed": True,
            },
            {
                "id": "external-call-needs-timeout",
                "when": "external_http_call",
                "require": "timeout_configured",
                "severity": "warn",
                "owner": "@platform",
                "override_allowed": True,
            },
        ],
    }


def parse_policy_pack(text: str) -> dict[str, Any]:
    pack: dict[str, Any] = {"name": "Policy Pack", "version": 1, "rules": []}
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line == "rules:":
            continue
        if line.startswith("- "):
            if current:
                pack["rules"].append(current)
            current = parse_kv(line[2:])
            continue
        kv = parse_kv(line)
        if current is not None:
            current.update(kv)
        else:
            pack.update(kv)
    if current:
        pack["rules"].append(current)
    for rule in pack["rules"]:
        if isinstance(rule.get("require_any"), str):
            rule["require_any"] = [part.strip() for part in rule["require_any"].split(",")]
    return pack


def evaluate_policy_pack(pack: dict[str, Any], concept_findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    concepts = {finding["concept"] for finding in concept_findings if finding.get("confidence", 0) >= 0.45}
    findings = []
    for rule in pack.get("rules", []):
        when = rule.get("when")
        if when not in concepts:
            continue
        has_requirement = bool(rule.get("require") or rule.get("require_any"))
        required_ok = True
        if rule.get("require"):
            required_ok = rule["require"] in concepts
        if rule.get("require_any"):
            required_ok = any(concept in concepts for concept in rule["require_any"])
        # Rules with NO requirement field treat the concept's mere presence as
        # the violation (e.g., secret_exposure, auth_bypass).
        if has_requirement and required_ok:
            continue
        # Emit one violation per matching concept finding so the path stays
        # specific — repeating the rule for each affected file.
        sources = [f for f in concept_findings if f["concept"] == when] or [{}]
        for source in sources:
            requirement = rule.get("require") or " or ".join(rule.get("require_any", []))
            default_message = (
                rule.get("message_override")
                or (f"{when} requires {requirement}." if requirement else f"{when} is not allowed.")
            )
            default_action = (
                rule.get("suggested_action_override")
                or (
                    f"Add {requirement} evidence or request owner override."
                    if requirement
                    else "Remove or justify this change before merge."
                )
            )
            findings.append(
                {
                    "rule_id": rule.get("id", f"{when}-policy"),
                    "concept": when,
                    "path": source.get("path"),
                    "symbol": source.get("symbol"),
                    "severity": rule.get("severity", "warn"),
                    "owner": rule.get("owner", "@owners"),
                    "override_allowed": rule.get("override_allowed", True),
                    "message": default_message,
                    "suggested_action": default_action,
                    "evidence": source.get("evidence", []),
                }
            )
    return findings


def parse_kv(line: str) -> dict[str, Any]:
    key, _, value = line.partition(":")
    value = value.strip().strip("'\"")
    if value.lower() == "true":
        parsed: Any = True
    elif value.lower() == "false":
        parsed = False
    elif value.startswith("[") and value.endswith("]"):
        parsed = [part.strip().strip("'\"") for part in value[1:-1].split(",") if part.strip()]
    else:
        parsed = value
    return {key.strip(): parsed}
