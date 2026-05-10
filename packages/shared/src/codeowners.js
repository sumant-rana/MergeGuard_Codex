import { normalizePath } from "./classifier.js";

export function parseCodeowners(text = "") {
  return String(text)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"))
    .map((line) => {
      const [pattern, ...owners] = line.split(/\s+/);
      return {
        pattern,
        owners,
        specificity: pattern.replaceAll("*", "").replaceAll("/", "").length
      };
    })
    .filter((entry) => entry.pattern && entry.owners.length);
}

export function assignOwners(files, codeownersText = "") {
  const entries = parseCodeowners(codeownersText);
  return files.map((file) => {
    const owner = ownersForPath(file.path, entries)[0] || file.owner || "unassigned";
    return { ...file, owner };
  });
}

export function ownersForPath(path, entries = []) {
  const clean = normalizePath(path);
  const matches = entries
    .filter((entry) => matchesPattern(clean, entry.pattern))
    .sort((a, b) => b.specificity - a.specificity);
  return matches[0]?.owners || [];
}

function matchesPattern(path, pattern) {
  const cleanPattern = normalizePath(pattern).replace(/^\//, "");
  if (cleanPattern === "*") return true;
  if (cleanPattern.endsWith("/")) return path.startsWith(cleanPattern);
  if (!cleanPattern.includes("*")) {
    return path === cleanPattern || path.startsWith(`${cleanPattern}/`);
  }

  const escaped = cleanPattern
    .split("*")
    .map((part) => part.replace(/[.+?^${}()|[\]\\]/g, "\\$&"))
    .join(".*");
  return new RegExp(`^${escaped}$`).test(path);
}
