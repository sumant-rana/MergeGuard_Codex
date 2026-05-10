# MergeGuard Command Center Implementation Specification

**Source:** `github_reviewops_command_center.docx`  
**Purpose:** Convert the MergeGuard product brief into an implementation-ready engineering specification.  
**Principle:** Every stage must deliver usable incremental value. Stage 1 must be a thin but complete end-to-end MVP.

## 1. Product Objective

MergeGuard Command Center is a GitHub-native ReviewOps platform that turns pull-request review into a risk-informed merge decision workflow.

The system must answer seven questions for every pull request:

1. Did the PR implement the stated ticket or PR intent?
2. What behavior changed, not just what text changed?
3. Which files/functions require close review, and which are safe to skim?
4. What risky behavior changed without sufficient tests, traces, contracts, or canary evidence?
5. Did the PR introduce a concept-policy violation such as PII write without authorization?
6. Did a prompt, model, or agent workflow change in a way that should block merge?
7. What must the author or reviewer do next?

## 2. Success Definition

The implementation is successful when a team can install MergeGuard on one or more GitHub repositories and get:

- A risk-sorted queue dashboard for open PRs.
- A PR detail dashboard with merge readiness, hotspots, evidence, checks, and actions.
- A single updated PR comment with a concise review brief.
- GitHub check runs that can be advisory first and required later.
- Labels that route PRs by risk, missing evidence, intent drift, policy blockers, and prompt drift.
- Policy configuration that teams can tune without code changes.
- Learning data from reviewer overrides and post-merge outcomes.

## 3. Scope

### In Scope

- GitHub App for webhook ingestion, check runs, comments, labels, and repository installation.
- Optional GitHub Action or reusable workflow for running repo-local analyzers.
- Web dashboard with queue and PR detail views.
- Analysis pipeline for review compression, intent extraction, semantic diff, evidence mapping, concept policy, prompt canaries, runtime contracts, and reviewer actions.
- Persistent findings store, concept index, analysis run history, and override history.
- Team policy packs in YAML.
- Audit export for security/compliance review.
- Advisory mode and blocking mode.

### Out of Scope

- Auto-approval of PRs.
- Full replacement of GitHub, CI, or existing code review tools.
- General-purpose code completion.
- Blocking a merge without explainable evidence and an owner-controlled override path.
- Full language coverage in the MVP. Python and TypeScript are first-class initial targets.

## 4. Users and Jobs

| User | Job To Be Done | Required Product Behavior |
| --- | --- | --- |
| PR author | Understand blockers before asking for review | Author sees analysis status, top blocker, missing evidence, suggested tests, and prompt canary failures. |
| Human reviewer | Spend attention where risk is highest | Reviewer gets safe-to-skim groups, must-inspect hotspots, behavioral deltas, and a concrete checklist. |
| Staff engineer | Keep review quality high as PR volume grows | Staff view shows risk-sorted queue, cross-repo trends, policy drift, and override patterns. |
| Security/compliance reviewer | Prove dangerous concepts are guarded | Dashboard exposes policy findings, evidence links, audit exports, and code owner override trails. |
| Engineering manager | See bottlenecks and release risk | Queue view shows blocked PRs, average review time, high-risk themes, and recurring blockers. |

## 5. System Architecture

### 5.1 Recommended Initial Stack

| Layer | Recommendation | Reason |
| --- | --- | --- |
| Frontend | Next.js / React / TypeScript | Fast dashboard iteration and GitHub OAuth-friendly UI. |
| API service | Node.js TypeScript with Fastify or NestJS | Fits GitHub App/webhook ecosystem and shared types with frontend. |
| Analyzer workers | Python and TypeScript worker processes | Python has strong static analysis tooling; TypeScript fits JS/TS repos. |
| Queue | Redis + BullMQ, or managed equivalent | Analysis should be asynchronous and retryable. |
| Database | PostgreSQL | Relational entities: PRs, findings, checks, policy packs, overrides. |
| Object storage | S3-compatible storage or local dev filesystem | Store raw analysis artifacts, logs, rendered reports, canary outputs. |
| Auth | GitHub OAuth for users; GitHub App installation tokens for repos | Native permissions model. |
| Deployment | Containerized services | Enables local dev, staging, and production parity. |

### 5.2 Logical Services

| Service | Responsibility |
| --- | --- |
| GitHub App Service | Receives webhooks, validates signatures, manages installations, posts comments, creates check runs, applies labels. |
| Ingestion Service | Normalizes PR metadata, changed files, commit SHAs, CODEOWNERS, PR text, linked issues, and prompt path changes. |
| Analysis Orchestrator | Creates analysis runs, schedules workers, aggregates findings, computes merge readiness, emits events. |
| Sandbox Runner | Checks out base and head revisions, prepares repo workspace, runs analyzers safely. |
| Review Compression Worker | Scores files/functions by risk and reviewer payoff; classifies safe-to-skim vs must-inspect. |
| Intent Worker | Extracts intended behaviors from PR text, linked issue, ticket, and docs. |
| Semantic Diff Worker | Produces behavior deltas, divergent examples, invariant changes, and blast radius. |
| Evidence Worker | Links intent and behavior claims to tests, traces, canaries, contracts, or missing proof. |
| Concept Policy Worker | Maintains concept index and evaluates team policy rules. |
| Prompt Canary Worker | Runs golden prompt suites for changed prompt/model files. |
| Contract Worker | Compares runtime contract summaries and generates suggested tests. |
| Dashboard API | Serves queue, PR detail, findings, config, overrides, metrics, and audit exports. |
| Learning Service | Captures overrides, reviewer actions, post-merge outcomes, and threshold tuning signals. |

### 5.3 Event Flow

1. GitHub sends `pull_request.opened`, `pull_request.synchronize`, `pull_request.reopened`, or `check_suite.rerequested`.
2. GitHub App Service validates payload and stores webhook event.
3. Ingestion Service fetches PR metadata, changed files, PR description, linked issue references, checks, labels, CODEOWNERS, and relevant files.
4. Analysis Orchestrator creates an `AnalysisRun` with status `queued`.
5. Workers run in parallel where possible.
6. Findings are persisted with evidence links, severities, confidence, and suggested actions.
7. Merge readiness is computed.
8. GitHub App updates check runs, PR comment, labels, and dashboard state.
9. Reviewer approves, requests changes, or overrides findings with rationale.
10. Learning Service captures outcome and uses it to tune future scoring.

## 6. Core Domain Model

### 6.1 Entities

| Entity | Key Fields |
| --- | --- |
| Organization | id, github_org_id, name, plan, created_at |
| Repository | id, org_id, github_repo_id, owner, name, default_branch, installation_id, enabled |
| PullRequest | id, repo_id, number, title, author, base_sha, head_sha, state, draft, labels, created_at, updated_at |
| AnalysisRun | id, pr_id, head_sha, status, started_at, completed_at, trigger, duration_ms, summary |
| ChangedFile | id, run_id, path, status, additions, deletions, language, generated, classification |
| IntentItem | id, run_id, text, category, source, confidence, severity, out_of_scope |
| BehavioralDelta | id, run_id, path, symbol, old_behavior, new_behavior, divergent_input, severity, confidence |
| Hotspot | id, run_id, path, symbol, risk_score, reason, owner, required_action |
| EvidenceLink | id, run_id, finding_id, type, path, test_name, url, confidence, status |
| ConceptFinding | id, run_id, concept, path, symbol, confidence, relation, policy_result, severity |
| ContractFinding | id, run_id, path, symbol, old_contract, new_contract, violated_assumption, generated_test_status |
| PromptCanaryRun | id, run_id, suite, prompt_path, model, correctness, format, style, latency, cost, drift_summary |
| CheckResult | id, run_id, check_name, conclusion, summary, blocking, details_url |
| ReviewerOverride | id, run_id, finding_id, reviewer, reason, created_at, later_outcome |
| PolicyPack | id, repo_id, name, yaml, version, active, created_by |

### 6.2 Finding Severity

| Severity | Meaning | Default GitHub Behavior |
| --- | --- | --- |
| Info | Useful context, not review-blocking | Include in dashboard only. |
| Warn | Reviewer should inspect | Include in PR brief; check remains passing unless policy says otherwise. |
| Review Required | Human decision required before merge | Check is neutral or action-required in advisory mode. |
| Block | Merge should not proceed without fix or owner override | Check fails in blocking mode. |

### 6.3 Merge Readiness Score

The merge readiness model produces:

- `risk_score`: 0-100, where higher means more merge risk.
- `status`: `pass`, `review`, `blocked`, `analysis_failed`, or `stale`.
- `top_blocker`: single highest-priority unresolved blocker.
- `next_action`: specific action for author/reviewer.

Initial scoring can be heuristic:

```text
risk_score =
  base_diff_risk
  + ownership_risk
  + hotspot_risk
  + missing_evidence_risk
  + policy_risk
  + prompt_drift_risk
  + contract_drift_risk
  - test_coverage_credit
```

Later stages can replace coefficients with calibrated models, but every score must remain explainable.

## 7. Dashboard Requirements

### 7.1 Queue Dashboard

The queue dashboard must support engineering leads, staff reviewers, and managers.

Required widgets:

- Open PR count.
- High-risk PR count.
- PRs blocked by missing evidence.
- Average review time and trend.
- Risk-sorted queue.
- Gate state per PR: `PASS`, `REVIEW`, `BLOCKED`, `CANARY FAIL`, `ANALYSIS FAILED`.
- Owner/team.
- Next action.
- Hotspot themes.
- Review bottlenecks.
- Filters for repo, owner, team, risk state, label, and age.

Acceptance criteria:

- A reviewer can identify the highest-risk open PR in under 10 seconds.
- A manager can see the most common blocker category for the current queue.
- The queue updates after PR webhook events and analysis completion.

### 7.2 PR Detail Dashboard

Required regions:

| Region | Minimum Fields |
| --- | --- |
| Merge readiness | Risk score, check state, top blocker, next action, stale analysis warning. |
| Intent vs implementation | Intent items, mapped code paths, evidence status, unmatched intent, unexpected scope. |
| Behavioral diff | Changed symbols, old behavior, new behavior, divergent examples, confidence, severity. |
| Review compression | Must-inspect files, safe-to-skim files, generated/mechanical/wiring/logic split. |
| Blast radius | Callers, downstream services, owners, CODEOWNERS, impacted tests. |
| Evidence coverage | Tests, traces, contracts, canaries, missing proof, suggested test cases. |
| Concept policy gates | Rule id, concept, location, pass/fail/warn, override owner. |
| Runtime contracts | Contract changes, violated assumptions, generated test status. |
| Prompt canaries | Correctness, format, style, refusal, latency, cost, before/after outputs. |
| Reviewer actions | Checklist, approve/request changes links, override form, issue creation. |

Acceptance criteria:

- The top blocker visible in GitHub is also visible in the dashboard.
- Every blocking finding has a source, evidence, severity, owner, and remediation suggestion.
- Reviewer overrides require a reason and are written to history.

### 7.3 PR Comment

The bot must post a single sticky comment and update it instead of spamming the PR.

Required sections:

1. Merge readiness and top blocker.
2. Risk hotspots.
3. Safe-to-skim groups.
4. Intent gaps and missing evidence.
5. Prompt or contract drift, if present.
6. Reviewer checklist.
7. Links to full dashboard, traces, generated tests, and audit export if available.

The comment must remain concise. Detailed findings belong in the dashboard.

## 8. GitHub Integration Requirements

### 8.1 GitHub App Permissions

Minimum permissions:

- Pull requests: read/write.
- Checks: read/write.
- Contents: read.
- Issues: read, optionally write for generated follow-up issues.
- Metadata: read.
- Commit statuses: read/write if check runs are insufficient.

### 8.2 Webhooks

Required events:

- `pull_request`
- `pull_request_review`
- `check_suite`
- `check_run`
- `issue_comment`
- `installation`
- `installation_repositories`

### 8.3 Check Runs

Required checks:

| Check | Stage Introduced | Pass | Block |
| --- | --- | --- | --- |
| MergeGuard Review Brief | Stage 1 | Analysis completed and no hard blocker remains | Analysis failed or top blocker unresolved |
| Evidence Coverage | Stage 1 thin, Stage 3 full | Critical behaviors have evidence or reviewer acceptance | Critical behavior lacks proof |
| Intent Match | Stage 3 | Critical intent items map to implementation evidence | Missing intent, unexpected scope, ambiguous intent |
| Behavioral Diff | Stage 4 | No unresolved high-severity behavior divergence | Broken invariant or unexplained high-risk divergence |
| Concept Policy | Stage 5 | Policy rules pass or are overridden by owner | Dangerous concept combination introduced |
| Prompt Canary | Stage 6 | Canary thresholds pass | Drift or format/schema failure |
| Runtime Contracts | Stage 7 | No unresolved contract drift | Violated assumption without evidence or owner override |

### 8.4 Labels

Required labels:

- `mergeguard/high-risk`
- `mergeguard/missing-evidence`
- `mergeguard/intent-drift`
- `mergeguard/policy-blocked`
- `mergeguard/prompt-drift`
- `mergeguard/safe-to-skim`
- `mergeguard/analysis-failed`
- `mergeguard/override`

## 9. Analysis Components

### 9.1 Review Compression

Purpose: decide how a reviewer should spend time.

Inputs:

- Changed files, additions, deletions.
- File type and language.
- Generated file detection.
- CODEOWNERS.
- Path risk patterns.
- Keyword risk patterns.
- Historical incident paths.
- Concept tags when available.

Outputs:

- File classification: `generated`, `mechanical`, `wiring`, `test`, `docs`, `logic`, `security-sensitive`, `prompt`.
- Hotspot list with risk score and reason.
- Safe-to-skim groups.
- PR-specific checklist.

Stage 1 can use heuristics. Stage 2 should add AST signals. Stage 5 should incorporate concept tags.

### 9.2 Intent Extraction

Purpose: turn PR/ticket text into a review spec.

Inputs:

- PR title and description.
- Linked GitHub issues.
- Optional Jira/Linear ticket text.
- Linked docs.
- Commit messages.

Outputs:

- `should` intent items.
- `must_not` constraints.
- `out_of_scope` constraints.
- Confidence and source for each item.

Rules:

- If intent is ambiguous, the finding should request clarification instead of inventing requirements.
- Extracted intent is editable or overrideable by reviewer/author.

### 9.3 Semantic Diff

Purpose: explain what changed in behavior.

Inputs:

- Base and head source.
- Changed symbols.
- Tests and call graph.
- Optional traces.

Outputs:

- Changed function summaries.
- Old vs new behavior.
- Divergent examples where available.
- Invariant changes.
- Blast-radius relationships.

MVP can start with changed-function summaries and risky keyword/path heuristics. Later stages add AST, type-aware, test-aware, and trace-aware analysis.

### 9.4 Evidence Mapping

Purpose: separate proven behavior from belief.

Evidence types:

- Unit test.
- Integration test.
- E2E test.
- Static proof.
- Runtime trace.
- Contract summary.
- Prompt canary result.
- Reviewer acceptance.

Outputs:

- Evidence links by finding and intent item.
- Missing evidence findings.
- Suggested test cases.

### 9.5 Concept Policy

Purpose: turn code comprehension into enforceable policy.

Initial concept taxonomy:

- `auth-check`
- `pii-read`
- `pii-write`
- `billing-side-effect`
- `idempotency-key-check`
- `external-http-call`
- `timeout-configured`
- `retry-with-backoff`
- `raw-sql`
- `cache-invalidate`
- `feature-flag-read`
- `prompt-change`
- `agent-tool-call`

Policy language must support:

- `when`
- `require`
- `require_any`
- `severity`
- `changed_paths`
- confidence threshold
- owner override requirement

### 9.6 Prompt Canary

Purpose: block unsafe prompt/model/config changes.

Inputs:

- Changed prompt/model paths.
- Golden prompt suite.
- Expected assertions: exact match, regex, JSON schema, rubric, latency, token cost.

Outputs:

- Before/after run result.
- Correctness score.
- Format score.
- Style/verbosity/refusal drift.
- Latency and cost delta.
- Blocking finding when threshold fails.

### 9.7 Runtime Contract Comparator

Purpose: catch implicit assumption drift.

Inputs:

- Contract summaries from staging/prod-safe shape capture.
- Changed functions.
- Optional generated property tests.

Outputs:

- Changed contract findings.
- Violated assumptions.
- Suggested property tests.
- Runtime guard recommendations.

Privacy rule: record shapes and invariants, not raw PII values.

### 9.8 Learning Loop

Purpose: make findings and thresholds improve with use.

Captured signals:

- Reviewer override reason.
- Whether finding caused request-changes.
- Whether PR later caused incident or revert.
- Whether reviewer inspected safe-to-skim files anyway.
- Which checks teams promote from advisory to blocking.

Outputs:

- Threshold recommendations.
- Per-team false-positive reports.
- Policy drift trends.
- Model calibration datasets.

## 10. Staged Implementation Plan

## Stage 1: Thin End-to-End MVP

### Goal

Deliver a working GitHub-to-dashboard-to-PR-comment loop that provides immediate review value, even if analysis is heuristic.

### Incremental Value

Teams can install the app on a repository and immediately see:

- PRs in a dashboard queue.
- Basic risk score.
- Changed-file classification.
- Missing-test signal.
- Sticky PR comment.
- GitHub check run.
- Labels for high risk, missing evidence, and safe-to-skim.

### Scope

Build:

1. GitHub App installation flow.
2. Webhook receiver for PR open/synchronize/reopen.
3. PR ingestion: title, description, author, base/head SHA, changed files, additions/deletions, labels.
4. Basic repo checkout or GitHub API diff retrieval.
5. Heuristic review compression:
   - Generated file detection by path and extension.
   - Test/docs/config/source classification.
   - Risk keywords: auth, token, payment, billing, refund, pii, sql, migration, retry, timeout, prompt.
   - Risk paths: `payments/**`, `auth/**`, `security/**`, `migrations/**`, `prompts/**`, `agents/**`.
6. Missing evidence v0:
   - Source change without nearby test change.
   - Risky source change without test file modification.
7. Dashboard:
   - Queue view.
   - PR detail shell.
   - Analysis status.
   - File groups.
   - Risk score.
   - Top blocker.
8. GitHub check run:
   - `MergeGuard Review Brief`.
   - Advisory by default.
9. Sticky PR comment:
   - Risk score.
   - Must-inspect files.
   - Safe-to-skim files.
   - Missing evidence warning.
   - Dashboard link.
10. Labels:
   - `mergeguard/high-risk`
   - `mergeguard/missing-evidence`
   - `mergeguard/safe-to-skim`
   - `mergeguard/analysis-failed`

### Exclusions

- No LLM requirement.
- No semantic diff beyond file/risk heuristics.
- No concept index.
- No prompt canary execution.
- No runtime contracts.
- No blocking checks by default.

### Acceptance Criteria

- Installing the app on a test repo succeeds.
- Opening a PR creates an analysis run within 10 seconds.
- Analysis completes within 60 seconds for a PR under 50 changed files.
- Dashboard shows the PR with risk state and file groups.
- PR comment is posted and updated on synchronize.
- Check run appears on the PR.
- A risky payment/auth/prompt PR gets a higher score than a docs-only PR.
- Re-running analysis does not create duplicate comments.

### Stage 1 Data Model Minimum

- Organization
- Repository
- PullRequest
- AnalysisRun
- ChangedFile
- Hotspot
- EvidenceLink
- CheckResult

### Stage 1 Technical Tasks

1. Create monorepo with `apps/api`, `apps/web`, `packages/shared`, `workers/analyzer`.
2. Implement GitHub App manifest and webhook signature verification.
3. Implement database schema and migrations.
4. Implement webhook event persistence and idempotency key.
5. Implement changed-file fetch via GitHub REST API.
6. Implement risk scoring heuristics.
7. Implement queue dashboard.
8. Implement PR detail page.
9. Implement sticky comment renderer.
10. Implement check-run creation/update.
11. Implement label creation and label application.
12. Add smoke tests against fixture webhook payloads.

## Stage 2: Review Compression Dashboard

### Goal

Make the dashboard genuinely useful for reviewer routing.

### Incremental Value

Reviewers can reduce review time by seeing safe-to-skim groups, must-inspect hotspots, ownership, and concrete review questions.

### Scope

Add:

- AST-aware file/symbol extraction for Python and TypeScript.
- Generated/mechanical/refactor/wiring/logic classifier.
- CODEOWNERS parsing and owner assignment.
- Reviewer checklist generator v1 using templates and heuristics.
- Queue filters by owner, repo, risk state, and label.
- Hotspot theme aggregation.
- Review bottleneck aggregation.
- PR detail drill-down by file/function.

### Acceptance Criteria

- 42-file mixed PR is split into safe-to-skim and must-inspect groups.
- Generated files are not ranked as top hotspots unless policy says otherwise.
- Reviewer checklist includes at least three PR-specific questions for high-risk PRs.
- Queue supports filtering by owner and high-risk state.

## Stage 3: Intent and Evidence Mapping

### Goal

Introduce the Truth Report: compare stated intent with implementation evidence.

### Incremental Value

Authors and reviewers can see when a PR does more, less, or different work than the ticket/PR description.

### Scope

Add:

- Intent extraction from PR title/description and linked GitHub issues.
- Optional Jira/Linear connector interface.
- Intent categories: `should`, `must_not`, `out_of_scope`.
- Evidence mapping to changed tests and referenced code paths.
- Missing evidence findings.
- Suggested test cases.
- `Intent Match` and `Evidence Coverage` checks.
- `mergeguard/intent-drift` label.

### Acceptance Criteria

- A PR description with explicit requirements produces structured intent items.
- Missing implementation for a critical intent item is surfaced.
- Unexpected risky change not tied to intent is surfaced as scope drift.
- Each intent item shows evidence status: `proven`, `partial`, `missing`, or `accepted`.

## Stage 4: Semantic Diff and Blast Radius

### Goal

Move from file-level review to behavior-level review.

### Incremental Value

Reviewers can inspect behavior deltas and impacted callers instead of manually reading every changed token.

### Scope

Add:

- Function-level extraction for Python and TypeScript.
- Base/head changed symbol mapping.
- Per-function behavior summaries.
- Risk category tagging: auth, data mutation, side effects, concurrency, error handling, config, prompt.
- Lightweight invariant detection from tests and simple symbolic traces where feasible.
- Divergent examples when inputs can be generated safely.
- Static call graph for changed functions.
- Blast-radius view in PR detail.
- `Behavioral Diff` check.

### Acceptance Criteria

- Changed functions show old behavior, new behavior, severity, and confidence.
- High-risk behavior deltas appear above low-risk formatting/refactor changes.
- Blast-radius view identifies direct callers for Python and TypeScript functions.
- A seeded behavior regression appears in the top three findings.

## Stage 5: Concept Index and Policy Gates

### Goal

Turn team review rules into enforceable policy.

### Incremental Value

Security and platform teams can block dangerous concept combinations automatically.

### Scope

Add:

- Concept taxonomy v1.
- Incremental concept classification for changed functions and direct callers.
- Policy pack parser and validator.
- Policy evaluation engine.
- Policy owner and override workflow.
- `Concept Policy` check.
- `mergeguard/policy-blocked` label.
- Audit export v1 for policy findings.

### Acceptance Criteria

- Policy `pii-write requires auth-check` can be configured in YAML.
- PR introducing PII write without auth check produces a blocking finding in blocking mode.
- Policy override requires owner and reason.
- Audit export includes rule, location, evidence, decision, and override trail.

## Stage 6: Prompt Canary Gate

### Goal

Cover PRs where the changed artifact is a prompt, model config, or agent workflow.

### Incremental Value

Teams can prevent prompt/model regressions before merge.

### Scope

Add:

- Prompt suite schema.
- Prompt/model changed-path detector.
- Runner for golden prompt suites.
- Assertions: exact match, regex, JSON schema, rubric, latency, cost.
- Before/after output diff.
- Drift scoring for correctness, format, style, refusal, latency, and cost.
- `Prompt Canary` check.
- `mergeguard/prompt-drift` label.

### Acceptance Criteria

- Changing a prompt path runs canaries.
- Invalid JSON output fails format check.
- Canary output is visible in dashboard and linked from PR comment.
- Canary pass/fail can be advisory or blocking by policy.

## Stage 7: Runtime Contracts and Generated Tests

### Goal

Expose implicit assumptions that code review and types usually miss.

### Incremental Value

Teams can catch contract drift and generate targeted tests for fragile behavior.

### Scope

Add:

- Contract summary ingestion from staging/shadow traffic.
- Shape-only capture schema.
- Contract comparator for changed functions.
- Contract drift findings.
- Suggested property tests for expressible contracts.
- Optional generated test PR or patch artifact.
- `Runtime Contracts` check.

### Acceptance Criteria

- Contract summaries can be attached to functions.
- Changed function with incompatible contract produces a finding.
- Suggested test includes file path, framework, and test intent.
- No raw PII values are stored.

## Stage 8: Learning Loop, Team Calibration, and Audit

### Goal

Make MergeGuard improve with reviewer behavior and become trustworthy as a required check.

### Incremental Value

Teams can tune false positives, promote high-precision checks to blocking, and export review evidence.

### Scope

Add:

- Override capture and reason taxonomy.
- Per-team threshold configuration.
- False-positive tracking.
- Post-merge outcome ingestion: revert, incident tag, hotfix label.
- Trend dashboards.
- Audit export for PR, policy, evidence, overrides, and check outcomes.
- Admin UI for policy pack editing and check mode changes.

### Acceptance Criteria

- Reviewer override is recorded and visible in history.
- Admin can switch checks from advisory to blocking.
- Team can see top false-positive finding categories.
- Audit export reconstructs the merge decision for a PR.

## Stage 9: Production Hardening and Enterprise Readiness

### Goal

Prepare for multi-team, multi-repo production use.

### Incremental Value

The system becomes reliable, secure, scalable, and maintainable enough for broad rollout.

### Scope

Add:

- Multi-tenant organization isolation.
- Role-based access control.
- SSO/SAML where needed.
- Rate limit handling for GitHub API.
- Worker autoscaling.
- Analyzer sandbox isolation.
- Secrets management.
- Data retention controls.
- Privacy scrubber for traces and artifacts.
- Observability: metrics, logs, traces, alerts.
- Disaster recovery and backup.

### Acceptance Criteria

- GitHub rate limits do not break analysis; jobs retry gracefully.
- Analysis failures are visible and do not leave stale blocking checks indefinitely.
- Tenant data is isolated.
- Security review approves permissions and data retention model.
- SLOs are met for pilot repositories.

## 11. API Requirements

### 11.1 Internal API Endpoints

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/webhooks/github` | POST | Receive GitHub webhook events. |
| `/api/installations` | GET | List installations visible to current user. |
| `/api/repos` | GET | List enabled repos. |
| `/api/prs` | GET | Queue dashboard query. |
| `/api/prs/:id` | GET | PR detail view. |
| `/api/prs/:id/rerun` | POST | Re-run analysis. |
| `/api/runs/:id` | GET | Analysis run details. |
| `/api/findings/:id/override` | POST | Override finding with reason. |
| `/api/policy-packs` | GET/POST | List and create policy packs. |
| `/api/policy-packs/:id/activate` | POST | Activate policy version. |
| `/api/audit/pr/:id` | GET | Export PR audit report. |

### 11.2 Webhook Idempotency

Every webhook event must be processed idempotently using:

- GitHub delivery id.
- Repository id.
- PR number.
- Head SHA.
- Event action.

Duplicate webhook deliveries must not create duplicate analysis runs unless the user manually requests rerun.

## 12. Security and Privacy Requirements

- Validate GitHub webhook signatures.
- Store GitHub App private key in managed secrets.
- Use installation tokens with least privilege.
- Do not store raw source code permanently unless explicitly configured.
- Store analysis artifacts with retention policy.
- For runtime contracts, record shapes and invariants, not raw values.
- Redact secrets from logs, prompts, traces, and generated reports.
- Require owner reason for blocking overrides.
- Maintain audit history for check result changes.
- Separate tenant data by organization id in every query.

## 13. Observability Requirements

Metrics:

- Webhook received count.
- Analysis queued/running/completed/failed count.
- Analysis duration p50/p95/p99.
- GitHub API rate limit remaining.
- Check update failures.
- PR comment update failures.
- Worker retry count.
- Finding counts by severity/category.
- Override rate by category.

Logs:

- Webhook delivery id.
- Analysis run id.
- Worker job id.
- GitHub API request id when available.
- Policy pack version.

Alerts:

- Analysis failure rate above threshold.
- PR checks stale for more than configured time.
- GitHub API rate limit exhaustion.
- Worker queue backlog above threshold.
- Dashboard/API availability below SLO.

## 14. Testing Strategy

### Unit Tests

- Risk scorer.
- File classifier.
- Policy parser.
- Policy evaluator.
- PR comment renderer.
- Check result mapper.
- Label manager.
- Webhook signature validator.

### Integration Tests

- GitHub webhook fixture to analysis run.
- Analysis run to check result.
- Analysis run to sticky comment update.
- Policy pack to blocking finding.
- Prompt canary fixture to prompt drift finding.

### End-to-End Tests

- Open PR in test repository.
- Confirm dashboard row appears.
- Confirm check run appears.
- Confirm PR comment appears.
- Push new commit and confirm existing comment updates.
- Confirm labels update with state changes.

### Evaluation Tests

- Seeded risky PR corpus.
- Docs-only PR corpus.
- Generated-file heavy PR corpus.
- Prompt regression corpus.
- Policy violation corpus.

## 15. Rollout Plan

1. Internal development repo in advisory mode.
2. One pilot repo with Stage 1 MVP.
3. Three pilot repos after Stage 2.
4. Security-sensitive repo after Stage 5 policy gates.
5. Prompt-heavy repo after Stage 6.
6. Promote selected checks to required only after false-positive review.
7. Expand org-wide with documented policy packs and override process.

## 16. Definition of Done by Stage

| Stage | Done When |
| --- | --- |
| 1 | A real PR produces dashboard entry, PR comment, check run, labels, and basic risk score. |
| 2 | Reviewers can use the dashboard to route attention to must-inspect files. |
| 3 | Intent and missing evidence are visible and actionable. |
| 4 | Behavior-level deltas and blast radius are visible for Python/TypeScript changes. |
| 5 | Team policy rules can block or warn with owner override. |
| 6 | Prompt/model changes run canaries and surface drift. |
| 7 | Runtime contract changes produce findings and suggested tests. |
| 8 | Overrides, outcomes, audit exports, and threshold tuning are available. |
| 9 | System is secure, observable, reliable, and ready for multi-team rollout. |

## 17. Open Engineering Decisions

- Whether Stage 1 should use GitHub API file contents only or full repo checkout.
- Whether LLM calls should run in the platform service or in customer-controlled GitHub Actions.
- Whether prompt canary runs should execute in MergeGuard infrastructure or repository CI.
- How much source code can be stored, and for how long.
- Whether policy packs live in the dashboard, the repository, or both.
- Whether generated tests open a PR automatically or produce downloadable patches first.
- Which LLM provider and model policy to use for intent/semantic stages.
- How to price analysis for very large monorepos.

## 18. Recommended First Sprint Backlog

1. Create GitHub App and local webhook development flow.
2. Create database schema for Stage 1 entities.
3. Implement PR ingestion and changed file fetch.
4. Implement analysis job queue.
5. Implement heuristic file classifier and risk scorer.
6. Implement queue dashboard.
7. Implement PR detail shell.
8. Implement sticky PR comment.
9. Implement check-run update.
10. Implement labels.
11. Add fixture-based tests.
12. Run E2E against a test repository.

