# Magenta Deployment Notes

MergeGuard now has a deployable Magenta-style agent per feature. The local dashboard keeps using the dependency-free shim, but the same `agents/<name>/src/<module>/main.py` files create a real `magenta_sdklanggraph.App` when they run in an Agentic Platform container.

## What Gets Deployed

Root workspace manifest:

```sh
agent.yaml
```

Per-agent deployable workspaces:

```text
agents/review-compression
agents/intent-extractor
agents/evidence-mapper
agents/semantic-diff-explainer
agents/concept-classifier
agents/slop-detector
agents/policy-gate
agents/prompt-canary
agents/contract-comparator
agents/semantic-evidence-agent
agents/test-coverage-validator
agents/truth-report-synthesizer
```

The root `pyproject.toml` declares the agents and shared `packages/` folder as a uv workspace. That matters because the Magenta CLI production Dockerfile copies only workspace top-level directories. The checked-in `uv.lock` is required by the generated production Dockerfile.

## Validate Locally

```sh
make magenta-version
make magenta-validate
uv sync --locked --no-dev --package mergeguard-agent-review-compression --dry-run
make test-agentic
```

The `uv sync --dry-run` command verifies that the lockfile and a representative agent package resolve without changing the local environment.

## Run The Local Demo

Run the full agent chain once:

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

Click `Run Demo PR`. The tool invokes the twelve independent agents, stores the OE-shaped execution envelopes, and renders the dashboard.

## Build And Deploy On Magenta

Authenticate and initialize the monorepo once:

```sh
./tools/bin/agentic auth login
./tools/bin/agentic init
```

Build and deploy one workspace:

```sh
./tools/bin/agentic build --workspace review-compression
./tools/bin/agentic deploy --workspace review-compression
```

Build all workspaces:

```sh
./tools/bin/agentic build --all
```

Deploy each workspace after its build completes:

```sh
./tools/bin/agentic deploy --workspace intent-extractor
./tools/bin/agentic deploy --workspace evidence-mapper
./tools/bin/agentic deploy --workspace semantic-diff-explainer
./tools/bin/agentic deploy --workspace concept-classifier
./tools/bin/agentic deploy --workspace slop-detector
./tools/bin/agentic deploy --workspace policy-gate
./tools/bin/agentic deploy --workspace prompt-canary
./tools/bin/agentic deploy --workspace contract-comparator
./tools/bin/agentic deploy --workspace semantic-evidence-agent
./tools/bin/agentic deploy --workspace test-coverage-validator
./tools/bin/agentic deploy --workspace truth-report-synthesizer
```

For repository memory retrieval, run Atlas setup and sync secrets before deploying memory-enabled agents:

```sh
./tools/bin/agentic atlas setup --context prod
./tools/bin/agentic secret sync --context prod
```

The `semantic-evidence-agent` has `features.memory: true`, so deployed runs use Magenta memory, Voyage embeddings, and Atlas Vector Search through `app.memory`. Local demo runs use the deterministic memory shim documented in [Semantic Memory](SEMANTIC_MEMORY.md).

## Platform Payload Contract

The deployed agents accept a JSON message. The message content should be either the direct agent envelope or an object with a `payload` field:

```json
{
  "payload": {
    "analysis_run_id": "run-123",
    "pull_request": {},
    "changed_files": [],
    "prior_results": {},
    "settings": {}
  }
}
```

Invoke a deployed agent with a fixture payload:

```sh
python3 - <<'PY' > /tmp/mergeguard-agent-payload.json
import json
from pathlib import Path

fixture = json.loads(Path("fixtures/agentic/demo_pr.json").read_text())
print(json.dumps({
    "payload": {
        "analysis_run_id": "manual-run",
        "pull_request": fixture["pull_request"],
        "changed_files": fixture["changed_files"],
        "prior_results": {},
        "settings": fixture["settings"],
    }
}))
PY

./tools/bin/agentic invoke \
  --workspace review-compression \
  --file /tmp/mergeguard-agent-payload.json \
  --json
```

The response body is a JSON string containing the same `AgentResult` shape the local dashboard consumes.

## Demo Scope

This demo intentionally avoids real GitHub App installation, webhook delivery, and live GitHub changed-file fetching. The deployable unit is the agent. The dashboard/orchestrator is the tool layer that sends envelopes to agents, stores their outputs, and presents the combined truth report.
