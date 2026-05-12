# MergeGuard Agentic Architecture

This implementation follows the staged Magenta spec in demo-first form:

- Each MergeGuard feature is a separate agent under `agents/<agent-name>/`.
- Each agent has its own `agent.yaml`, `pyproject.toml`, and `src/<module>/main.py`.
- Each agent creates a real `magenta_sdklanggraph.App` when the Magenta SDK is installed.
- The root uv workspace includes `agents/` and `packages/`, and `uv.lock` is checked in for Magenta production image generation.
- The local fallback shim is used only for dependency-free dashboard and unit-test runs.
- The dashboard/tool invokes agents through an orchestration layer.
- In production, the worker swaps `LocalPlatformClient` for Magenta Orchestration Engine `/invoke`.
- In demo mode, `LocalPlatformClient` loads each deployable agent entrypoint locally and records OE-shaped executions.

## Latest Magenta CLI

The local source checkouts were refreshed:

- `/Users/sumant.rana/Sumant/workspace/test-code/magenta-client-libraries`
- `/Users/sumant.rana/Sumant/workspace/test-code/magenta-examples`

The latest released CLI binary is installed locally at:

```sh
./tools/bin/agentic
```

Verified version:

```sh
make magenta-version
```

Expected version:

```text
0.1.28-alpha
```

Validate all manifests:

```sh
make magenta-validate
```

## Agents

| Agent | Responsibility | Deployable Path |
| --- | --- | --- |
| `review-compression` | Classifies changed files, assigns owners, ranks hotspots, partitions must-inspect vs safe-to-skim. | `agents/review-compression` |
| `intent-extractor` | Extracts `should`, `must_not`, and `out_of_scope` intent items from PR text. | `agents/intent-extractor` |
| `evidence-mapper` | Maps intent and risky changes to tests/evidence or HITL author-preview questions. | `agents/evidence-mapper` |
| `semantic-diff-explainer` | Produces behavior deltas, divergent examples, blast-radius summaries. | `agents/semantic-diff-explainer` |
| `concept-classifier` | Tags changed code with concepts such as PII write, billing side effect, external HTTP call. | `agents/concept-classifier` |
| `policy-gate` | Evaluates policy pack rules and emits pass/warn/block findings plus override suspend payloads. | `agents/policy-gate` |
| `prompt-canary` | Runs deterministic prompt/model/agent workflow canary checks. | `agents/prompt-canary` |
| `contract-comparator` | Compares shape-only runtime contracts and suggests property-test stubs. | `agents/contract-comparator` |
| `test-coverage-validator` | Validates whether changed tests cover PR intent, behavioral deltas, and changed source functionality. | `agents/test-coverage-validator` |
| `truth-report-synthesizer` | Aggregates all agent outputs into merge readiness, checks, PR comment, and dashboard summary. | `agents/truth-report-synthesizer` |

## Orchestration Flow

The demo orchestration order is:

1. `review-compression`
2. `intent-extractor`
3. `semantic-diff-explainer`
4. `concept-classifier`
5. `policy-gate`
6. `prompt-canary`
7. `contract-comparator`
8. `evidence-mapper`
9. `test-coverage-validator`
10. `truth-report-synthesizer`

The sequence is implemented in:

```text
packages/orchestration/engine.py
```

The local OE-compatible invoker is:

```text
packages/orchestration/local_platform.py
```

The JSON-backed Mongo-shaped demo store is:

```text
packages/mongo/local_store.py
```

## Production Swap Points

For Magenta deployment:

- Keep each `agents/<name>/agent.yaml`.
- Run `./tools/bin/agentic validate agents/<name>/agent.yaml`.
- Initialize/build/deploy per workspace with `agentic init`, `agentic build --workspace <name>`, and `agentic deploy --workspace <name>`.
- Replace `LocalPlatformClient.invoke(...)` with an HTTPS client calling OE `/invoke`.
- Replace `LocalMergeGuardStore` with Mongo collections and `mongomem_worker.queue.TaskQueueProcessor`.

Each agent exposes this deployable pattern:

```python
app = create_app(AGENT_ID, "...")
register_entrypoint(app, run)
```

When `magenta_sdklanggraph` is present, `create_app` returns a Magenta SDK `App`, and `register_entrypoint` wraps the deterministic feature logic in a one-node LangGraph compiled with `app.checkpointer()`. In platform mode the graph reads a JSON message, extracts either the direct envelope or `{"payload": ...}`, runs the feature logic, and emits the structured `AgentResult` as the final AI message. When the SDK is absent, the same code runs through the local shim.

Build one workspace after `agentic init`:

```sh
./tools/bin/agentic build --workspace review-compression
```

Build all workspaces:

```sh
./tools/bin/agentic build --all
```

The demo intentionally does not implement GitHub App installation or real webhook handling because the current focus is independent agents and dashboard orchestration.
