import { classifyChangedFile } from "../../../packages/shared/src/classifier.js";
import { clampRisk, summarizeAnalysis } from "../../../packages/shared/src/risk.js";
import { assignOwners } from "../../../packages/shared/src/codeowners.js";
import { classifyConcepts } from "../../../packages/shared/src/concepts.js";
import { compareRuntimeContracts } from "../../../packages/shared/src/contracts.js";
import { extractIntentItems, findUnexpectedScope, mapIntentEvidence } from "../../../packages/shared/src/intent.js";
import { evaluatePolicies } from "../../../packages/shared/src/policy.js";
import { promptFindingsFromCanaries, runPromptCanaries } from "../../../packages/shared/src/promptCanary.js";
import { aggregateHotspotThemes, buildBehavioralDeltas, buildBlastRadius } from "../../../packages/shared/src/semantic.js";
import { extractChangedSymbols, summarizePatch } from "../../../packages/shared/src/symbols.js";

export function analyzePullRequest(input, options = {}) {
  const rawFiles = input.changedFiles || [];
  const classifiedFiles = rawFiles.map((file) => ({
    ...classifyChangedFile(file),
    symbols: extractChangedSymbols(file),
    patch_summary: summarizePatch(file)
  }));
  const changedFiles = assignOwners(classifiedFiles, input.codeowners || input.mergeguard?.codeowners || "");
  const conceptFindings = classifyConcepts(changedFiles, rawFiles);
  const policyFindings = evaluatePolicies({
    conceptFindings,
    policyPacks: options.policyPacks || input.policyPacks || [],
    files: changedFiles
  });
  const promptCanaryRuns = runPromptCanaries(changedFiles, rawFiles, input.promptSuites || input.mergeguard?.prompt_suites || []);
  const promptFindings = promptFindingsFromCanaries(promptCanaryRuns);
  const contractFindings = compareRuntimeContracts(
    changedFiles,
    rawFiles,
    input.contractSummaries || input.mergeguard?.contracts || []
  );
  const intentItems = mapIntentEvidence(extractIntentItems(input.pr || {}), changedFiles);
  const unexpectedScopeFindings = findUnexpectedScope(changedFiles, intentItems);
  const behavioralDeltas = buildBehavioralDeltas(changedFiles, conceptFindings);
  const blastRadius = buildBlastRadius(changedFiles, input.codeowners || "");
  const baseSummary = summarizeAnalysis(changedFiles, {
    mode: options.checkMode || "advisory",
    hotspotLimit: options.hotspotLimit || 12
  });
  const summary = buildAdvancedSummary({
    baseSummary,
    changedFiles,
    conceptFindings,
    policyFindings,
    promptFindings,
    promptCanaryRuns,
    contractFindings,
    intentItems,
    unexpectedScopeFindings,
    behavioralDeltas,
    blastRadius,
    checkMode: options.checkMode || "advisory"
  });

  return {
    changedFiles,
    intentItems,
    behavioralDeltas,
    conceptFindings,
    policyFindings,
    promptCanaryRuns,
    contractFindings,
    blastRadius,
    summary,
    completedAt: new Date().toISOString()
  };
}

function buildAdvancedSummary({
  baseSummary,
  changedFiles,
  conceptFindings,
  policyFindings,
  promptFindings,
  promptCanaryRuns,
  contractFindings,
  intentItems,
  unexpectedScopeFindings,
  behavioralDeltas,
  blastRadius,
  checkMode
}) {
  const blockingFindings = [
    ...policyFindings.filter((finding) => finding.severity === "block"),
    ...promptFindings.filter((finding) => finding.severity === "block"),
    ...contractFindings.filter((finding) => finding.severity === "block")
  ];
  const reviewFindings = [
    ...baseSummary.evidence_findings.filter((finding) => finding.severity === "review_required"),
    ...policyFindings.filter((finding) => ["review_required", "block"].includes(finding.severity)),
    ...promptFindings,
    ...contractFindings.filter((finding) => ["review_required", "block"].includes(finding.severity)),
    ...unexpectedScopeFindings.filter((finding) => finding.severity === "review_required"),
    ...intentItems.filter((item) => item.evidence_status === "missing" && item.confidence >= 0.7)
  ];

  const riskScore = clampRisk(
    baseSummary.risk_score +
      policyFindings.length * 12 +
      promptFindings.length * 18 +
      contractFindings.filter((finding) => finding.severity !== "info").length * 10 +
      unexpectedScopeFindings.length * 7 +
      behavioralDeltas.filter((delta) => delta.severity === "review_required").length * 5
  );

  const status =
    blockingFindings.length && checkMode === "blocking"
      ? "blocked"
      : reviewFindings.length || riskScore >= 50
        ? "review"
        : baseSummary.status;
  const topFinding = blockingFindings[0] || reviewFindings[0] || null;
  const topBlocker = topFinding?.message || topFinding?.text || baseSummary.top_blocker;
  const nextAction = nextActionFor({
    status,
    topFinding,
    baseSummary,
    promptFindings,
    policyFindings,
    contractFindings
  });

  return {
    ...baseSummary,
    risk_score: riskScore,
    status,
    top_blocker: topBlocker || null,
    next_action: nextAction,
    intent_items: intentItems,
    unexpected_scope_findings: unexpectedScopeFindings,
    behavioral_deltas: behavioralDeltas,
    blast_radius: blastRadius,
    concept_findings: conceptFindings,
    policy_findings: policyFindings,
    prompt_findings: promptFindings,
    prompt_canary_runs: promptCanaryRuns,
    contract_findings: contractFindings,
    hotspot_themes: aggregateHotspotThemes(changedFiles, conceptFindings),
    owner_summary: summarizeOwners(changedFiles),
    review_bottlenecks: reviewBottlenecks({ policyFindings, promptFindings, contractFindings, baseSummary }),
    suggested_tests: suggestedTests({ baseSummary, contractFindings, intentItems }),
    learning_recommendations: learningRecommendations({ policyFindings, promptFindings, contractFindings, baseSummary })
  };
}

function nextActionFor({ status, topFinding, baseSummary, promptFindings, policyFindings, contractFindings }) {
  if (status === "blocked" && policyFindings.length) return policyFindings[0].suggested_action;
  if (promptFindings.length) return promptFindings[0].suggested_action;
  if (contractFindings.some((finding) => finding.severity === "review_required")) {
    return contractFindings.find((finding) => finding.severity === "review_required").suggested_test?.intent;
  }
  if (topFinding?.suggested_action) return topFinding.suggested_action;
  return baseSummary.next_action;
}

function summarizeOwners(files) {
  const counts = new Map();
  for (const file of files) counts.set(file.owner || "unassigned", (counts.get(file.owner || "unassigned") || 0) + 1);
  return [...counts.entries()]
    .map(([owner, file_count]) => ({ owner, file_count }))
    .sort((a, b) => b.file_count - a.file_count);
}

function reviewBottlenecks({ policyFindings, promptFindings, contractFindings, baseSummary }) {
  const items = [];
  if (baseSummary.evidence_findings.length) items.push({ category: "missing-evidence", count: baseSummary.evidence_findings.length });
  if (policyFindings.length) items.push({ category: "policy", count: policyFindings.length });
  if (promptFindings.length) items.push({ category: "prompt-canary", count: promptFindings.length });
  if (contractFindings.length) items.push({ category: "runtime-contract", count: contractFindings.length });
  return items.sort((a, b) => b.count - a.count);
}

function suggestedTests({ baseSummary, contractFindings, intentItems }) {
  const evidenceTests = baseSummary.evidence_findings.map((finding) => ({
    path: finding.path,
    framework: inferFramework(finding.path),
    intent: finding.suggested_action
  }));
  const contractTests = contractFindings.map((finding) => finding.suggested_test).filter(Boolean);
  const intentTests = intentItems
    .filter((item) => item.evidence_status !== "proven")
    .map((item) => ({
      path: "review-intent",
      framework: "repo-default",
      intent: item.suggested_test
    }));
  return [...evidenceTests, ...contractTests, ...intentTests].slice(0, 10);
}

function inferFramework(path) {
  if (String(path).endsWith(".py")) return "pytest";
  if (/\.(ts|tsx|js|jsx)$/.test(String(path))) return "vitest";
  return "repo-default";
}

function learningRecommendations({ policyFindings, promptFindings, contractFindings, baseSummary }) {
  const recommendations = [];
  if (baseSummary.evidence_findings.length > 3) {
    recommendations.push("Consider a repo policy requiring test evidence for risky source paths.");
  }
  if (policyFindings.length) {
    recommendations.push("Track owner overrides for policy findings before promoting this gate to required.");
  }
  if (promptFindings.length) {
    recommendations.push("Add golden prompt outputs for the changed prompt paths to reduce future false positives.");
  }
  if (contractFindings.length) {
    recommendations.push("Attach shape-only runtime contracts to this service in CI or staging.");
  }
  return recommendations;
}
