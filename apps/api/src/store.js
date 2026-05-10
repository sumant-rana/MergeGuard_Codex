import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { newId } from "../../../packages/shared/src/ids.js";
import { defaultPolicyPack } from "../../../packages/shared/src/policy.js";

const EMPTY_STATE = {
  organizations: [],
  repositories: [],
  pullRequests: [],
  analysisRuns: [],
  changedFiles: [],
  intentItems: [],
  behavioralDeltas: [],
  hotspots: [],
  evidenceLinks: [],
  conceptFindings: [],
  contractFindings: [],
  promptCanaryRuns: [],
  blastRadius: [],
  checkResults: [],
  webhookEvents: [],
  policyPacks: [],
  reviewerOverrides: [],
  postMergeOutcomes: [],
  thresholdRecommendations: [],
  auditExports: []
};

export class JsonStore {
  constructor(filePath) {
    this.filePath = resolve(process.cwd(), filePath);
    this.state = structuredClone(EMPTY_STATE);
  }

  async init() {
    await mkdir(dirname(this.filePath), { recursive: true });
    try {
      const raw = await readFile(this.filePath, "utf8");
      this.state = { ...structuredClone(EMPTY_STATE), ...JSON.parse(raw) };
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      await this.save();
    }
  }

  async save() {
    await mkdir(dirname(this.filePath), { recursive: true });
    const temp = `${this.filePath}.tmp`;
    await writeFile(temp, `${JSON.stringify(this.state, null, 2)}\n`, "utf8");
    await rename(temp, this.filePath);
  }

  async upsertOrganization(input) {
    const existing = this.state.organizations.find((org) => org.github_org_id === input.github_org_id);
    const now = new Date().toISOString();
    if (existing) {
      Object.assign(existing, { ...input, updated_at: now });
      await this.save();
      return existing;
    }

    const org = {
      id: newId("org"),
      github_org_id: input.github_org_id,
      name: input.name,
      plan: input.plan || "local",
      created_at: now,
      updated_at: now
    };
    this.state.organizations.push(org);
    await this.save();
    return org;
  }

  async upsertRepository(input) {
    const existing = this.state.repositories.find((repo) => repo.github_repo_id === input.github_repo_id);
    const now = new Date().toISOString();
    if (existing) {
      Object.assign(existing, { ...input, updated_at: now });
      await this.save();
      return existing;
    }

    const repo = {
      id: newId("repo"),
      org_id: input.org_id,
      github_repo_id: input.github_repo_id,
      owner: input.owner,
      name: input.name,
      default_branch: input.default_branch || "main",
      installation_id: input.installation_id || null,
      enabled: input.enabled !== false,
      created_at: now,
      updated_at: now
    };
    this.state.repositories.push(repo);
    await this.save();
    return repo;
  }

  async upsertPullRequest(input) {
    const existing = this.state.pullRequests.find((pr) => pr.repo_id === input.repo_id && pr.number === input.number);
    const now = new Date().toISOString();
    if (existing) {
      Object.assign(existing, { ...input, updated_at: now });
      await this.save();
      return existing;
    }

    const pr = {
      id: newId("pr"),
      repo_id: input.repo_id,
      number: input.number,
      title: input.title,
      author: input.author,
      base_sha: input.base_sha,
      head_sha: input.head_sha,
      state: input.state || "open",
      draft: Boolean(input.draft),
      body: input.body || "",
      labels: input.labels || [],
      github_url: input.github_url || null,
      created_at: input.created_at || now,
      updated_at: now
    };
    this.state.pullRequests.push(pr);
    await this.save();
    return pr;
  }

  findWebhookEvent(idempotencyKey) {
    return this.state.webhookEvents.find((event) => event.idempotency_key === idempotencyKey);
  }

  async recordWebhookEvent(input) {
    const existing = this.findWebhookEvent(input.idempotency_key);
    const now = new Date().toISOString();
    if (existing) return existing;

    const event = {
      id: newId("evt"),
      idempotency_key: input.idempotency_key,
      github_delivery_id: input.github_delivery_id,
      event_name: input.event_name,
      action: input.action,
      repository_id: input.repository_id,
      pull_number: input.pull_number,
      head_sha: input.head_sha,
      status: input.status || "received",
      run_id: input.run_id || null,
      error: null,
      created_at: now,
      updated_at: now
    };
    this.state.webhookEvents.push(event);
    await this.save();
    return event;
  }

  async updateWebhookEvent(idempotencyKey, patch) {
    const event = this.findWebhookEvent(idempotencyKey);
    if (!event) return null;
    Object.assign(event, patch, { updated_at: new Date().toISOString() });
    await this.save();
    return event;
  }

  async createAnalysisRun(input) {
    const now = new Date().toISOString();
    const run = {
      id: newId("run"),
      pr_id: input.pr_id,
      head_sha: input.head_sha,
      status: input.status || "running",
      started_at: now,
      completed_at: null,
      trigger: input.trigger || "webhook",
      duration_ms: null,
      summary: null,
      external_sync: null
    };
    this.state.analysisRuns.push(run);
    await this.save();
    return run;
  }

  async completeAnalysisRun(runId, patch) {
    const run = this.state.analysisRuns.find((item) => item.id === runId);
    if (!run) throw new Error(`Analysis run not found: ${runId}`);
    Object.assign(run, patch, {
      completed_at: patch.completed_at || new Date().toISOString(),
      status: patch.status || "completed"
    });
    await this.save();
    return run;
  }

  async replaceRunArtifacts(runId, {
    changedFiles,
    intentItems,
    behavioralDeltas,
    hotspots,
    evidenceLinks,
    conceptFindings,
    contractFindings,
    promptCanaryRuns,
    blastRadius,
    checkResults
  }) {
    this.state.changedFiles = this.state.changedFiles.filter((file) => file.run_id !== runId);
    this.state.intentItems = this.state.intentItems.filter((item) => item.run_id !== runId);
    this.state.behavioralDeltas = this.state.behavioralDeltas.filter((delta) => delta.run_id !== runId);
    this.state.hotspots = this.state.hotspots.filter((hotspot) => hotspot.run_id !== runId);
    this.state.evidenceLinks = this.state.evidenceLinks.filter((link) => link.run_id !== runId);
    this.state.conceptFindings = this.state.conceptFindings.filter((finding) => finding.run_id !== runId);
    this.state.contractFindings = this.state.contractFindings.filter((finding) => finding.run_id !== runId);
    this.state.promptCanaryRuns = this.state.promptCanaryRuns.filter((canary) => canary.run_id !== runId);
    this.state.blastRadius = this.state.blastRadius.filter((item) => item.run_id !== runId);
    this.state.checkResults = this.state.checkResults.filter((check) => check.run_id !== runId);

    if (changedFiles) {
      this.state.changedFiles.push(
        ...changedFiles.map((file) => ({ id: newId("file"), run_id: runId, ...file }))
      );
    }
    if (intentItems) {
      this.state.intentItems.push(
        ...intentItems.map((item) => ({ id: newId("intent"), run_id: runId, ...item }))
      );
    }
    if (behavioralDeltas) {
      this.state.behavioralDeltas.push(
        ...behavioralDeltas.map((delta) => ({ id: newId("delta"), run_id: runId, ...delta }))
      );
    }
    if (hotspots) {
      this.state.hotspots.push(
        ...hotspots.map((hotspot) => ({ id: newId("hotspot"), run_id: runId, ...hotspot }))
      );
    }
    if (evidenceLinks) {
      this.state.evidenceLinks.push(
        ...evidenceLinks.map((link) => ({ id: newId("evidence"), run_id: runId, ...link }))
      );
    }
    if (conceptFindings) {
      this.state.conceptFindings.push(
        ...conceptFindings.map((finding) => ({ id: finding.id || newId("concept"), run_id: runId, ...finding }))
      );
    }
    if (contractFindings) {
      this.state.contractFindings.push(
        ...contractFindings.map((finding) => ({ id: newId("contract"), run_id: runId, ...finding }))
      );
    }
    if (promptCanaryRuns) {
      this.state.promptCanaryRuns.push(
        ...promptCanaryRuns.map((canary) => ({ id: newId("canary"), run_id: runId, ...canary }))
      );
    }
    if (blastRadius) {
      this.state.blastRadius.push(
        ...blastRadius.map((item) => ({ id: newId("blast"), run_id: runId, ...item }))
      );
    }
    if (checkResults) {
      this.state.checkResults.push(
        ...checkResults.map((check) => ({ id: newId("check"), run_id: runId, ...check }))
      );
    }

    await this.save();
  }

  latestRunForPr(prId) {
    return this.state.analysisRuns
      .filter((run) => run.pr_id === prId)
      .sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)))[0] || null;
  }

  getRun(runId) {
    const run = this.state.analysisRuns.find((item) => item.id === runId);
    if (!run) return null;
    return {
      ...run,
      changed_files: this.state.changedFiles.filter((file) => file.run_id === runId),
      intent_items: this.state.intentItems.filter((item) => item.run_id === runId),
      behavioral_deltas: this.state.behavioralDeltas.filter((delta) => delta.run_id === runId),
      hotspots: this.state.hotspots.filter((hotspot) => hotspot.run_id === runId),
      evidence_links: this.state.evidenceLinks.filter((link) => link.run_id === runId),
      concept_findings: this.state.conceptFindings.filter((finding) => finding.run_id === runId),
      contract_findings: this.state.contractFindings.filter((finding) => finding.run_id === runId),
      prompt_canary_runs: this.state.promptCanaryRuns.filter((canary) => canary.run_id === runId),
      blast_radius: this.state.blastRadius.filter((item) => item.run_id === runId),
      check_results: this.state.checkResults.filter((check) => check.run_id === runId)
    };
  }

  listRepositories() {
    return this.state.repositories.map((repo) => ({
      ...repo,
      organization: this.state.organizations.find((org) => org.id === repo.org_id) || null
    }));
  }

  listInstallations() {
    const byInstallation = new Map();
    for (const repo of this.state.repositories) {
      const key = repo.installation_id || "local";
      if (!byInstallation.has(key)) byInstallation.set(key, { installation_id: key, repositories: [] });
      byInstallation.get(key).repositories.push(repo);
    }
    return [...byInstallation.values()];
  }

  listPullRequests(filters = {}) {
    const rows = this.state.pullRequests.map((pr) => this.buildPrRow(pr));
    return rows
      .filter((row) => !filters.repo || `${row.repository.owner}/${row.repository.name}` === filters.repo)
      .filter(
        (row) =>
          !filters.owner ||
          row.author === filters.owner ||
          (row.latest_run?.summary?.owner_summary || []).some((owner) => owner.owner === filters.owner)
      )
      .filter((row) => !filters.risk_state || row.latest_run?.summary?.status === filters.risk_state)
      .filter((row) => !filters.label || row.labels.includes(filters.label))
      .sort((a, b) => (b.latest_run?.summary?.risk_score || 0) - (a.latest_run?.summary?.risk_score || 0));
  }

  getPullRequest(prId) {
    const pr = this.state.pullRequests.find((item) => item.id === prId);
    if (!pr) return null;
    const row = this.buildPrRow(pr);
    if (row.latest_run) {
      row.latest_run = this.getRun(row.latest_run.id);
    }
    return row;
  }

  findPullRequest(repoId, number) {
    return this.state.pullRequests.find((pr) => pr.repo_id === repoId && pr.number === number) || null;
  }

  buildPrRow(pr) {
    const repository = this.state.repositories.find((repo) => repo.id === pr.repo_id) || null;
    const organization = repository
      ? this.state.organizations.find((org) => org.id === repository.org_id) || null
      : null;
    const latestRun = this.latestRunForPr(pr.id);
    return {
      ...pr,
      repository,
      organization,
      latest_run: latestRun ? this.getRun(latestRun.id) : null
    };
  }

  auditForPullRequest(prId) {
    const pr = this.getPullRequest(prId);
    if (!pr) return null;
    const runs = this.state.analysisRuns
      .filter((run) => run.pr_id === prId)
      .map((run) => this.getRun(run.id));
    const events = this.state.webhookEvents.filter((event) => event.pull_number === pr.number);
    return {
      exported_at: new Date().toISOString(),
      pull_request: pr,
      analysis_runs: runs,
      webhook_events: events,
      policy_packs: this.state.policyPacks.filter((pack) => pack.repo_id === pr.repo_id || pack.repo_id === pr.repository?.id || !pack.repo_id),
      overrides: this.state.reviewerOverrides.filter((override) => runs.some((run) => run.id === override.run_id)),
      post_merge_outcomes: this.state.postMergeOutcomes.filter((outcome) => outcome.pr_id === pr.id),
      reconstruction: {
        latest_status: pr.latest_run?.summary?.status || "unknown",
        latest_top_blocker: pr.latest_run?.summary?.top_blocker || null,
        checks: pr.latest_run?.check_results || [],
        labels: pr.latest_run?.external_sync?.labels || []
      }
    };
  }

  listPolicyPacks(repoId = null) {
    const packs = this.state.policyPacks.filter((pack) => !repoId || pack.repo_id === repoId || pack.repo_id === null);
    return packs.length ? packs : [defaultPolicyPack(repoId)];
  }

  activePolicyPacksForRepo(repoId) {
    const packs = this.state.policyPacks.filter((pack) => pack.active && (pack.repo_id === repoId || pack.repo_id === null));
    return packs.length ? packs : [defaultPolicyPack(repoId)];
  }

  async createPolicyPack(input) {
    const now = new Date().toISOString();
    const pack = {
      id: newId("policy"),
      repo_id: input.repo_id || null,
      name: input.name || "Policy Pack",
      yaml: input.yaml || "",
      version: input.version || 1,
      active: Boolean(input.active),
      created_by: input.created_by || "local",
      created_at: now,
      updated_at: now
    };
    this.state.policyPacks.push(pack);
    await this.save();
    return pack;
  }

  async activatePolicyPack(policyPackId) {
    const pack = this.state.policyPacks.find((item) => item.id === policyPackId);
    if (!pack) return null;
    for (const item of this.state.policyPacks) {
      if (item.repo_id === pack.repo_id) item.active = false;
    }
    pack.active = true;
    pack.updated_at = new Date().toISOString();
    await this.save();
    return pack;
  }

  async recordReviewerOverride(input) {
    const now = new Date().toISOString();
    const override = {
      id: newId("override"),
      run_id: input.run_id,
      finding_id: input.finding_id,
      reviewer: input.reviewer || "local-reviewer",
      reason: input.reason,
      created_at: now,
      later_outcome: null
    };
    this.state.reviewerOverrides.push(override);
    await this.save();
    return override;
  }

  async recordPostMergeOutcome(input) {
    const now = new Date().toISOString();
    const outcome = {
      id: newId("outcome"),
      pr_id: input.pr_id,
      outcome_type: input.outcome_type,
      label: input.label || null,
      notes: input.notes || "",
      created_at: now
    };
    this.state.postMergeOutcomes.push(outcome);
    await this.save();
    return outcome;
  }

  metrics() {
    const runs = this.state.analysisRuns;
    const completed = runs.filter((run) => run.status === "completed");
    const failed = runs.filter((run) => run.status === "failed");
    const durations = completed.map((run) => run.duration_ms).filter((duration) => typeof duration === "number").sort((a, b) => a - b);
    const latestRows = this.listPullRequests();
    return {
      generated_at: new Date().toISOString(),
      webhook_received_count: this.state.webhookEvents.length,
      analysis: {
        queued: runs.filter((run) => run.status === "queued").length,
        running: runs.filter((run) => run.status === "running").length,
        completed: completed.length,
        failed: failed.length,
        duration_ms: {
          p50: percentile(durations, 0.5),
          p95: percentile(durations, 0.95),
          p99: percentile(durations, 0.99)
        }
      },
      findings: {
        evidence: this.state.evidenceLinks.length,
        concept: this.state.conceptFindings.length,
        contract: this.state.contractFindings.length,
        prompt_canary_failures: this.state.promptCanaryRuns.filter((run) => run.status === "fail").length
      },
      override_rate: this.state.reviewerOverrides.length / Math.max(1, this.state.evidenceLinks.length + this.state.conceptFindings.length),
      queue: {
        open_prs: latestRows.length,
        high_risk: latestRows.filter((row) => (row.latest_run?.summary?.risk_score || 0) >= 65).length,
        blocked: latestRows.filter((row) => row.latest_run?.summary?.status === "blocked").length,
        review: latestRows.filter((row) => row.latest_run?.summary?.status === "review").length
      },
      alerts: buildAlerts({ runs, latestRows })
    };
  }
}

function percentile(values, p) {
  if (!values.length) return null;
  const index = Math.min(values.length - 1, Math.floor(values.length * p));
  return values[index];
}

function buildAlerts({ runs, latestRows }) {
  const alerts = [];
  const failed = runs.filter((run) => run.status === "failed").length;
  if (failed / Math.max(1, runs.length) > 0.2) alerts.push("analysis failure rate above 20%");
  if (latestRows.some((row) => row.latest_run?.summary?.status === "analysis_failed")) alerts.push("one or more PRs have failed analysis");
  if (latestRows.filter((row) => row.latest_run?.summary?.status === "review").length > 10) alerts.push("review queue backlog above 10 PRs");
  return alerts;
}
