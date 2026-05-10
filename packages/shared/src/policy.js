import { newId } from "./ids.js";

export const DEFAULT_POLICY_YAML = `name: MergeGuard Default Policy
version: 1
rules:
  - id: pii-write-requires-auth
    when: pii-write
    require: auth-check
    severity: block
    owner: "@security"
  - id: billing-side-effect-needs-idempotency
    when: billing-side-effect
    require_any: idempotency-key-check, feature-flag-read
    severity: review_required
    owner: "@payments"
  - id: external-call-needs-timeout
    when: external-http-call
    require: timeout-configured
    severity: warn
    owner: "@platform"
  - id: agent-tool-call-needs-prompt-gate
    when: agent-tool-call
    require: prompt-change
    severity: review_required
    owner: "@ai-platform"
`;

export function defaultPolicyPack(repoId = null) {
  return normalizePolicyPack({
    id: "policy_default",
    repo_id: repoId,
    name: "MergeGuard Default Policy",
    yaml: DEFAULT_POLICY_YAML,
    version: 1,
    active: true,
    created_by: "system"
  });
}

export function normalizePolicyPack(pack) {
  const parsed = parsePolicyYaml(pack.yaml || "");
  return {
    id: pack.id || newId("policy"),
    repo_id: pack.repo_id || null,
    name: pack.name || parsed.name || "Policy Pack",
    yaml: pack.yaml || "",
    version: pack.version || parsed.version || 1,
    active: pack.active !== false,
    created_by: pack.created_by || "local",
    rules: parsed.rules
  };
}

export function parsePolicyYaml(yaml = "") {
  const lines = String(yaml).split(/\r?\n/);
  const doc = { name: "Policy Pack", version: 1, rules: [] };
  let current = null;

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || line === "rules:") continue;

    if (line.startsWith("- ")) {
      if (current) doc.rules.push(normalizeRule(current));
      current = parseKeyValue(line.slice(2));
      continue;
    }

    const entry = parseKeyValue(line);
    if (!entry) continue;

    if (current) Object.assign(current, entry);
    else Object.assign(doc, entry);
  }

  if (current) doc.rules.push(normalizeRule(current));
  return doc;
}

export function evaluatePolicies({ conceptFindings, policyPacks, files }) {
  const packs = policyPacks?.length ? policyPacks.map(normalizePolicyPack) : [defaultPolicyPack()];
  const concepts = new Set(conceptFindings.filter((finding) => finding.confidence >= 0.45).map((finding) => finding.concept));
  const findings = [];

  for (const pack of packs.filter((pack) => pack.active)) {
    for (const rule of pack.rules) {
      if (!concepts.has(rule.when)) continue;
      if (rule.changed_paths?.length && !files.some((file) => pathMatchesAny(file.path, rule.changed_paths))) continue;

      const required = rule.require ? concepts.has(rule.require) : true;
      const requiredAny = rule.require_any?.length ? rule.require_any.some((concept) => concepts.has(concept)) : true;
      if (required && requiredAny) continue;

      const source = conceptFindings.find((finding) => finding.concept === rule.when);
      findings.push({
        id: newId("policyfinding"),
        rule_id: rule.id,
        policy_pack_id: pack.id,
        policy_pack_name: pack.name,
        concept: rule.when,
        path: source?.path || files[0]?.path || null,
        symbol: source?.symbol || null,
        confidence: source?.confidence || 0.6,
        relation: rule.require ? `requires ${rule.require}` : `requires any of ${rule.require_any.join(", ")}`,
        policy_result: rule.severity === "block" ? "block" : "warn",
        severity: rule.severity,
        owner: rule.owner || "@owners",
        message: `${rule.when} requires ${rule.require || rule.require_any.join(" or ")} by ${rule.id}.`,
        suggested_action: `Add ${rule.require || rule.require_any.join(" or ")} evidence or request override from ${rule.owner || "@owners"}.`
      });
    }
  }

  return findings;
}

function parseKeyValue(line) {
  const index = line.indexOf(":");
  if (index === -1) return null;
  const key = line.slice(0, index).trim();
  const raw = line.slice(index + 1).trim();
  return { [key]: parseValue(raw) };
}

function parseValue(value) {
  if (value === "true") return true;
  if (value === "false") return false;
  if (/^\d+$/.test(value)) return Number(value);
  if (value.startsWith("[") && value.endsWith("]")) {
    return value
      .slice(1, -1)
      .split(",")
      .map((part) => stripQuotes(part.trim()))
      .filter(Boolean);
  }
  if (value.includes(",") && !value.includes("://")) {
    return value.split(",").map((part) => stripQuotes(part.trim())).filter(Boolean);
  }
  return stripQuotes(value);
}

function stripQuotes(value) {
  return String(value).replace(/^['"]|['"]$/g, "");
}

function normalizeRule(rule) {
  return {
    id: rule.id || `policy-${rule.when}`,
    when: rule.when,
    require: Array.isArray(rule.require) ? rule.require[0] : rule.require || null,
    require_any: Array.isArray(rule.require_any) ? rule.require_any : rule.require_any ? [rule.require_any] : [],
    severity: rule.severity || "warn",
    changed_paths: Array.isArray(rule.changed_paths) ? rule.changed_paths : rule.changed_paths ? [rule.changed_paths] : [],
    confidence: Number(rule.confidence || 0.45),
    owner: rule.owner || "@owners"
  };
}

function pathMatchesAny(path, patterns) {
  return patterns.some((pattern) => {
    if (pattern === "**/*") return true;
    if (pattern.endsWith("/**")) return path.startsWith(pattern.slice(0, -3));
    if (pattern.startsWith("**/*.")) return path.endsWith(pattern.slice(4));
    return path === pattern || path.startsWith(`${pattern}/`);
  });
}
