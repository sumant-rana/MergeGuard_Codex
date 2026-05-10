# Stage-Wise Implementation

This document maps the codebase to the staged plan in `mergeguard_implementation_spec.md`.

## Stage 1: Thin End-To-End MVP

Status: implemented.

Delivered:

- Monorepo layout with `apps/api`, `apps/web`, `packages/shared`, and `workers/analyzer`.
- GitHub webhook receiver at `/webhooks/github`.
- Webhook SHA-256 signature verification.
- Webhook idempotency by delivery id, repository id, PR number, head SHA, and action.
- Pull request ingestion for `opened`, `synchronize`, `reopened`, `ready_for_review`, and `edited`.
- Changed-file fetch through GitHub REST when credentials are configured.
- Fixture changed-file ingestion for local development.
- Heuristic review compression, missing evidence, risk score, merge readiness, queue dashboard, PR detail dashboard, sticky PR comment, check runs, labels, JSON store, and Postgres schema.

## Stage 2: Review Compression Dashboard

Status: implemented as deterministic local analysis.

Delivered:

- CODEOWNERS parsing and owner assignment in `packages/shared/src/codeowners.js`.
- Lightweight Python/TypeScript/JavaScript symbol extraction in `packages/shared/src/symbols.js`.
- Hotspot theme aggregation, owner summary, and review bottlenecks.
- Dashboard owner/theme regions and owner-aware queue filtering.
- Generated/docs/tests/wiring/logic/security-sensitive/prompt file grouping.

## Stage 3: Intent And Evidence Mapping

Status: implemented as PR title/body intent extraction with heuristic evidence mapping.

Delivered:

- `should`, `must_not`, and `out_of_scope` intent extraction in `packages/shared/src/intent.js`.
- Intent-to-path matching.
- Evidence status: `proven`, `partial`, `missing`.
- Unexpected risky scope detection.
- Suggested tests for missing intent evidence.
- `Intent Match` and `Evidence Coverage` check results.
- `mergeguard/intent-drift` label support.

## Stage 4: Semantic Diff And Blast Radius

Status: implemented as lightweight symbol, behavior, divergent-example, and blast-radius analysis.

Delivered:

- Changed symbol extraction from patches or fixture content.
- Behavior delta summaries with old behavior, new behavior, severity, confidence, category, and divergent examples.
- Blast-radius summaries with direct callers, downstream services, owners, and impacted tests.
- `Behavioral Diff` check result.
- PR detail regions for behavioral diff and blast radius.

## Stage 5: Concept Index And Policy Gates

Status: implemented with a concept taxonomy, default policy pack, YAML policy packs, policy checks, and audit output.

Delivered:

- Concept taxonomy in `packages/shared/src/concepts.js`.
- Policy YAML parser and evaluator in `packages/shared/src/policy.js`.
- Default policy pack for PII, billing, external HTTP, and agent workflow risks.
- Policy pack API: `GET/POST /api/policy-packs`.
- Policy activation API: `POST /api/policy-packs/:id/activate`.
- Concept policy findings, policy owners, and suggested actions.
- `Concept Policy` check result.
- `mergeguard/policy-blocked` label support.
- Audit export includes policies and policy findings.

## Stage 6: Prompt Canary Gate

Status: implemented with deterministic prompt canary scoring and prompt-drift check results.

Delivered:

- Prompt/model changed-path detector.
- Prompt suite schema support through fixture payloads.
- Heuristic assertions for safety, JSON format, correctness, style, latency, and cost.
- Before/after fields in the canary run model.
- `Prompt Canary` check result.
- `mergeguard/prompt-drift` label support.
- PR detail region for prompt canary runs.

## Stage 7: Runtime Contracts And Generated Tests

Status: implemented with shape-only contract comparison and suggested test artifacts.

Delivered:

- Shape-only contract summaries in fixture payloads.
- Contract comparator in `packages/shared/src/contracts.js`.
- Contract drift findings for removed fields and changed types.
- Suggested property/contract tests with path, framework, and test intent.
- `Runtime Contracts` check result.
- PR detail region for runtime contracts and suggested tests.

## Stage 8: Learning Loop, Team Calibration, And Audit

Status: implemented with override capture, post-merge outcomes, recommendations, metrics, and audit reconstruction.

Delivered:

- Reviewer override endpoint: `POST /api/findings/:id/override`.
- Post-merge outcome endpoint: `POST /api/outcomes`.
- Learning recommendations in analysis summary.
- Override rate and finding counts in metrics.
- Audit export reconstruction with latest status, top blocker, checks, labels, overrides, outcomes, and policy packs.

## Stage 9: Production Hardening And Enterprise Readiness

Status: implemented as production-readiness surfaces and migration seams, with remaining work isolated to deployment adapters.

Delivered:

- Multi-entity organization/repository/PR/run model.
- Postgres migrations for all major Stage 1-9 entities.
- GitHub API rate-limit retry.
- Metrics endpoint with webhook count, analysis counts, duration percentiles, finding counts, override rate, queue state, and local alerts.
- Local JSON development store with a clear Postgres migration path.
- Raw patch/source is used during analysis but not persisted in stored changed-file artifacts.

Remaining production adapters:

- Replace JSON store with Postgres.
- Move analysis execution behind Redis/BullMQ or managed queue workers.
- Add dashboard authentication, SSO, RBAC, and tenant-scoped authorization.
- Wire logs, traces, and alerts to the chosen deployment platform.
- Add managed secrets and retention controls in the hosting environment.
