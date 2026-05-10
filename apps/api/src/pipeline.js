import { analyzePullRequest } from "../../../workers/analyzer/src/analyze.js";
import { renderReviewComment, STICKY_COMMENT_MARKER } from "../../../packages/shared/src/comment.js";
import { buildWebhookIdempotencyKey } from "../../../packages/shared/src/ids.js";
import { allManagedLabelNames, labelsForSummary, MANAGED_LABELS } from "../../../packages/shared/src/labels.js";

const SUPPORTED_PR_ACTIONS = new Set(["opened", "synchronize", "reopened", "ready_for_review", "edited"]);

export async function processGitHubWebhook({ eventName, deliveryId, payload, store, github, config }) {
  if (eventName === "pull_request") {
    return processPullRequestWebhook({ deliveryId, payload, store, github, config });
  }

  return {
    status: "ignored",
    message: `Event ${eventName} is accepted by the GitHub App but has no analysis handler yet.`
  };
}

export async function processPullRequestWebhook({ deliveryId, payload, store, github, config }) {
  const action = payload.action;
  const prPayload = payload.pull_request;
  const repoPayload = payload.repository;

  if (!prPayload || !repoPayload) {
    return { status: "ignored", message: "Webhook payload did not include pull_request and repository." };
  }

  if (!SUPPORTED_PR_ACTIONS.has(action)) {
    return { status: "ignored", message: `Pull request action ${action} is not analyzed.` };
  }

  const idempotencyKey = buildWebhookIdempotencyKey({
    deliveryId,
    repositoryId: repoPayload.id,
    pullNumber: prPayload.number,
    headSha: prPayload.head?.sha,
    action
  });

  const duplicate = store.findWebhookEvent(idempotencyKey);
  if (duplicate?.run_id) {
    return {
      status: "duplicate",
      message: "Duplicate webhook delivery ignored.",
      run: store.getRun(duplicate.run_id)
    };
  }

  await store.recordWebhookEvent({
    idempotency_key: idempotencyKey,
    github_delivery_id: deliveryId,
    event_name: "pull_request",
    action,
    repository_id: repoPayload.id,
    pull_number: prPayload.number,
    head_sha: prPayload.head?.sha
  });

  const org = await store.upsertOrganization({
    github_org_id: payload.organization?.id || repoPayload.owner?.id || repoPayload.owner?.login,
    name: payload.organization?.login || repoPayload.owner?.login || repoPayload.owner?.name || "local"
  });

  const repo = await store.upsertRepository({
    org_id: org.id,
    github_repo_id: repoPayload.id,
    owner: repoPayload.owner?.login || repoPayload.full_name?.split("/")[0],
    name: repoPayload.name,
    default_branch: repoPayload.default_branch,
    installation_id: payload.installation?.id || null,
    enabled: true
  });

  const pr = await store.upsertPullRequest({
    repo_id: repo.id,
    number: prPayload.number,
    title: prPayload.title,
    body: prPayload.body || "",
    author: prPayload.user?.login || "unknown",
    base_sha: prPayload.base?.sha,
    head_sha: prPayload.head?.sha,
    state: prPayload.state,
    draft: prPayload.draft,
    labels: (prPayload.labels || []).map((label) => label.name || label),
    github_url: prPayload.html_url,
    created_at: prPayload.created_at,
    updated_at: prPayload.updated_at
  });

  const run = await store.createAnalysisRun({
    pr_id: pr.id,
    head_sha: pr.head_sha,
    trigger: `github:${action}`
  });
  await store.updateWebhookEvent(idempotencyKey, { status: "running", run_id: run.id });

  try {
    const started = Date.now();
    const changedFiles = await getChangedFiles({ payload, repo, pr, github, config });
    const analysis = analyzePullRequest(
      {
        pr,
        repository: repo,
        changedFiles,
        codeowners: payload.mergeguard?.codeowners || payload.mergeguard_codeowners || "",
        promptSuites: payload.mergeguard?.prompt_suites || [],
        contractSummaries: payload.mergeguard?.contracts || []
      },
      {
        checkMode: config.checkMode,
        policyPacks: store.activePolicyPacksForRepo(repo.id)
      }
    );
    const dashboardUrl = `${config.publicUrl}/?pr=${encodeURIComponent(pr.id)}`;
    const checkResults = buildCheckResults({ summary: analysis.summary, dashboardUrl, checkMode: config.checkMode });

    await store.replaceRunArtifacts(run.id, {
      changedFiles: analysis.changedFiles,
      intentItems: analysis.intentItems,
      behavioralDeltas: analysis.behavioralDeltas,
      hotspots: analysis.summary.hotspots,
      evidenceLinks: analysis.summary.evidence_findings.map((finding) => ({
        finding_id: null,
        type: finding.type,
        path: finding.path,
        test_name: null,
        url: null,
        confidence: finding.confidence,
        status: finding.status,
        severity: finding.severity,
        message: finding.message,
        suggested_action: finding.suggested_action
      })),
      conceptFindings: [...analysis.conceptFindings, ...analysis.policyFindings],
      contractFindings: analysis.contractFindings,
      promptCanaryRuns: analysis.promptCanaryRuns,
      blastRadius: analysis.blastRadius,
      checkResults
    });

    const externalSync = await syncGitHubOutputs({ pr, repo, run, summary: analysis.summary, github, config, dashboardUrl });
    const completedRun = await store.completeAnalysisRun(run.id, {
      status: "completed",
      duration_ms: Date.now() - started,
      summary: analysis.summary,
      external_sync: externalSync
    });
    await store.updateWebhookEvent(idempotencyKey, { status: "processed", error: null });

    return {
      status: "processed",
      pr,
      run: store.getRun(completedRun.id)
    };
  } catch (error) {
    const summary = {
      risk_score: 100,
      status: "analysis_failed",
      top_blocker: error.message,
      next_action: "Inspect MergeGuard logs, fix the analyzer failure, then rerun analysis.",
      evidence_findings: [],
      file_groups: { must_inspect: [], safe_to_skim: [] },
      checklist: ["Rerun MergeGuard after the analysis failure is resolved."]
    };
    await store.replaceRunArtifacts(run.id, {
      changedFiles: [],
      intentItems: [],
      behavioralDeltas: [],
      hotspots: [],
      evidenceLinks: [],
      conceptFindings: [],
      contractFindings: [],
      promptCanaryRuns: [],
      blastRadius: [],
      checkResults: [
        {
          check_name: "MergeGuard Review Brief",
          conclusion: "failure",
          summary: error.message,
          blocking: true,
          details_url: `${config.publicUrl}/?pr=${encodeURIComponent(pr.id)}`
        }
      ]
    });
    await store.completeAnalysisRun(run.id, {
      status: "failed",
      duration_ms: null,
      summary,
      external_sync: { skipped: true, reason: "analysis failed before sync" }
    });
    await store.updateWebhookEvent(idempotencyKey, { status: "failed", error: error.stack || error.message });
    return {
      status: "failed",
      error: error.message,
      run: store.getRun(run.id)
    };
  }
}

export async function rerunAnalysisForPullRequest({ prId, store, github, config }) {
  const current = store.getPullRequest(prId);
  if (!current) return null;

  const previousRun = current.latest_run;
  const previousFiles = previousRun?.changed_files || [];
  const run = await store.createAnalysisRun({
    pr_id: current.id,
    head_sha: current.head_sha,
    trigger: "manual-rerun"
  });
  const started = Date.now();
  const analysis = analyzePullRequest(
    { pr: current, repository: current.repository, changedFiles: previousFiles },
    { checkMode: config.checkMode, policyPacks: store.activePolicyPacksForRepo(current.repository.id) }
  );
  const dashboardUrl = `${config.publicUrl}/?pr=${encodeURIComponent(current.id)}`;
  const checkResults = buildCheckResults({ summary: analysis.summary, dashboardUrl, checkMode: config.checkMode });

  await store.replaceRunArtifacts(run.id, {
    changedFiles: analysis.changedFiles,
    intentItems: analysis.intentItems,
    behavioralDeltas: analysis.behavioralDeltas,
    hotspots: analysis.summary.hotspots,
    evidenceLinks: analysis.summary.evidence_findings,
    conceptFindings: [...analysis.conceptFindings, ...analysis.policyFindings],
    contractFindings: analysis.contractFindings,
    promptCanaryRuns: analysis.promptCanaryRuns,
    blastRadius: analysis.blastRadius,
    checkResults
  });
  const externalSync = await syncGitHubOutputs({
    pr: current,
    repo: current.repository,
    run,
    summary: analysis.summary,
    github,
    config,
    dashboardUrl
  });
  await store.completeAnalysisRun(run.id, {
    status: "completed",
    duration_ms: Date.now() - started,
    summary: analysis.summary,
    external_sync: externalSync
  });

  return store.getRun(run.id);
}

async function getChangedFiles({ payload, repo, pr, github, config }) {
  const fixtureFiles = payload.mergeguard?.changed_files || payload.mergeguard_changed_files;
  if (fixtureFiles && config.allowFixtureFiles) return fixtureFiles;

  if (!github.enabled) {
    throw new Error("GitHub API credentials are not configured and the webhook did not include mergeguard.changed_files.");
  }

  const files = await github.listPullFiles({
    owner: repo.owner,
    repo: repo.name,
    pullNumber: pr.number,
    installationId: repo.installation_id
  });

  return files.map((file) => ({
    path: file.filename,
    status: file.status,
    additions: file.additions,
    deletions: file.deletions,
    changes: file.changes,
    patch: file.patch
  }));
}

async function syncGitHubOutputs({ pr, repo, run, summary, github, config, dashboardUrl }) {
  const labels = labelsForSummary(summary, run.status);
  const commentBody = renderReviewComment({ pr, run, summary, dashboardUrl });
  const checkResults = buildCheckResults({ summary, dashboardUrl, checkMode: config.checkMode });

  if (!github.enabled) {
    return {
      skipped: true,
      reason: "GitHub credentials are not configured.",
      labels,
      managed_labels: allManagedLabelNames()
    };
  }

  const result = { labels, check_runs: [], comment: null, label_sync: null };
  for (const checkResult of checkResults) {
    result.check_runs.push(
      await github.createCheckRun({
        owner: repo.owner,
        repo: repo.name,
        headSha: pr.head_sha,
        name: checkResult.check_name,
        conclusion: checkResult.conclusion,
        summary: checkResult.summary,
        text: commentBody,
        detailsUrl: dashboardUrl,
        installationId: repo.installation_id
      })
    );
  }
  result.comment = await github.upsertStickyComment({
    owner: repo.owner,
    repo: repo.name,
    pullNumber: pr.number,
    marker: STICKY_COMMENT_MARKER,
    body: commentBody,
    installationId: repo.installation_id
  });
  await github.syncLabels({
    owner: repo.owner,
    repo: repo.name,
    pullNumber: pr.number,
    desiredLabels: labels,
    managedLabels: MANAGED_LABELS,
    installationId: repo.installation_id
  });
  result.label_sync = "completed";
  return result;
}

function buildCheckResults({ summary, dashboardUrl, checkMode }) {
  return [
    buildReviewBriefCheck({ summary, dashboardUrl, checkMode }),
    buildNamedGate({
      checkName: "Evidence Coverage",
      status: evidenceStatus(summary),
      summary: evidenceSummary(summary),
      dashboardUrl,
      checkMode
    }),
    buildNamedGate({
      checkName: "Intent Match",
      status: intentStatus(summary),
      summary: intentSummary(summary),
      dashboardUrl,
      checkMode
    }),
    buildNamedGate({
      checkName: "Behavioral Diff",
      status: behaviorStatus(summary),
      summary: behaviorSummary(summary),
      dashboardUrl,
      checkMode
    }),
    buildNamedGate({
      checkName: "Concept Policy",
      status: policyStatus(summary),
      summary: policySummary(summary),
      dashboardUrl,
      checkMode
    }),
    buildNamedGate({
      checkName: "Prompt Canary",
      status: promptStatus(summary),
      summary: promptSummary(summary),
      dashboardUrl,
      checkMode
    }),
    buildNamedGate({
      checkName: "Runtime Contracts",
      status: contractStatus(summary),
      summary: contractSummary(summary),
      dashboardUrl,
      checkMode
    })
  ];
}

function buildReviewBriefCheck({ summary, dashboardUrl, checkMode }) {
  let conclusion = "success";
  if (summary.status === "analysis_failed") conclusion = "failure";
  else if (summary.status === "blocked") conclusion = checkMode === "blocking" ? "failure" : "neutral";
  else if (summary.status === "review") conclusion = "neutral";

  return {
    check_name: "MergeGuard Review Brief",
    conclusion,
    summary: `${summary.status.toUpperCase().replace("_", " ")} · Risk ${summary.risk_score}/100 · ${summary.next_action}`,
    blocking: conclusion === "failure",
    details_url: dashboardUrl
  };
}

function buildNamedGate({ checkName, status, summary, dashboardUrl, checkMode }) {
  const conclusion =
    status === "fail" && checkMode === "blocking"
      ? "failure"
      : status === "fail" || status === "review"
        ? "neutral"
        : "success";
  return {
    check_name: checkName,
    conclusion,
    summary,
    blocking: conclusion === "failure",
    details_url: dashboardUrl
  };
}

function evidenceStatus(summary) {
  return summary.evidence_findings?.some((finding) => finding.severity === "review_required") ? "review" : "pass";
}

function evidenceSummary(summary) {
  const count = summary.evidence_findings?.length || 0;
  return count ? `${count} missing evidence finding(s).` : "Changed behavior has Stage 1 evidence coverage.";
}

function intentStatus(summary) {
  return (summary.unexpected_scope_findings?.length || 0) || summary.intent_items?.some((item) => item.evidence_status === "missing")
    ? "review"
    : "pass";
}

function intentSummary(summary) {
  const missing = summary.intent_items?.filter((item) => item.evidence_status === "missing").length || 0;
  const scope = summary.unexpected_scope_findings?.length || 0;
  return missing || scope ? `${missing} missing intent item(s), ${scope} unexpected scope finding(s).` : "Intent is mapped to implementation evidence.";
}

function behaviorStatus(summary) {
  return summary.behavioral_deltas?.some((delta) => delta.severity === "review_required") ? "review" : "pass";
}

function behaviorSummary(summary) {
  const count = summary.behavioral_deltas?.length || 0;
  return count ? `${count} behavior delta(s) summarized.` : "No behavior deltas detected.";
}

function policyStatus(summary) {
  return summary.policy_findings?.some((finding) => finding.severity === "block")
    ? "fail"
    : summary.policy_findings?.length
      ? "review"
      : "pass";
}

function policySummary(summary) {
  const count = summary.policy_findings?.length || 0;
  return count ? `${count} concept policy finding(s).` : "Concept policy rules pass.";
}

function promptStatus(summary) {
  return summary.prompt_findings?.length ? "fail" : "pass";
}

function promptSummary(summary) {
  const runs = summary.prompt_canary_runs?.length || 0;
  const failures = summary.prompt_findings?.length || 0;
  return runs ? `${runs} prompt canary suite(s), ${failures} failure(s).` : "No prompt/model canaries required.";
}

function contractStatus(summary) {
  return summary.contract_findings?.some((finding) => finding.severity === "review_required" || finding.severity === "block")
    ? "review"
    : "pass";
}

function contractSummary(summary) {
  const count = summary.contract_findings?.length || 0;
  return count ? `${count} runtime contract finding(s).` : "No runtime contract drift detected.";
}
