# MergeGuard Command Center

MergeGuard Command Center is now implemented as an agentic demo architecture: each ReviewOps feature is a separate Magenta-style agent, and the dashboard/tool orchestrates those agents end to end without requiring GitHub App installation.

The older Node prototype remains in the repository for comparison, but the current implementation path is Python-based and documented here:

- [Agentic Architecture](docs/AGENTIC_ARCHITECTURE.md)
- [Run The Agentic Demo](docs/RUN_AGENTIC_DEMO.md)
- [Magenta Deployment Notes](docs/MAGENTA_DEPLOYMENT.md)
- [Semantic Memory](docs/SEMANTIC_MEMORY.md)
- [Local GitHub PR Workflow](docs/LOCAL_GITHUB_PR_WORKFLOW.md)

Each feature agent now exposes a Magenta-compatible `app`: it uses `magenta_sdklanggraph.App` in a Magenta runtime and falls back to the local shim only for dependency-free demo tests.
Deployed agents accept the same analysis envelope as a JSON `agentic invoke` message and return the structured `AgentResult` JSON consumed by the dashboard.

The current implementation is intentionally dependency-light so it can run from a clean workspace:

- GitHub webhook receiver with SHA-256 signature verification.
- Idempotent pull request ingestion.
- Heuristic changed-file classification and risk scoring.
- Missing-test evidence signal.
- Risk-sorted queue dashboard and PR detail dashboard.
- Sticky PR comment renderer.
- GitHub check run and label sync when GitHub credentials are configured.
- Local fixture mode for development and smoke testing without a live GitHub App.
- CODEOWNERS owner assignment, symbol extraction, hotspot themes, and reviewer routing.
- Intent extraction and evidence mapping from PR title/body.
- Semantic behavior deltas and blast-radius summaries.
- Concept taxonomy and YAML policy pack evaluation.
- Prompt canary scoring for prompt/model/agent changes.
- Runtime contract comparison and generated test suggestions.
- Repository memory retrieval through the `semantic-evidence-agent`, using Magenta memory in deployment and a local deterministic fallback in demo mode.
- Reviewer override capture, post-merge outcome capture, metrics, and audit export.

## What Is Implemented

All stages are implemented as a runnable local monorepo:

| Spec Area | Implementation |
| --- | --- |
| `apps/api` | Node HTTP API, GitHub webhook route, JSON store, GitHub REST client, static dashboard hosting. |
| `apps/web` | Dependency-free browser dashboard served by the API. |
| `packages/shared` | File classifier, risk scorer, labels, PR comment renderer, webhook signature helper, policy engine, intent mapper, canary runner, contract comparator. |
| `workers/analyzer` | Analyzer worker that composes Stage 1-8 analysis engines. |
| `fixtures` | Webhook and changed-file fixtures for local analysis. |
| `tests` | Node test runner coverage for classifier, risk flow, sticky comments, signatures, and webhook idempotency. |

Stage coverage and extension points are documented in [docs/STAGE_IMPLEMENTATION.md](docs/STAGE_IMPLEMENTATION.md). Advanced feature behavior is documented in [docs/ADVANCED_FEATURES.md](docs/ADVANCED_FEATURES.md).

## Quick Agentic Demo

Validate the latest Magenta CLI and manifests:

```sh
make magenta-version
make magenta-validate
```

Run the full pipeline once:

```sh
python3 apps/worker/main.py
```

Start the dashboard:

```sh
python3 apps/api/main.py
```

Open:

```text
http://127.0.0.1:4100
```

Run tests:

```sh
make test-agentic
```

Analyze a real GitHub PR from a local checkout:

```sh
scripts/mergeguard_pr.py analyze --repo /absolute/path/to/target-repo --pr 123
```

Or create a PR and immediately process it:

```sh
scripts/mergeguard_pr.py create --repo /absolute/path/to/target-repo --base main --title "Fix issue" --body "Fixes #123"
```

## Requirements

- Node.js 20 or newer. This workspace was verified with Node 25.
- npm, only for running scripts. The implementation has no third-party package install step.
- Optional: a GitHub App or GitHub token if you want live PR comments, check runs, labels, and changed-file fetching.

## Quick Start With Fixture Data

1. Create a local environment file:

   ```sh
   cp .env.example .env
   ```

2. Start MergeGuard:

   ```sh
   npm run dev
   ```

3. In a second terminal, seed two demo PRs:

   ```sh
   npm run seed
   ```

4. Open the dashboard:

   ```text
   http://localhost:4000
   ```

You should see one risky payment/auth/prompt/API-contract PR and one docs-only PR. The risky PR should sort higher and show missing evidence, CODEOWNERS ownership, intent drift, behavior summaries, prompt canary failure, runtime contract drift, and generated test suggestions.

## Run The Tests

```sh
npm test
```

The tests use Node's built-in test runner and local temporary files. No database or GitHub network access is required.

## Simulate A GitHub Webhook

With the server running:

```sh
npm run simulate:webhook
```

You can pass a different fixture path:

```sh
npm run simulate:webhook -- fixtures/webhooks/pull_request_opened.json
```

The simulator signs the request with `GITHUB_WEBHOOK_SECRET`, defaulting to `change-me`, which matches `.env.example`.

## Real GitHub App Setup

1. Create a GitHub App using the values in `github-app-manifest.json`.
2. Set the webhook URL to:

   ```text
   https://your-public-url/webhooks/github
   ```

3. Configure the webhook secret and copy it into `.env`:

   ```sh
   GITHUB_WEBHOOK_SECRET=your-secret
   ```

4. Set one authentication mode:

   GitHub App mode:

   ```sh
   GITHUB_APP_ID=123456
   GITHUB_APP_PRIVATE_KEY_PATH=/absolute/path/to/private-key.pem
   ```

   Or token mode for sandbox testing:

   ```sh
   GITHUB_TOKEN=github_pat_or_test_token
   ```

5. Set the public URL used in dashboard links:

   ```sh
   MERGEGUARD_PUBLIC_URL=https://your-public-url
   ```

6. Start the server:

   ```sh
   npm run dev
   ```

When a supported PR webhook arrives, MergeGuard fetches changed files from GitHub, analyzes the PR, writes a dashboard entry, creates advisory gate check runs, updates one sticky PR comment, and syncs MergeGuard labels.

## Check Modes

MergeGuard defaults to advisory mode:

```sh
MERGEGUARD_CHECK_MODE=advisory
```

In advisory mode, review-needed PRs create a neutral check. Analysis failures fail the check.

Blocking mode can be enabled:

```sh
MERGEGUARD_CHECK_MODE=blocking
```

In blocking mode, severe missing evidence, blocking concept policies, prompt canary failures, and runtime contract blockers can produce failing checks. Teams should only use this after validating false positives on pilot repos.

## API Routes

| Route | Method | Purpose |
| --- | --- | --- |
| `/health` | `GET` | Service health. |
| `/webhooks/github` | `POST` | GitHub webhook receiver. |
| `/api/installations` | `GET` | Installation groups from stored repositories. |
| `/api/repos` | `GET` | Enabled repositories in the local store. |
| `/api/prs` | `GET` | Risk-sorted queue. Supports `repo`, `owner`, `risk_state`, and `label` query parameters. |
| `/api/prs/:id` | `GET` | PR detail payload. |
| `/api/prs/:id/rerun` | `POST` | Re-run analysis using the last known changed files. |
| `/api/runs/:id` | `GET` | Analysis run detail. |
| `/api/policy-packs` | `GET/POST` | List or create YAML policy packs. |
| `/api/policy-packs/:id/activate` | `POST` | Activate a policy pack for its repo scope. |
| `/api/findings/:id/override` | `POST` | Record reviewer override with required reason. |
| `/api/outcomes` | `POST` | Record post-merge outcome such as revert, incident, or hotfix. |
| `/api/metrics` | `GET` | Operational metrics and local alerts. |
| `/api/audit/pr/:id` | `GET` | Audit JSON export for a PR. |

## Data Storage

By default the app writes local state to:

```text
./data/mergeguard.json
```

Override it with:

```sh
MERGEGUARD_DATA_FILE=/path/to/mergeguard.json
```

Postgres schema baselines are available at `apps/api/migrations/001_stage1.sql` and `apps/api/migrations/002_stages_2_to_9.sql` for teams that want to replace the local JSON store with a database adapter.

## Labels Managed

- `mergeguard/high-risk`
- `mergeguard/missing-evidence`
- `mergeguard/safe-to-skim`
- `mergeguard/analysis-failed`
- `mergeguard/intent-drift`
- `mergeguard/policy-blocked`
- `mergeguard/prompt-drift`
- `mergeguard/override`

## Troubleshooting

`401 missing x-hub-signature-256 header`

The webhook route requires a valid signature when `GITHUB_WEBHOOK_SECRET` is set. Use `scripts/simulate-webhook.js` or configure the same secret in GitHub.

`GitHub API credentials are not configured and the webhook did not include mergeguard.changed_files`

Real GitHub webhooks do not include changed files. Configure GitHub credentials or use fixture mode with `mergeguard.changed_files`.

`The dashboard is empty`

Seed local data with `npm run seed`, or trigger a real pull request webhook.

`GitHub check/comment/label sync is skipped`

Set `GITHUB_TOKEN` or GitHub App credentials. Without credentials, analysis still works locally and records the skipped sync in the run payload.
