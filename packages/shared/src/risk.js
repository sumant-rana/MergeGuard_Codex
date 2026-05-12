export function clampRisk(value) {
  return Math.max(0, Math.min(100, Math.round(value)));
}

export function summarizeAnalysis(classifiedFiles, options = {}) {
  const files = [...classifiedFiles];
  const testsChanged = files.some((file) => file.classification === "test");
  const docsOnly = files.length > 0 && files.every((file) => ["docs", "generated"].includes(file.classification));
  const sourceFiles = files.filter((file) =>
    ["logic", "security-sensitive", "prompt", "wiring"].includes(file.classification)
  );
  const riskySourceFiles = sourceFiles.filter((file) => file.risk_score >= 45);
  const missingEvidenceFiles = sourceFiles.filter((file) => !testsChanged && file.classification !== "wiring");

  const topRisks = files
    .map((file) => file.risk_score)
    .sort((a, b) => b - a)
    .slice(0, 5);
  const topRiskAverage = topRisks.length ? topRisks.reduce((sum, score) => sum + score, 0) / topRisks.length : 0;
  const maxFileRisk = topRisks[0] || 0;
  const diffSize = files.reduce((sum, file) => sum + file.changes, 0);
  const baseDiffRisk = Math.min(20, diffSize / 18);
  const missingEvidenceRisk = missingEvidenceFiles.length ? Math.min(26, 10 + missingEvidenceFiles.length * 4) : 0;
  const riskyEvidenceRisk = riskySourceFiles.length && !testsChanged ? 12 : 0;
  const testCredit = testsChanged ? 14 : 0;
  const docsCredit = docsOnly ? 8 : 0;

  const riskScore = clampRisk(
    maxFileRisk * 0.45 +
      topRiskAverage * 0.25 +
      baseDiffRisk +
      missingEvidenceRisk +
      riskyEvidenceRisk -
      testCredit -
      docsCredit
  );

  const hotspots = files
    .filter((file) => file.risk_score >= 30 || file.must_inspect)
    .sort((a, b) => b.risk_score - a.risk_score)
    .slice(0, options.hotspotLimit || 12)
    .map((file) => ({
      path: file.path,
      symbol: null,
      risk_score: file.risk_score,
      reason: file.risk_reasons.join("; "),
      owner: "unassigned",
      required_action: requiredActionForFile(file, testsChanged)
    }));

  const evidenceFindings = missingEvidenceFiles.map((file) => ({
    type: "missing-test",
    path: file.path,
    status: "missing",
    confidence: file.risk_score >= 45 ? 0.82 : 0.68,
    severity: file.risk_score >= 45 ? "review_required" : "warn",
    message:
      file.risk_score >= 45
        ? "Risky source change has no changed test evidence in this PR."
        : "Source change has no changed test evidence in this PR.",
    suggested_action: suggestedTestAction(file)
  }));

  const status = determineReadinessStatus({ riskScore, evidenceFindings, docsOnly, mode: options.mode });
  const topBlocker = pickTopBlocker({ status, evidenceFindings, hotspots, docsOnly });
  const nextAction = nextActionFor({ status, topBlocker, testsChanged, hotspots, docsOnly });

  return {
    risk_score: riskScore,
    status,
    top_blocker: topBlocker,
    next_action: nextAction,
    tests_changed: testsChanged,
    docs_only: docsOnly,
    hotspots,
    evidence_findings: evidenceFindings,
    file_groups: groupFiles(files),
    checklist: buildChecklist({ hotspots, evidenceFindings, testsChanged, docsOnly })
  };
}

function determineReadinessStatus({ riskScore, evidenceFindings, docsOnly, mode }) {
  if (docsOnly) return "pass";
  const severeMissingEvidence = evidenceFindings.some((finding) => finding.severity === "review_required");
  if (mode === "blocking" && severeMissingEvidence) return "blocked";
  if (riskScore >= 50 || evidenceFindings.length > 0) return "review";
  return "pass";
}

function pickTopBlocker({ status, evidenceFindings, hotspots, docsOnly }) {
  if (docsOnly) return null;
  const severeEvidence = evidenceFindings.find((finding) => finding.severity === "review_required");
  if (severeEvidence) return severeEvidence.message;
  if (evidenceFindings.length) return evidenceFindings[0].message;
  if (status === "review" && hotspots.length) return `Inspect high-risk change in ${hotspots[0].path}.`;
  if (status === "blocked") return "Blocking review gate is unresolved.";
  return null;
}

function nextActionFor({ status, topBlocker, testsChanged, hotspots, docsOnly }) {
  if (docsOnly) return "Review documentation wording and merge when normal review is complete.";
  if (topBlocker?.includes("no changed test")) return "Add or link test evidence, or record reviewer acceptance with rationale.";
  if (status === "blocked") return "Resolve the blocking finding or request an owner override.";
  if (hotspots.length) return `Start review with ${hotspots[0].path}, then verify evidence for the changed behavior.`;
  if (testsChanged) return "Review changed tests and confirm they cover the intended behavior.";
  return "Perform normal code review.";
}

function requiredActionForFile(file, testsChanged) {
  if (file.classification === "prompt") return "Run or review prompt drift check evidence.";
  if (file.classification === "security-sensitive" && !testsChanged) return "Inspect closely and require test or trace evidence.";
  if (file.classification === "security-sensitive") return "Inspect sensitive behavior and verify updated tests.";
  if (file.risk_score >= 45) return "Inspect behavior and failure modes.";
  return "Review briefly for unintended scope.";
}

function suggestedTestAction(file) {
  if (file.classification === "prompt") return `Add prompt drift check coverage for ${file.path}.`;
  if (file.path.includes("auth")) return `Add auth success and denial-path tests for ${file.path}.`;
  if (file.path.includes("payment") || file.path.includes("billing")) return `Add monetary side-effect and idempotency tests for ${file.path}.`;
  if (file.path.includes("retry") || file.path.includes("timeout")) return `Add timeout, retry, and failure-mode tests for ${file.path}.`;
  return `Add or link tests that exercise the changed behavior in ${file.path}.`;
}

function groupFiles(files) {
  const groups = {
    must_inspect: [],
    safe_to_skim: [],
    generated: [],
    tests: [],
    docs: [],
    wiring: [],
    logic: [],
    prompt: []
  };

  for (const file of files) {
    if (file.must_inspect) groups.must_inspect.push(file);
    if (file.safe_to_skim) groups.safe_to_skim.push(file);
    if (file.generated) groups.generated.push(file);
    if (file.classification === "test") groups.tests.push(file);
    if (file.classification === "docs") groups.docs.push(file);
    if (file.classification === "wiring") groups.wiring.push(file);
    if (["logic", "security-sensitive"].includes(file.classification)) groups.logic.push(file);
    if (file.classification === "prompt") groups.prompt.push(file);
  }

  for (const key of Object.keys(groups)) {
    groups[key].sort((a, b) => b.risk_score - a.risk_score || a.path.localeCompare(b.path));
  }

  return groups;
}

function buildChecklist({ hotspots, evidenceFindings, testsChanged, docsOnly }) {
  if (docsOnly) {
    return ["Confirm the documentation change matches the linked intent.", "Check links, examples, and version references."];
  }

  const checklist = [];
  if (hotspots[0]) checklist.push(`Inspect ${hotspots[0].path} first because it has the highest risk score.`);
  if (evidenceFindings.length) checklist.push("Confirm missing evidence is added, linked, or explicitly accepted by the reviewer.");
  if (!testsChanged) checklist.push("Ask why no tests changed for this behavior change.");
  if (hotspots.some((hotspot) => hotspot.reason.includes("prompt"))) checklist.push("Verify prompt/model behavior with a golden prompt or manual before/after output.");
  if (hotspots.some((hotspot) => hotspot.reason.includes("auth") || hotspot.reason.includes("billing"))) checklist.push("Review authorization, idempotency, and rollback behavior.");
  if (!checklist.length) checklist.push("Perform standard review and confirm the diff matches the PR intent.");
  return checklist.slice(0, 5);
}
