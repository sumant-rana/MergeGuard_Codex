import { detectLanguage, normalizePath } from "./classifier.js";

export function extractChangedSymbols(rawFile = {}) {
  const path = normalizePath(rawFile.path || rawFile.filename || rawFile.name);
  const language = detectLanguage(path);
  const source = [rawFile.patch, rawFile.current_content, rawFile.content]
    .filter(Boolean)
    .join("\n");

  if (!source.trim()) {
    return fallbackSymbols(path, language);
  }

  if (language === "python") return extractPythonSymbols(source, path);
  if (["typescript", "javascript"].includes(language)) return extractJavaScriptSymbols(source, path);
  return fallbackSymbols(path, language);
}

export function summarizePatch(rawFile = {}) {
  const patch = String(rawFile.patch || "");
  if (!patch) {
    return {
      added_lines: Number(rawFile.additions || 0),
      removed_lines: Number(rawFile.deletions || 0),
      touched_lines: Number(rawFile.changes || rawFile.additions || 0) + Number(rawFile.deletions || 0),
      added_keywords: [],
      removed_keywords: []
    };
  }

  const addedLines = patch
    .split(/\r?\n/)
    .filter((line) => line.startsWith("+") && !line.startsWith("+++"));
  const removedLines = patch
    .split(/\r?\n/)
    .filter((line) => line.startsWith("-") && !line.startsWith("---"));

  return {
    added_lines: addedLines.length,
    removed_lines: removedLines.length,
    touched_lines: addedLines.length + removedLines.length,
    added_keywords: keywordHits(addedLines.join("\n")),
    removed_keywords: keywordHits(removedLines.join("\n"))
  };
}

function extractPythonSymbols(source, path) {
  return extractByPatterns(source, path, [
    { kind: "class", pattern: /^[+\s]*class\s+([A-Za-z_][\w]*)/ },
    { kind: "function", pattern: /^[+\s]*(?:async\s+)?def\s+([A-Za-z_][\w]*)/ }
  ]);
}

function extractJavaScriptSymbols(source, path) {
  return extractByPatterns(source, path, [
    { kind: "class", pattern: /^[+\s]*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)/ },
    { kind: "function", pattern: /^[+\s]*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)/ },
    { kind: "function", pattern: /^[+\s]*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(?/ },
    { kind: "function", pattern: /^[+\s]*(?:public|private|protected)?\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*{/ }
  ]);
}

function extractByPatterns(source, path, patterns) {
  const symbols = [];
  const seen = new Set();
  source.split(/\r?\n/).forEach((line, index) => {
    for (const { kind, pattern } of patterns) {
      const match = line.match(pattern);
      if (!match) continue;
      const name = match[1];
      const key = `${kind}:${name}`;
      if (seen.has(key)) continue;
      seen.add(key);
      symbols.push({
        name,
        kind,
        path,
        line: index + 1,
        changed: line.trimStart().startsWith("+"),
        confidence: line.trimStart().startsWith("+") ? 0.78 : 0.62
      });
    }
  });
  return symbols.length ? symbols : fallbackSymbols(path);
}

function fallbackSymbols(path, language = detectLanguage(path)) {
  const basename = path.split("/").pop() || path;
  return [
    {
      name: basename.replace(/\.[^.]+$/, ""),
      kind: language === "markdown" ? "document" : "file",
      path,
      line: null,
      changed: true,
      confidence: 0.38
    }
  ];
}

function keywordHits(text) {
  const lower = text.toLowerCase();
  return [
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
    "agent",
    "schema",
    "contract",
    "cache",
    "feature"
  ].filter((keyword) => lower.includes(keyword));
}
