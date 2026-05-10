import { normalizePath } from "./classifier.js";

export const CONCEPT_TAXONOMY = [
  "auth-check",
  "pii-read",
  "pii-write",
  "billing-side-effect",
  "idempotency-key-check",
  "external-http-call",
  "timeout-configured",
  "retry-with-backoff",
  "raw-sql",
  "cache-invalidate",
  "feature-flag-read",
  "prompt-change",
  "agent-tool-call"
];

const CONCEPT_PATTERNS = [
  { concept: "auth-check", terms: ["auth", "authorize", "permission", "role", "scope", "session"] },
  { concept: "pii-read", terms: ["pii", "email", "phone", "ssn", "date_of_birth", "customer_address"] },
  { concept: "pii-write", terms: ["write_pii", "update_profile", "save_email", "persist_email", "customer_address", "ssn"] },
  { concept: "billing-side-effect", terms: ["payment", "billing", "refund", "charge", "invoice", "payout"] },
  { concept: "idempotency-key-check", terms: ["idempotency", "dedupe", "idempotent"] },
  { concept: "external-http-call", terms: ["fetch(", "axios", "http.", "requests.", "urllib", "webhook"] },
  { concept: "timeout-configured", terms: ["timeout", "deadline", "abortcontroller"] },
  { concept: "retry-with-backoff", terms: ["retry", "backoff", "exponential"] },
  { concept: "raw-sql", terms: ["select ", "insert ", "update ", "delete ", "raw sql", "execute("] },
  { concept: "cache-invalidate", terms: ["cache", "invalidate", "redis", "ttl"] },
  { concept: "feature-flag-read", terms: ["feature_flag", "featureflag", "launchdarkly", "flag"] },
  { concept: "prompt-change", terms: ["prompt", "system message", "model", "temperature"] },
  { concept: "agent-tool-call", terms: ["tool_call", "function_call", "agent", "planner", "executor"] }
];

export function classifyConcepts(files, rawFiles = []) {
  const rawByPath = new Map(rawFiles.map((file) => [normalizePath(file.path || file.filename || file.name), file]));
  return files.flatMap((file) => {
    const raw = rawByPath.get(file.path) || {};
    const haystack = [
      file.path,
      file.classification,
      file.risk_reasons?.join(" "),
      raw.patch,
      raw.current_content,
      raw.content
    ]
      .filter(Boolean)
      .join("\n")
      .toLowerCase();

    return CONCEPT_PATTERNS.filter(({ terms }) => terms.some((term) => haystack.includes(term))).map(({ concept, terms }) => ({
      concept,
      path: file.path,
      symbol: file.symbols?.[0]?.name || null,
      confidence: confidenceForConcept({ concept, file, haystack, terms }),
      relation: "introduced-or-modified",
      policy_result: "pending",
      severity: "info",
      evidence: terms.filter((term) => haystack.includes(term)).slice(0, 4)
    }));
  });
}

function confidenceForConcept({ concept, file, haystack, terms }) {
  let score = 0.48;
  if (file.path.toLowerCase().includes(concept.split("-")[0])) score += 0.16;
  if (terms.filter((term) => haystack.includes(term)).length > 1) score += 0.14;
  if (file.risk_score >= 45) score += 0.08;
  if (["prompt-change", "billing-side-effect", "auth-check"].includes(concept)) score += 0.08;
  return Math.min(0.95, Number(score.toFixed(2)));
}
