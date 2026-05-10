import { normalizePath } from "./classifier.js";

export function compareRuntimeContracts(files, rawFiles = [], contractSummaries = []) {
  const filePaths = new Set(files.map((file) => file.path));
  const supplied = contractSummaries
    .filter((contract) => filePaths.has(normalizePath(contract.path)))
    .flatMap((contract) => compareSuppliedContract(contract));

  if (supplied.length) return supplied;

  return files
    .filter((file) => ["logic", "security-sensitive"].includes(file.classification))
    .filter((file) => /api|contract|schema|serializer|validator|model|dto/.test(file.path.toLowerCase()))
    .map((file) => ({
      path: file.path,
      symbol: file.symbols?.[0]?.name || null,
      old_contract: "previous shape unknown",
      new_contract: "changed API/data-shape surface inferred from path and diff",
      violated_assumption: "Downstream callers may rely on the previous response or input shape.",
      generated_test_status: "suggested",
      severity: file.risk_score >= 60 ? "review_required" : "warn",
      confidence: 0.58,
      suggested_test: {
        path: suggestedContractTestPath(file.path),
        framework: file.language === "python" ? "pytest" : "vitest",
        intent: `Lock the public input/output shape for ${file.path}.`
      }
    }));
}

function compareSuppliedContract(contract) {
  const oldShape = contract.old || contract.before || {};
  const newShape = contract.new || contract.after || {};
  const removed = Object.keys(oldShape).filter((key) => !(key in newShape));
  const changedTypes = Object.keys(oldShape).filter((key) => key in newShape && oldShape[key] !== newShape[key]);
  if (!removed.length && !changedTypes.length) return [];

  return [
    {
      path: normalizePath(contract.path),
      symbol: contract.symbol || null,
      old_contract: JSON.stringify(oldShape),
      new_contract: JSON.stringify(newShape),
      violated_assumption: [
        removed.length ? `Removed fields: ${removed.join(", ")}` : null,
        changedTypes.length ? `Changed field types: ${changedTypes.join(", ")}` : null
      ]
        .filter(Boolean)
        .join("; "),
      generated_test_status: "suggested",
      severity: removed.length ? "review_required" : "warn",
      confidence: 0.82,
      suggested_test: {
        path: suggestedContractTestPath(contract.path),
        framework: contract.framework || "repo-default",
        intent: "Assert backward-compatible runtime contract shape."
      }
    }
  ];
}

function suggestedContractTestPath(path) {
  const clean = normalizePath(path);
  if (clean.endsWith(".py")) return clean.replace(/\.py$/, "_contract_test.py");
  if (/\.(ts|tsx|js|jsx)$/.test(clean)) return clean.replace(/\.(ts|tsx|js|jsx)$/, ".contract.test.$1");
  return `${clean}.contract.test`;
}
