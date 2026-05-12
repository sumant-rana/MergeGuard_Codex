# Advanced Feature Guide

The implementation covers every stage from the spec as a local-first ReviewOps prototype. Later stages use deterministic heuristics and structured fixtures so teams can validate workflow shape before replacing individual engines with deeper analyzers.

## Change Triage

Inputs:

- Changed files from GitHub REST or fixture payloads.
- Optional `mergeguard.codeowners`.
- Optional patch text from GitHub.

Outputs:

- `changed_files[].symbols`
- `changed_files[].owner`
- `summary.hotspot_themes`
- `summary.owner_summary`
- `summary.review_bottlenecks`

CODEOWNERS fixture example:

```text
payments/ @payments-team
auth/ @identity-team
prompts/ @ai-platform
api/ @platform-team
*.md @docs-team
```

## Intent And Evidence

Intent is extracted from PR title and body. Items are classified as:

- `should`
- `must_not`
- `out_of_scope`

Each item receives `mapped_paths`, `evidence_status`, and `suggested_test`. The dashboard shows these in the "Intent Vs Implementation" region.

## Semantic Diff And Blast Radius

The analyzer extracts lightweight Python/TypeScript/JavaScript symbols from patch or fixture content. It then creates behavior deltas, divergent examples, downstream services, direct callers, and impacted test suggestions.

This is deterministic and path/patch based. It is meant to be replaced incrementally by language-server or AST-backed workers.

## Concept Policy Gates

Concept taxonomy:

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

Create a custom policy pack:

```sh
curl -s http://localhost:4000/api/policy-packs \
  -H 'content-type: application/json' \
  -d '{"name":"Custom","active":true,"yaml":"name: Custom\nversion: 1\nrules:\n  - id: pii-auth\n    when: pii-write\n    require: auth-check\n    severity: block\n    owner: \"@security\"\n"}'
```

Activate a policy:

```sh
curl -X POST http://localhost:4000/api/policy-packs/POLICY_ID/activate
```

## Prompt Canary Gate

Prompt paths are detected under `prompts/`, `prompt/`, `.prompt`, `.prompt.md`, `.jinja`, and `.tmpl`.

Fixture canary schema:

```json
{
  "name": "refund-agent-golden",
  "prompt_path": "prompts/refund-agent.prompt.md",
  "model": "gpt-repo-default",
  "assertions": {
    "format": "json",
    "safety": "no instruction bypass"
  },
  "thresholds": {
    "correctness": 0.75,
    "format": 0.8,
    "style": 0.65,
    "latency_delta_ms": 750,
    "cost_delta_pct": 35
  }
}
```

Outputs include `prompt_canary_runs`, `summary.prompt_findings`, the `Prompt Canary` check result, and `mergeguard/prompt-drift` when failing.

## Runtime Contracts

Contract fixtures compare shape-only summaries:

```json
{
  "path": "api/refund_response.ts",
  "symbol": "RefundResponse",
  "old": {
    "id": "string",
    "receiptUrl": "string"
  },
  "new": {
    "id": "string"
  },
  "framework": "vitest"
}
```

Outputs include `contract_findings`, `summary.suggested_tests`, and the `Runtime Contracts` check result. Raw PII values are not required or stored by this flow.

## Learning Loop

Record an override:

```sh
curl -s http://localhost:4000/api/findings/FINDING_ID/override \
  -H 'content-type: application/json' \
  -d '{"run_id":"RUN_ID","reviewer":"octocat","reason":"Accepted because production canary already covered this path."}'
```

Record a post-merge outcome:

```sh
curl -s http://localhost:4000/api/outcomes \
  -H 'content-type: application/json' \
  -d '{"pr_id":"PR_ID","outcome_type":"revert","label":"hotfix","notes":"Regression in refund response consumer."}'
```

The audit export includes overrides and outcomes:

```sh
curl -s http://localhost:4000/api/audit/pr/PR_ID
```

## Production Hardening Surfaces

Implemented surfaces:

- Multi-entity organization/repository/PR data model.
- GitHub rate-limit retry for API calls.
- Metrics endpoint with analysis duration, queue state, finding counts, override rate, and local alerts.
- Postgres migrations for all major entities.
- Local JSON store for development.
- Raw patch/source is used during analysis but not persisted in `changed_files`.

Metrics:

```sh
curl -s http://localhost:4000/api/metrics
```

Production next steps are adapter swaps rather than API redesigns: replace JSON with Postgres, move analyzers behind a queue worker, add dashboard auth/RBAC, and wire logs/traces/alerts to the deployment platform.
