import { normalizePath } from "./classifier.js";

const REQUIREMENT_WORDS = ["should", "must", "need", "needs", "require", "requires", "implement", "support", "prevent", "ensure"];
const NEGATIVE_WORDS = ["must not", "should not", "do not", "without", "avoid", "prevent"];
const OUT_OF_SCOPE_MARKERS = ["out of scope", "non-goal", "not included", "does not"];

export function extractIntentItems(pr = {}) {
  const text = [pr.title, pr.body, pr.description].filter(Boolean).join("\n");
  const lines = text
    .split(/\r?\n|[.;]/)
    .map((line) => line.replace(/^[-*]\s*/, "").trim())
    .filter(Boolean);

  const items = [];
  for (const line of lines) {
    const lower = line.toLowerCase();
    const hasRequirement = REQUIREMENT_WORDS.some((word) => lower.includes(word));
    const hasNegative = NEGATIVE_WORDS.some((word) => lower.includes(word));
    const outOfScope = OUT_OF_SCOPE_MARKERS.some((word) => lower.includes(word));

    if (!hasRequirement && !hasNegative && !outOfScope && line !== pr.title) continue;

    items.push({
      text: line,
      category: outOfScope ? "out_of_scope" : hasNegative ? "must_not" : "should",
      source: line === pr.title ? "pr_title" : "pr_body",
      confidence: hasRequirement || hasNegative || outOfScope ? 0.76 : 0.52,
      severity: hasNegative ? "review_required" : "warn",
      out_of_scope: outOfScope
    });
  }

  if (!items.length && pr.title) {
    items.push({
      text: `Review that implementation matches PR title: ${pr.title}`,
      category: "should",
      source: "pr_title",
      confidence: 0.42,
      severity: "warn",
      out_of_scope: false
    });
  }

  return items.slice(0, 12);
}

export function mapIntentEvidence(intentItems, files) {
  return intentItems.map((item) => {
    const terms = importantTerms(item.text);
    const matchedFiles = files.filter((file) => {
      const path = normalizePath(file.path).toLowerCase();
      const reasonText = String(file.risk_reasons || "").toLowerCase();
      return terms.some((term) => path.includes(term) || reasonText.includes(term));
    });
    const hasTests = matchedFiles.some((file) => file.classification === "test") || files.some((file) => file.classification === "test");
    const status = matchedFiles.length && hasTests ? "proven" : matchedFiles.length ? "partial" : "missing";

    return {
      ...item,
      mapped_paths: matchedFiles.map((file) => file.path).slice(0, 8),
      evidence_status: status,
      suggested_test:
        status === "missing"
          ? `Add an implementation or explicit reviewer acceptance for intent: ${item.text}`
          : hasTests
            ? "Changed tests are present; verify they exercise this intent."
            : `Add tests covering ${matchedFiles[0]?.path || "the mapped implementation"}.`
    };
  });
}

export function findUnexpectedScope(files, intentItems) {
  const terms = new Set(intentItems.flatMap((item) => importantTerms(item.text)));
  return files
    .filter((file) => file.must_inspect || file.risk_score >= 50)
    .filter((file) => {
      const haystack = `${file.path} ${file.risk_reasons?.join(" ") || ""}`.toLowerCase();
      return ![...terms].some((term) => haystack.includes(term));
    })
    .map((file) => ({
      type: "unexpected-scope",
      path: file.path,
      severity: file.risk_score >= 65 ? "review_required" : "warn",
      confidence: 0.64,
      message: `High-risk change in ${file.path} is not clearly tied to stated PR intent.`,
      suggested_action: "Confirm this change is intended or split it into a separate PR."
    }));
}

function importantTerms(text) {
  return String(text)
    .toLowerCase()
    .replace(/[^a-z0-9_\s/-]/g, " ")
    .split(/\s+/)
    .filter((word) => word.length >= 4)
    .filter((word) => !["that", "this", "with", "from", "into", "review", "implementation", "matches", "title"].includes(word))
    .slice(0, 12);
}
