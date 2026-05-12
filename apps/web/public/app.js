const state = {
  prs: [],
  selectedPrId: new URLSearchParams(window.location.search).get("pr") || null,
  filters: {
    repo: "",
    owner: "",
    risk_state: ""
  }
};

const metricsEl = document.querySelector("#metrics");
const queueBodyEl = document.querySelector("#queue-body");
const detailEl = document.querySelector("#detail-pane");
const repoFilterEl = document.querySelector("#repo-filter");
const ownerFilterEl = document.querySelector("#owner-filter");
const stateFilterEl = document.querySelector("#state-filter");
const refreshButton = document.querySelector("#refresh-button");

refreshButton.addEventListener("click", () => loadQueue());
document.querySelector("#filters").addEventListener("change", (event) => {
  state.filters[event.target.name] = event.target.value;
  loadQueue();
});

await loadQueue();

async function loadQueue() {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(state.filters)) {
    if (value) params.set(key, value);
  }

  const data = await fetchJson(`/api/prs${params.toString() ? `?${params}` : ""}`);
  state.prs = data.pull_requests || [];

  if (!state.selectedPrId && state.prs[0]) {
    state.selectedPrId = state.prs[0].id;
  }

  renderMetrics();
  renderFilters();
  renderQueue();
  await renderDetail();
}

function renderMetrics() {
  const highRisk = state.prs.filter((pr) => latestSummary(pr).risk_score >= 65).length;
  const missingEvidence = state.prs.filter((pr) => (latestSummary(pr).evidence_findings || []).length > 0).length;
  const review = state.prs.filter((pr) => latestSummary(pr).status === "review").length;
  const avgRisk = state.prs.length
    ? Math.round(state.prs.reduce((sum, pr) => sum + (latestSummary(pr).risk_score || 0), 0) / state.prs.length)
    : 0;

  metricsEl.innerHTML = [
    metric("Open PRs", state.prs.length),
    metric("High Risk", highRisk),
    metric("Missing Evidence", missingEvidence),
    metric("Average Risk", avgRisk),
    metric("Needs Review", review)
  ]
    .slice(0, 4)
    .join("");
}

function renderFilters() {
  const repos = unique(state.prs.map((pr) => repoSlug(pr)).filter(Boolean));
  const owners = unique(
    state.prs
      .flatMap((pr) => [pr.author, ...(latestSummary(pr).owner_summary || []).map((owner) => owner.owner)])
      .filter(Boolean)
  );
  updateSelect(repoFilterEl, repos, "All repos", state.filters.repo);
  updateSelect(ownerFilterEl, owners, "All owners", state.filters.owner);
}

function renderQueue() {
  if (!state.prs.length) {
    queueBodyEl.innerHTML = `<tr><td colspan="5" class="subtle">No analyzed pull requests.</td></tr>`;
    detailEl.innerHTML = `<div class="empty-detail">No pull request data available.</div>`;
    return;
  }

  queueBodyEl.innerHTML = state.prs
    .map((pr) => {
      const summary = latestSummary(pr);
      const selected = pr.id === state.selectedPrId ? " selected" : "";
      return `<tr class="queue-row${selected}" data-pr-id="${escapeAttr(pr.id)}">
        <td class="risk-cell">${summary.risk_score ?? "n/a"}</td>
        <td>
          <span class="pr-title">#${pr.number} ${escapeHtml(pr.title)}</span>
          <span class="repo-name">${escapeHtml(repoSlug(pr))}</span>
        </td>
        <td>${escapeHtml(pr.author || "unknown")}</td>
        <td>${statePill(summary.status)}</td>
        <td>${escapeHtml(summary.next_action || "Analysis pending.")}</td>
      </tr>`;
    })
    .join("");

  for (const row of queueBodyEl.querySelectorAll(".queue-row")) {
    row.addEventListener("click", async () => {
      state.selectedPrId = row.dataset.prId;
      const url = new URL(window.location.href);
      url.searchParams.set("pr", state.selectedPrId);
      history.replaceState(null, "", url);
      renderQueue();
      await renderDetail();
    });
  }
}

async function renderDetail() {
  if (!state.selectedPrId) return;
  const data = await fetchJson(`/api/prs/${encodeURIComponent(state.selectedPrId)}`);
  const pr = data.pull_request;
  if (!pr) return;

  const run = pr.latest_run;
  const summary = run?.summary || {};
  const files = run?.changed_files || [];
  const mustInspect = summary.file_groups?.must_inspect || files.filter((file) => file.must_inspect);
  const safeToSkim = summary.file_groups?.safe_to_skim || files.filter((file) => file.safe_to_skim);
  const evidence = summary.evidence_findings || run?.evidence_links || [];
  const checks = run?.check_results || [];
  const intentItems = summary.intent_items || run?.intent_items || [];
  const behaviorDeltas = summary.behavioral_deltas || run?.behavioral_deltas || [];
  const blastRadius = summary.blast_radius || run?.blast_radius || [];
  const policyFindings = summary.policy_findings || [];
  const promptRuns = summary.prompt_canary_runs || run?.prompt_canary_runs || [];
  const contractFindings = summary.contract_findings || run?.contract_findings || [];
  const suggestedTests = summary.suggested_tests || [];
  const hotspotThemes = summary.hotspot_themes || [];
  const ownerSummary = summary.owner_summary || [];

  detailEl.innerHTML = `
    <div class="detail-header">
      <div class="detail-title-row">
        <div>
          <h2>#${pr.number} ${escapeHtml(pr.title)}</h2>
          <div class="subtle">${escapeHtml(repoSlug(pr))} · ${escapeHtml(pr.author || "unknown")} · ${escapeHtml(run?.status || "no run")}</div>
        </div>
        <button class="rerun-button" type="button" title="Rerun analysis" aria-label="Rerun analysis">R</button>
      </div>
      <div class="readiness-grid">
        <div class="readiness-item"><span>Risk</span><strong>${summary.risk_score ?? "n/a"}/100</strong></div>
        <div class="readiness-item"><span>Gate</span><strong>${statePill(summary.status)}</strong></div>
        <div class="readiness-item"><span>Top Blocker</span><strong>${escapeHtml(summary.top_blocker || "None")}</strong></div>
      </div>
    </div>

    <section class="detail-section">
      <h3>Next Action</h3>
      <p>${escapeHtml(summary.next_action || "Analysis pending.")}</p>
    </section>

    <section class="detail-section">
      <h3>Must Inspect</h3>
      ${fileList(mustInspect)}
    </section>

    <section class="detail-section">
      <h3>Hotspot Themes And Owners</h3>
      ${chipList([...hotspotThemes.map((item) => `${item.theme}: ${item.count}`), ...ownerSummary.map((item) => `${item.owner}: ${item.file_count}`)])}
    </section>

    <section class="detail-section">
      <h3>Requirements vs Implementation</h3>
      ${intentList(intentItems, summary.unexpected_scope_findings || [])}
    </section>

    <section class="detail-section">
      <h3>Behavior Impact</h3>
      ${behaviorList(behaviorDeltas)}
    </section>

    <section class="detail-section">
      <h3>Blast Radius</h3>
      ${blastList(blastRadius)}
    </section>

    <section class="detail-section">
      <h3>Safe To Skim</h3>
      ${fileList(safeToSkim)}
    </section>

    <section class="detail-section">
      <h3>Missing Evidence</h3>
      ${evidenceList(evidence)}
    </section>

    <section class="detail-section">
      <h3>Policy Guardrails</h3>
      ${policyList(policyFindings)}
    </section>

    <section class="detail-section">
      <h3>Prompt Canaries</h3>
      ${promptList(promptRuns)}
    </section>

    <section class="detail-section">
      <h3>Runtime Contracts And Suggested Tests</h3>
      ${contractList(contractFindings, suggestedTests)}
    </section>

    <section class="detail-section">
      <h3>Reviewer Checklist</h3>
      ${checklist(summary.checklist || [])}
    </section>

    <section class="detail-section">
      <h3>Checks</h3>
      ${checkList(checks)}
    </section>
  `;

  detailEl.querySelector(".rerun-button").addEventListener("click", async () => {
    await fetchJson(`/api/prs/${encodeURIComponent(pr.id)}/rerun`, { method: "POST" });
    await loadQueue();
  });
}

function metric(label, value) {
  return `<div class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function fileList(files) {
  if (!files.length) return `<p class="subtle">No files in this group.</p>`;
  return `<ul class="file-list">${files
    .map(
      (file) => `<li class="file-item">
        <span class="file-path">${escapeHtml(file.path)}</span>
        <span class="file-meta">
          <span>${escapeHtml(file.classification || "unknown")}</span>
          <span>risk ${escapeHtml(file.risk_score ?? "n/a")}</span>
          <span>+${escapeHtml(file.additions || 0)} / -${escapeHtml(file.deletions || 0)}</span>
          <span>${escapeHtml((file.risk_reasons || ["low-risk file shape"])[0])}</span>
        </span>
      </li>`
    )
    .join("")}</ul>`;
}

function evidenceList(findings) {
  if (!findings.length) return `<p class="subtle">No missing-evidence findings.</p>`;
  return `<ul class="evidence-list">${findings
    .map(
      (finding) => `<li class="evidence-item">
        <strong>${escapeHtml(finding.path || "unknown")}</strong>
        <div>${escapeHtml(finding.message || finding.status || "Evidence finding")}</div>
        <div class="subtle">${escapeHtml(finding.suggested_action || "")}</div>
      </li>`
    )
    .join("")}</ul>`;
}

function intentList(items, unexpectedScope) {
  if (!items.length && !unexpectedScope.length) return `<p class="subtle">No structured intent findings.</p>`;
  return `<ul class="evidence-list">${[
    ...items.map(
      (item) => `<li class="evidence-item">
        <strong>${escapeHtml(item.category)} · ${escapeHtml(item.evidence_status || "unknown")}</strong>
        <div>${escapeHtml(item.text)}</div>
        <div class="subtle">${escapeHtml((item.mapped_paths || []).join(", ") || item.suggested_test || "")}</div>
      </li>`
    ),
    ...unexpectedScope.map(
      (finding) => `<li class="evidence-item">
        <strong>unexpected scope · ${escapeHtml(finding.severity)}</strong>
        <div>${escapeHtml(finding.message)}</div>
        <div class="subtle">${escapeHtml(finding.suggested_action || "")}</div>
      </li>`
    )
  ].join("")}</ul>`;
}

function behaviorList(items) {
  if (!items.length) return `<p class="subtle">No behavior deltas detected.</p>`;
  return `<ul class="evidence-list">${items
    .slice(0, 10)
    .map(
      (item) => `<li class="evidence-item">
        <strong>${escapeHtml(item.path)}${item.symbol ? ` · ${escapeHtml(item.symbol)}` : ""}</strong>
        <div>${escapeHtml(item.new_behavior || item.summary || "")}</div>
        <div class="subtle">${escapeHtml(item.divergent_input ? `Divergent example: ${item.divergent_input}` : item.severity)}</div>
      </li>`
    )
    .join("")}</ul>`;
}

function blastList(items) {
  if (!items.length) return `<p class="subtle">No blast-radius items.</p>`;
  return `<ul class="evidence-list">${items
    .slice(0, 8)
    .map(
      (item) => `<li class="evidence-item">
        <strong>${escapeHtml(item.path)}</strong>
        <div>${escapeHtml((item.downstream_services || []).join(", "))}</div>
        <div class="subtle">Callers: ${escapeHtml((item.direct_callers || []).join(", "))} · Tests: ${escapeHtml((item.impacted_tests || []).join(", "))}</div>
      </li>`
    )
    .join("")}</ul>`;
}

function policyList(items) {
  if (!items.length) return `<p class="subtle">Policy guardrails pass.</p>`;
  return `<ul class="evidence-list">${items
    .map(
      (item) => `<li class="evidence-item">
        <strong>${escapeHtml(item.rule_id || item.concept)} · ${escapeHtml(item.severity || "info")}</strong>
        <div>${escapeHtml(item.message || item.relation || "")}</div>
        <div class="subtle">${escapeHtml(item.owner || "unassigned")} · ${escapeHtml(item.path || "")}</div>
      </li>`
    )
    .join("")}</ul>`;
}

function promptList(items) {
  if (!items.length) return `<p class="subtle">No prompt drift checks required.</p>`;
  return `<ul class="evidence-list">${items
    .map(
      (item) => `<li class="evidence-item">
        <strong>${escapeHtml(item.suite)} · ${escapeHtml(item.status)}</strong>
        <div>${escapeHtml(item.prompt_path)} · model ${escapeHtml(item.model || "default")}</div>
        <div class="subtle">correctness ${escapeHtml(item.correctness)} · format ${escapeHtml(item.format)} · latency ${escapeHtml(item.latency)}ms · cost ${escapeHtml(item.cost)}%</div>
      </li>`
    )
    .join("")}</ul>`;
}

function contractList(contractFindings, suggestedTests) {
  const rows = [
    ...contractFindings.map(
      (finding) => `<li class="evidence-item">
        <strong>${escapeHtml(finding.path)} · ${escapeHtml(finding.severity)}</strong>
        <div>${escapeHtml(finding.violated_assumption || "")}</div>
        <div class="subtle">${escapeHtml(finding.suggested_test?.intent || "")}</div>
      </li>`
    ),
    ...suggestedTests.map(
      (test) => `<li class="evidence-item">
        <strong>${escapeHtml(test.framework)} · ${escapeHtml(test.path)}</strong>
        <div>${escapeHtml(test.intent)}</div>
      </li>`
    )
  ];
  return rows.length ? `<ul class="evidence-list">${rows.join("")}</ul>` : `<p class="subtle">No runtime contract drift or generated tests.</p>`;
}

function chipList(items) {
  if (!items.length) return `<p class="subtle">No themes yet.</p>`;
  return `<div class="chip-list">${items.map((item) => `<span class="chip">${escapeHtml(item)}</span>`).join("")}</div>`;
}

function checklist(items) {
  if (!items.length) return `<p class="subtle">No checklist items.</p>`;
  return `<ul class="checklist">${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
}

function checkList(items) {
  if (!items.length) return `<p class="subtle">No check results.</p>`;
  return `<ul class="file-list">${items
    .map(
      (item) => `<li class="file-item">
        <span class="file-path">${escapeHtml(item.check_name)}</span>
        <span class="file-meta"><span>${escapeHtml(item.conclusion)}</span><span>${escapeHtml(item.summary)}</span></span>
      </li>`
    )
    .join("")}</ul>`;
}

function latestSummary(pr) {
  return pr.latest_run?.summary || {};
}

function repoSlug(pr) {
  return pr.repository ? `${pr.repository.owner}/${pr.repository.name}` : "";
}

function statePill(status = "analysis_failed") {
  const clean = String(status || "analysis_failed");
  return `<span class="state-pill state-${escapeAttr(clean)}">${escapeHtml(clean.toUpperCase().replace("_", " "))}</span>`;
}

function updateSelect(select, values, fallback, selected) {
  const current = selected || select.value;
  select.innerHTML = `<option value="">${escapeHtml(fallback)}</option>${values
    .map((value) => `<option value="${escapeAttr(value)}">${escapeHtml(value)}</option>`)
    .join("")}`;
  select.value = values.includes(current) ? current : "";
}

function unique(values) {
  return [...new Set(values)].sort((a, b) => a.localeCompare(b));
}

async function fetchJson(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`${response.status} ${body}`);
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("`", "&#96;");
}
