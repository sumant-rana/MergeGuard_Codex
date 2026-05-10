export function buildBehavioralDeltas(files, conceptFindings = []) {
  return files
    .filter((file) => file.classification !== "docs" && file.classification !== "generated")
    .flatMap((file) => {
      const symbols = file.symbols?.length ? file.symbols : [{ name: null, kind: "file" }];
      return symbols.slice(0, 4).map((symbol) => {
        const concepts = conceptFindings.filter((finding) => finding.path === file.path).map((finding) => finding.concept);
        const conceptText = concepts.length ? ` Concepts touched: ${concepts.join(", ")}.` : "";
        return {
          path: file.path,
          symbol: symbol.name,
          old_behavior: inferOldBehavior(file),
          new_behavior: inferNewBehavior(file, concepts),
          divergent_input: divergentExample(file, concepts),
          severity: file.risk_score >= 65 ? "review_required" : file.risk_score >= 35 ? "warn" : "info",
          confidence: symbol.confidence || 0.55,
          category: concepts[0] || file.classification,
          summary: `${file.classification} change in ${symbol.name || file.path}.${conceptText}`
        };
      });
    })
    .sort((a, b) => severityRank(b.severity) - severityRank(a.severity));
}

export function buildBlastRadius(files, codeowners = "") {
  return files
    .filter((file) => file.must_inspect || file.risk_score >= 30)
    .map((file) => ({
      path: file.path,
      symbol: file.symbols?.[0]?.name || null,
      direct_callers: inferCallers(file.path),
      downstream_services: inferServices(file.path),
      owners: file.owner ? [file.owner] : ["unassigned"],
      impacted_tests: inferImpactedTests(file.path),
      confidence: file.symbols?.[0]?.confidence || 0.48
    }));
}

export function aggregateHotspotThemes(files, conceptFindings = []) {
  const counts = new Map();
  for (const file of files) {
    add(counts, file.classification);
    for (const reason of file.risk_reasons || []) {
      if (reason.includes("auth")) add(counts, "auth");
      if (reason.includes("payment") || reason.includes("billing")) add(counts, "billing");
      if (reason.includes("prompt")) add(counts, "prompt");
      if (reason.includes("sql")) add(counts, "database");
    }
  }
  for (const finding of conceptFindings) add(counts, finding.concept);
  return [...counts.entries()]
    .map(([theme, count]) => ({ theme, count }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 8);
}

function inferOldBehavior(file) {
  if (file.deletions > file.additions) return "Previous behavior may have included branches or guards that were removed.";
  return "Previous behavior followed the base branch implementation for this file or symbol.";
}

function inferNewBehavior(file, concepts) {
  if (concepts.includes("billing-side-effect")) return "New behavior can affect monetary side effects such as charges, refunds, invoices, or payouts.";
  if (concepts.includes("auth-check")) return "New behavior changes authorization/session handling.";
  if (concepts.includes("prompt-change")) return "New behavior changes model or prompt responses.";
  if (concepts.includes("raw-sql")) return "New behavior changes direct database access.";
  return `New behavior changes ${file.classification} code with ${file.additions} additions and ${file.deletions} deletions.`;
}

function divergentExample(file, concepts) {
  if (concepts.includes("billing-side-effect")) return "Duplicate refund request with the same idempotency key.";
  if (concepts.includes("auth-check")) return "User without required role attempts the changed operation.";
  if (concepts.includes("prompt-change")) return "Golden prompt that previously required strict JSON output.";
  if (concepts.includes("raw-sql")) return "Input containing boundary characters or a missing tenant id.";
  return file.risk_score >= 45 ? "Boundary input around the changed branch or error path." : null;
}

function inferCallers(path) {
  const clean = path.replace(/\.[^.]+$/, "");
  const basename = clean.split("/").pop();
  return [`${basename} callers`, `${clean}/index exports`].slice(0, clean.includes("/") ? 2 : 1);
}

function inferServices(path) {
  const lower = path.toLowerCase();
  if (lower.includes("payment") || lower.includes("billing")) return ["payments", "finance-reporting"];
  if (lower.includes("auth")) return ["identity", "api-gateway"];
  if (lower.includes("prompt") || lower.includes("agent")) return ["ai-workflows"];
  if (lower.includes("cache")) return ["cache-layer"];
  return ["application"];
}

function inferImpactedTests(path) {
  const base = path.split("/").pop()?.replace(/\.[^.]+$/, "") || "changed_behavior";
  return [`${base}.test`, `${base}.spec`, `${base}_test.py`];
}

function add(counts, key) {
  if (!key) return;
  counts.set(key, (counts.get(key) || 0) + 1);
}

function severityRank(severity) {
  return { block: 4, review_required: 3, warn: 2, info: 1 }[severity] || 0;
}
