# Run The Agentic Demo

## Prerequisites

- Python 3.11+.
- No Python package install is required for the demo path.
- Latest Magenta CLI binary is already downloaded at `./tools/bin/agentic`.

## Validate Magenta Manifests

```sh
make magenta-version
make magenta-validate
```

The agent code is Magenta-compatible. In an agent container with `magenta_sdklanggraph` installed, each exported `app` is a real Magenta SDK `App`; local tests use the shim only because this repo's demo path avoids Python dependency installation.

Deployment-specific commands and the platform payload contract are in:

```text
docs/MAGENTA_DEPLOYMENT.md
```

## Run The Full Agent Pipeline From The Worker

```sh
python3 apps/worker/main.py
```

This reads:

```text
fixtures/agentic/demo_pr.json
```

It writes local demo state to:

```text
data/agentic_mergeguard.json
```

Expected result:

- 9 agent results.
- Risk score `100`.
- Status `blocked`.
- 7 synthesized check results.
- Findings from policy, prompt canary, runtime contracts, missing evidence, and behavioral diff.

## Start The Dashboard

```sh
python3 apps/api/main.py
```

Open:

```text
http://127.0.0.1:4100
```

Click `Run Demo PR`. The dashboard will:

- Invoke all agents through the local orchestration layer.
- Persist the analysis run.
- Show queue metrics.
- Render independent agent results, hotspots, intent/evidence, behavioral diff, policy gates, prompt canaries, runtime contracts, checks, and generated PR comment.

## API Smoke Commands

Run an analysis:

```sh
curl -s -X POST http://127.0.0.1:4100/api/demo/analyze \
  -H 'content-type: application/json' \
  -d '{}'
```

View queue:

```sh
curl -s http://127.0.0.1:4100/api/queue
```

View metrics:

```sh
curl -s http://127.0.0.1:4100/api/metrics
```

View a run:

```sh
curl -s http://127.0.0.1:4100/api/runs/RUN_ID
```

Analyze a real PR payload collected by the helper script:

```sh
scripts/mergeguard_pr.py analyze --repo /absolute/path/to/target-repo --pr 123
```

Or post a prepared payload directly:

```sh
curl -s -X POST http://127.0.0.1:4100/api/github/pr/analyze \
  -H 'content-type: application/json' \
  --data-binary @/tmp/mergeguard-pr.json
```

## Test

Agentic tests:

```sh
make test-agentic
```

Legacy Node prototype tests:

```sh
npm test
```

Manifest validation:

```sh
make magenta-validate
```

Representative workspace lockfile check:

```sh
uv sync --locked --no-dev --package mergeguard-agent-review-compression --dry-run
```

## Build On Magenta

After authenticating and initializing the workspace with the Agentic Platform:

```sh
./tools/bin/agentic auth login
./tools/bin/agentic init
./tools/bin/agentic build --workspace review-compression
./tools/bin/agentic deploy --workspace review-compression
```

Repeat per workspace, or build all:

```sh
./tools/bin/agentic build --all
```

Deployed agents accept a JSON message whose content is either the direct agent envelope or `{"payload": <agent-envelope>}`. See `docs/MAGENTA_DEPLOYMENT.md` for an `agentic invoke --file` example.

## Demo Fixture Shape

`fixtures/agentic/demo_pr.json` includes:

- repository metadata
- PR title/body
- changed files with patches
- CODEOWNERS text
- prompt canary suite
- shape-only runtime contract summaries

That lets the tool exercise every agent without installing a GitHub App.
