# MergeGuard Semantic Memory

MergeGuard now includes a deployable `semantic-evidence-agent` that uses Magenta memory as the access layer for Voyage embeddings and Atlas Vector Search.

## What It Does

The agent runs after PR triage, intent extraction, behavior impact, concept classification, prompt drift, and contract comparison. It then:

- indexes PR intent, changed-file summaries, behavioral deltas, concept findings, and configured repository memory;
- saves those records with `app.memory.save_semantic(...)`;
- searches with `app.memory.search_semantic(...)` for related tests, docs, prior PRs, policy notes, and runbooks;
- emits memory-backed evidence for the Verification Evidence, Test Coverage Validator, and Truth Report agents.

In Magenta, `app.memory` is backed by platform memory. The platform setup supplies Voyage embeddings and Atlas vector search through `VOYAGE_API_KEY` and `MONGODB_URI`. Locally, MergeGuard uses a deterministic lexical memory shim so the demo works without cloud credentials.

## Local Demo

```bash
python3 apps/api/main.py
```

Open:

```text
http://127.0.0.1:4100
```

Click `Run Demo PR`. The agent pipeline now shows `Repository Memory` as its own stage, and the dashboard includes a `Memory` tab with:

- indexed record count;
- memory provider;
- related test files;
- similar prior PRs;
- requirement-level evidence;
- retrieved semantic matches.

## Magenta Deployment

The new agent is independently deployable:

```bash
./tools/bin/agentic build --workspace semantic-evidence-agent
./tools/bin/agentic deploy --workspace semantic-evidence-agent
```

For platform memory, link Atlas and Voyage services:

```bash
./tools/bin/agentic atlas setup --context prod
./tools/bin/agentic secret sync --context prod
```

The required platform secrets are:

- `MONGODB_URI`
- `VOYAGE_API_KEY`
- one LLM key if the workspace also runs LLM-backed agents

The agent has `features.memory: true` in `agents/semantic-evidence-agent/agent.yaml`, so the deployed runtime reads the memory capability from Magenta configuration.

## Input Shape

The agent consumes the same MergeGuard envelope as the other agents:

```json
{
  "analysis_run_id": "run_123",
  "pull_request": {},
  "changed_files": [],
  "prior_results": {},
  "settings": {
    "repository_memory": [
      {
        "type": "test",
        "path": "payments/refund_processor.test.ts",
        "title": "Refund retry test coverage",
        "text": "Existing tests cover transient refund retry behavior."
      }
    ]
  }
}
```

`settings.repository_memory` is optional. It is useful for demos and for local repository indexing scripts. In production, the same records can be written through Magenta memory before or during analysis.

## Output Shape

Key fields emitted by the agent:

- `memory_provider`: `magenta-platform` or `local-demo`
- `index`: records stored and source type counts
- `semantic_queries`: searches issued for PR intent and risk signals
- `semantic_matches`: normalized retrieved memory records
- `requirement_evidence`: intent-by-intent memory coverage
- `related_tests`: existing tests likely relevant to the PR
- `similar_prs`: prior PRs with similar intent or risk profile
- `memory_findings`: missing-memory gaps for reviewer attention
- `recommended_test_updates`: test files that should be extended or linked

