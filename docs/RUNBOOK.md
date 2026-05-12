# Runbook

## Local Development

Create `.env`:

```sh
cp .env.example .env
```

Start the app:

```sh
npm run dev
```

Seed demo PRs:

```sh
npm run seed
```

Open:

```text
http://localhost:4000
```

The risky demo PR exercises all implemented gates:

- Review compression and CODEOWNERS.
- Intent/evidence mapping.
- Behavioral diff and blast radius.
- Concept policy.
- Prompt canary.
- Runtime contract comparison.
- Generated test suggestions.

## Verify A Webhook By Hand

```sh
npm run simulate:webhook
```

Expected result:

- HTTP `202 Accepted`.
- A new or duplicate analysis run response.
- Dashboard queue row for PR `#42`.

## Run Tests

```sh
npm test
```

Expected result:

- Classifier tests pass.
- Signature tests pass.
- Sticky comment test passes.
- Webhook pipeline idempotency test passes.
- Advanced analyzer tests pass.

## Reset Local Data

Stop the server, then remove the local data file:

```sh
rm -f data/mergeguard.json
```

Start the server again to recreate an empty store.

## Live GitHub Smoke Test

1. Configure `.env` with a public URL, webhook secret, and GitHub credentials.
2. Install the GitHub App on a sandbox repository.
3. Open a PR with one risky source file change and no test change.
4. Confirm:
   - The dashboard row appears.
   - The PR has a `MergeGuard Change Triage` check run.
   - The PR has advisory gate checks for evidence, intent, behavioral diff, concept policy, prompt canary, and runtime contracts.
   - One sticky MergeGuard comment appears.
   - `mergeguard/missing-evidence` is applied.
5. Push another commit to the PR.
6. Confirm:
   - A new analysis run appears.
   - The existing sticky comment is updated rather than duplicated.

## Operational Notes

- `MERGEGUARD_CHECK_MODE=advisory` is recommended until the team has reviewed false positives.
- `MERGEGUARD_ALLOW_FIXTURE_FILES=false` should be used outside local development.
- `MERGEGUARD_DATA_FILE` should point to persistent storage when running outside a laptop.
- GitHub App permissions must include pull requests, checks, issues, contents, statuses, and metadata as shown in `github-app-manifest.json`.

## Metrics And Audit

Metrics:

```sh
curl -s http://localhost:4000/api/metrics
```

Policy packs:

```sh
curl -s http://localhost:4000/api/policy-packs
```

Audit export:

```sh
curl -s http://localhost:4000/api/audit/pr/PR_ID
```
