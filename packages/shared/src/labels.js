export const MANAGED_LABELS = {
  highRisk: {
    name: "mergeguard/high-risk",
    color: "B60205",
    description: "MergeGuard identified elevated merge risk."
  },
  missingEvidence: {
    name: "mergeguard/missing-evidence",
    color: "D93F0B",
    description: "Changed behavior lacks test, trace, contract, or reviewer evidence."
  },
  safeToSkim: {
    name: "mergeguard/safe-to-skim",
    color: "0E8A16",
    description: "MergeGuard found low-risk file groups suitable for lighter review."
  },
  analysisFailed: {
    name: "mergeguard/analysis-failed",
    color: "5319E7",
    description: "MergeGuard analysis could not complete."
  },
  intentDrift: {
    name: "mergeguard/intent-drift",
    color: "FBCA04",
    description: "MergeGuard found possible mismatch between PR intent and implementation."
  },
  policyBlocked: {
    name: "mergeguard/policy-blocked",
    color: "B60205",
    description: "MergeGuard concept policy blocked this PR."
  },
  promptDrift: {
    name: "mergeguard/prompt-drift",
    color: "C5DEF5",
    description: "MergeGuard found prompt, model, or agent drift."
  },
  override: {
    name: "mergeguard/override",
    color: "F9D0C4",
    description: "A reviewer or owner override was recorded."
  }
};

export function labelsForSummary(summary, analysisStatus = "completed") {
  if (analysisStatus === "failed") return [MANAGED_LABELS.analysisFailed.name];

  const labels = [];
  if ((summary.risk_score || 0) >= 65) labels.push(MANAGED_LABELS.highRisk.name);
  if ((summary.evidence_findings || []).length > 0) labels.push(MANAGED_LABELS.missingEvidence.name);
  if ((summary.unexpected_scope_findings || []).length > 0 || (summary.intent_items || []).some((item) => item.evidence_status === "missing")) {
    labels.push(MANAGED_LABELS.intentDrift.name);
  }
  if ((summary.policy_findings || []).some((finding) => finding.policy_result === "block" || finding.severity === "block")) {
    labels.push(MANAGED_LABELS.policyBlocked.name);
  }
  if ((summary.prompt_findings || []).length > 0) labels.push(MANAGED_LABELS.promptDrift.name);
  if ((summary.file_groups?.safe_to_skim || []).length > 0) labels.push(MANAGED_LABELS.safeToSkim.name);
  return labels;
}

export function allManagedLabelNames() {
  return Object.values(MANAGED_LABELS).map((label) => label.name);
}
