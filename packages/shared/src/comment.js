export const STICKY_COMMENT_MARKER = "<!-- mergeguard:review-brief -->";

export function renderReviewComment({ pr, run, summary, dashboardUrl }) {
  const state = String(summary.status || "analysis_failed").toUpperCase().replace("_", " ");
  const topBlocker = summary.top_blocker || "None.";
  const mustInspect = summary.file_groups?.must_inspect || [];
  const safeToSkim = summary.file_groups?.safe_to_skim || [];
  const evidenceFindings = summary.evidence_findings || [];
  const policyFindings = summary.policy_findings || [];
  const promptFindings = summary.prompt_findings || [];
  const contractFindings = summary.contract_findings || [];
  const checklist = summary.checklist || [];

  return `${STICKY_COMMENT_MARKER}
## MergeGuard Review Brief

**Merge readiness:** ${state} · **Risk:** ${summary.risk_score ?? "n/a"}/100  
**Top blocker:** ${topBlocker}  
**Next action:** ${summary.next_action || "Review the dashboard for details."}

### Risk Hotspots
${formatFiles(mustInspect, "No must-inspect hotspots found.")}

### Safe To Skim
${formatFiles(safeToSkim.slice(0, 8), "No safe-to-skim groups identified.")}

### Intent Gaps And Missing Evidence
${formatIntentAndEvidence(summary.intent_items || [], summary.unexpected_scope_findings || [], evidenceFindings)}

### Prompt Or Contract Drift
${formatDrift({ policyFindings, promptFindings, contractFindings })}

### Reviewer Checklist
${formatChecklist(checklist)}

[Open MergeGuard dashboard](${dashboardUrl})${run?.id ? ` · Analysis run \`${run.id}\`` : ""}
`;
}

function formatFiles(files, empty) {
  if (!files.length) return empty;
  return files
    .slice(0, 10)
    .map((file) => `- \`${file.path}\` (${file.classification}, risk ${file.risk_score}) — ${firstReason(file)}`)
    .join("\n");
}

function firstReason(file) {
  if (Array.isArray(file.risk_reasons) && file.risk_reasons.length) return file.risk_reasons[0];
  if (file.reason) return file.reason;
  return "review attention recommended";
}

function formatEvidence(findings) {
  if (!findings.length) return "No missing-evidence findings in the Stage 1 analysis.";
  return findings
    .slice(0, 8)
    .map((finding) => `- \`${finding.path}\`: ${finding.message} Suggested action: ${finding.suggested_action}`)
    .join("\n");
}

function formatIntentAndEvidence(intentItems, unexpectedScope, evidenceFindings) {
  const lines = [];
  for (const finding of evidenceFindings.slice(0, 5)) {
    lines.push(`- \`${finding.path}\`: ${finding.message} Suggested action: ${finding.suggested_action}`);
  }
  for (const item of intentItems.filter((intent) => intent.evidence_status === "missing").slice(0, 3)) {
    lines.push(`- Intent missing evidence: ${item.text}`);
  }
  for (const finding of unexpectedScope.slice(0, 3)) {
    lines.push(`- \`${finding.path}\`: ${finding.message}`);
  }
  return lines.length ? lines.join("\n") : "No intent gaps or missing-evidence findings.";
}

function formatDrift({ policyFindings, promptFindings, contractFindings }) {
  const lines = [];
  for (const finding of policyFindings.slice(0, 3)) lines.push(`- Policy: ${finding.message}`);
  for (const finding of promptFindings.slice(0, 3)) lines.push(`- Prompt: ${finding.message}`);
  for (const finding of contractFindings.slice(0, 3)) lines.push(`- Contract: ${finding.violated_assumption}`);
  return lines.length ? lines.join("\n") : "No prompt, policy, or runtime contract drift detected.";
}

function formatChecklist(checklist) {
  if (!checklist.length) return "- Perform standard review and confirm the diff matches the PR intent.";
  return checklist.map((item) => `- ${item}`).join("\n");
}
