const SOURCE_EXTENSIONS = new Set([
  ".py",
  ".ts",
  ".tsx",
  ".js",
  ".jsx",
  ".mjs",
  ".cjs",
  ".java",
  ".go",
  ".rb",
  ".php",
  ".cs",
  ".rs",
  ".kt",
  ".swift"
]);

const DOC_EXTENSIONS = new Set([".md", ".mdx", ".rst", ".txt", ".adoc"]);
const CONFIG_EXTENSIONS = new Set([".json", ".yaml", ".yml", ".toml", ".ini", ".env", ".conf"]);
const GENERATED_EXTENSIONS = new Set([".lock", ".map"]);

export const RISK_KEYWORDS = [
  "auth",
  "token",
  "payment",
  "billing",
  "refund",
  "pii",
  "sql",
  "migration",
  "retry",
  "timeout",
  "prompt",
  "secret",
  "permission",
  "oauth",
  "webhook",
  "agent"
];

export const RISK_PATH_PREFIXES = [
  "payments/",
  "billing/",
  "auth/",
  "security/",
  "migrations/",
  "prompts/",
  "agents/",
  "infra/",
  "database/"
];

export function normalizePath(path) {
  return String(path || "").replaceAll("\\", "/").replace(/^\.\/+/, "");
}

export function extensionFor(path) {
  const clean = normalizePath(path);
  const name = clean.split("/").pop() || "";
  const index = name.lastIndexOf(".");
  return index >= 0 ? name.slice(index).toLowerCase() : "";
}

export function detectLanguage(path) {
  const ext = extensionFor(path);
  const languageByExtension = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".md": "markdown",
    ".mdx": "markdown",
    ".sql": "sql",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".rs": "rust"
  };
  return languageByExtension[ext] || "unknown";
}

export function isGeneratedPath(path) {
  const clean = normalizePath(path).toLowerCase();
  const ext = extensionFor(clean);
  return (
    GENERATED_EXTENSIONS.has(ext) ||
    clean.endsWith(".min.js") ||
    clean.endsWith(".min.css") ||
    clean.includes("/generated/") ||
    clean.includes("/dist/") ||
    clean.includes("/build/") ||
    clean.includes("/coverage/") ||
    clean.includes("/vendor/") ||
    clean.includes("/__generated__/") ||
    clean.includes(".generated.") ||
    clean === "package-lock.json" ||
    clean === "yarn.lock" ||
    clean === "pnpm-lock.yaml"
  );
}

export function isTestPath(path) {
  const clean = normalizePath(path).toLowerCase();
  return (
    clean.includes("/test/") ||
    clean.includes("/tests/") ||
    clean.includes("/__tests__/") ||
    clean.includes(".test.") ||
    clean.includes(".spec.") ||
    clean.endsWith("_test.py")
  );
}

export function isDocsPath(path) {
  const clean = normalizePath(path).toLowerCase();
  return clean.startsWith("docs/") || clean.includes("/docs/") || DOC_EXTENSIONS.has(extensionFor(clean));
}

export function isPromptPath(path) {
  const clean = normalizePath(path).toLowerCase();
  return (
    clean.startsWith("prompts/") ||
    clean.includes("/prompts/") ||
    clean.includes("/prompt/") ||
    clean.endsWith(".prompt") ||
    clean.endsWith(".prompt.md") ||
    clean.endsWith(".jinja") ||
    clean.endsWith(".tmpl")
  );
}

export function riskSignalsForPath(path) {
  const clean = normalizePath(path).toLowerCase();
  const keywordMatches = RISK_KEYWORDS.filter((keyword) => clean.includes(keyword));
  const pathMatches = RISK_PATH_PREFIXES.filter((prefix) => clean.startsWith(prefix) || clean.includes(`/${prefix}`));
  return { keywordMatches, pathMatches };
}

export function classifyChangedFile(file) {
  const path = normalizePath(file.path || file.filename || file.name);
  const ext = extensionFor(path);
  const generated = isGeneratedPath(path);
  const test = isTestPath(path);
  const docs = isDocsPath(path);
  const prompt = isPromptPath(path);
  const config = CONFIG_EXTENSIONS.has(ext) || path.startsWith(".github/");
  const source = SOURCE_EXTENSIONS.has(ext);
  const { keywordMatches, pathMatches } = riskSignalsForPath(path);
  const hasRiskSignal = keywordMatches.length > 0 || pathMatches.length > 0;

  let classification = "other";
  if (generated) classification = "generated";
  else if (test) classification = "test";
  else if (prompt) classification = "prompt";
  else if (docs) classification = "docs";
  else if (source && hasRiskSignal) classification = "security-sensitive";
  else if (source) classification = "logic";
  else if (config) classification = "wiring";

  const additions = Number(file.additions || 0);
  const deletions = Number(file.deletions || 0);
  const changes = Number(file.changes || additions + deletions || 0);
  const riskScore = scoreFileRisk({
    classification,
    changes,
    additions,
    deletions,
    keywordMatches,
    pathMatches,
    status: file.status
  });

  const reasons = buildReasons({
    classification,
    generated,
    keywordMatches,
    pathMatches,
    changes,
    status: file.status
  });

  return {
    path,
    status: file.status || "modified",
    additions,
    deletions,
    changes,
    language: detectLanguage(path),
    generated,
    classification,
    risk_score: riskScore,
    risk_reasons: reasons,
    safe_to_skim: riskScore < 25 && ["generated", "docs", "test", "wiring", "other"].includes(classification),
    must_inspect: riskScore >= 45 || ["security-sensitive", "prompt"].includes(classification)
  };
}

function scoreFileRisk({ classification, changes, additions, deletions, keywordMatches, pathMatches, status }) {
  const classificationWeight = {
    generated: 0,
    docs: 2,
    test: 4,
    other: 6,
    wiring: 10,
    logic: 18,
    prompt: 30,
    "security-sensitive": 34
  }[classification] ?? 8;

  const diffWeight = Math.min(24, Math.ceil(changes / 18));
  const keywordWeight = Math.min(24, keywordMatches.length * 8);
  const pathWeight = Math.min(24, pathMatches.length * 14);
  const deletionWeight = deletions > additions * 2 && deletions > 20 ? 8 : 0;
  const creationWeight = status === "added" && classification !== "test" ? 4 : 0;

  return Math.max(0, Math.min(100, classificationWeight + diffWeight + keywordWeight + pathWeight + deletionWeight + creationWeight));
}

function buildReasons({ classification, generated, keywordMatches, pathMatches, changes, status }) {
  const reasons = [];
  if (generated) reasons.push("generated or vendored artifact");
  if (classification === "security-sensitive") reasons.push("source file matched sensitive path or keyword");
  if (classification === "prompt") reasons.push("prompt or agent workflow artifact changed");
  if (classification === "logic") reasons.push("application logic changed");
  if (classification === "wiring") reasons.push("configuration or integration wiring changed");
  if (keywordMatches.length) reasons.push(`risk keywords: ${keywordMatches.join(", ")}`);
  if (pathMatches.length) reasons.push(`risk paths: ${pathMatches.join(", ")}`);
  if (changes > 120) reasons.push("large file-level diff");
  if (status === "removed") reasons.push("file removed");
  return reasons.length ? reasons : ["low-risk file shape"];
}
