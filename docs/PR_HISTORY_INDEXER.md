# PR History Indexer

`pr-history-indexer` is an **onboarding-only** Magenta agent. It scans
prior pull requests for a target repository, persists structured PR
history in MongoDB/Atlas, writes compact semantic records into Magenta
memory, and computes aggregated historical signals that the
`review-compression` agent consumes during normal PR analysis.

It is **not** part of the per-PR 12-agent sequence. It runs during
onboarding and on demand (re-index).

## Runtime modes

The agent rejects any `storage.mode` other than:

| mode    | persistence                | embeddings                  |
|---------|----------------------------|------------------------------|
| `local` | dockerized MongoDB         | Magenta memory shim (when available); otherwise `embeddings_written=false` warning |
| `cloud` | MongoDB Atlas              | Magenta cloud memory / Atlas Vector Search |

Required env in both modes:

- `MONGODB_URI` — points at the dockerized container (local) or the
  Atlas cluster (cloud). Missing URI is a hard failure.
- `GITHUB_TOKEN` (or `credentials.github_token` in the request body) —
  used by the REST collector. We deliberately use REST and **not** the
  `gh` CLI because neither the local OE container nor the cloud
  workspace ship `gh`.

Optional env:

- `MONGODB_HISTORY_DB` — database name; defaults to `mergeguard`.

## Input contract

```json
{
  "onboarding_run_id": "onb_123",
  "repository": {
    "owner": "mongodb",
    "name": "example-service",
    "full_name": "mongodb/example-service",
    "default_branch": "main"
  },
  "source": {
    "provider": "github",
    "mode": "github_app | gh_cli | token",
    "api_base_url": "https://api.github.com"
  },
  "scan": {
    "max_prs": 500,
    "states": ["merged", "closed"],
    "since": "2025-01-01T00:00:00Z",
    "include_files": true,
    "include_comments": false,
    "include_reviews": true,
    "include_linked_issues": true
  },
  "storage": {"mode": "local | cloud", "repo_key": "mongodb/example-service"},
  "credentials": {"github_token": "..."}
}
```

`include_comments` is **off** in v1 — PR review comments can contain
sensitive information and require an explicit opt-in surface that is
out of scope for this milestone.

## Output contract

```json
{
  "repository": "mongodb/example-service",
  "mode": "local",
  "scan_summary": {
    "prs_seen": 500,
    "prs_indexed": 487,
    "prs_skipped": 13,
    "files_indexed": 2200,
    "memory_records_written": 487,
    "embeddings_written": 487,
    "embedding_provider": "LocalSemanticMemory | MagentaMemory",
    "status": "completed"
  },
  "historical_signals": { /* see signals.py */ },
  "retrieval_ready": true,
  "warnings": [],
  "errors": []
}
```

## HTTP API

Three onboarding endpoints sit in front of the agent:

| method | path                                                          | purpose                                  |
|--------|---------------------------------------------------------------|------------------------------------------|
| POST   | `/api/onboarding/{session_id}/pr-history/start`               | start a background scan                  |
| GET    | `/api/onboarding/{session_id}/pr-history/status`              | poll the onboarding run                  |
| POST   | `/api/onboarding/{session_id}/pr-history/retry`               | re-run with the same (or new) payload    |

`start` validates the body (`storage.mode`, repository, credentials),
records the run in the history store, and dispatches the agent on a
daemon thread. The HTTP response is **202** before the scan completes;
clients poll `status` for progress.

`retry` reuses the stored `request_payload` when the body is empty so
the dashboard can offer a one-click retry.

## Collections (MongoDB / Atlas)

| collection            | unique key                                | notes |
|-----------------------|-------------------------------------------|-------|
| `repositories`        | `repo_key`                                | onboarded repo metadata |
| `onboarding_runs`     | `onboarding_run_id`                       | scan lifecycle + warnings/errors |
| `prior_prs`           | `(repo_key, pr_number)`                   | normalized prior PRs |
| `prior_pr_files`      | `(repo_key, pr_number, path)`             | per-file deltas |
| `repo_history_signals`| `repo_key`                                | aggregated signals |
| `memory_records`      | `label`                                   | metadata about Magenta memory writes |

Indexes are created on first use; re-running onboarding is idempotent.

## Historical signals

`compute_history_signals` (see `packages/history_store/signals.py`)
returns:

- `frequently_changed_files` — top-N by change count.
- `files_changed_together` — co-change pairs (count ≥ 2).
- `hotspot_paths` — score-weighted on frequency, bug labels, linked
  Jira keys, with human-readable `reasons`.
- `owner_activity` — per-author PR + file counts.
- `review_latency_by_area` — avg days from PR creation to merge, by
  top-level path area.
- `jira_key_frequency` — per-project key counts.

These feed the future hybrid `review-compression` agent's `history_context`.

## Local development

```bash
# unit tests (no Docker required; Mongo tests skip gracefully)
python3 -m unittest discover -s tests_agentic -p '*_test.py'

# Mongo integration tests (real container)
pip install -r requirements-dev.txt
python3 -m unittest tests_agentic.history_store_mongo_test
```

## Why REST, not `gh` CLI

The agent runs inside containers (`local` = docker, `cloud` = Magenta
workspace). Neither ships `gh`. The REST collector reuses
`packages/github_pr/pr_fetcher.py`'s auth, pagination, and rate-limit
conventions so it behaves identically to the live PR ingest path. `gh`
remains a laptop convenience for onboarding CLI ergonomics (see
`scripts/mergeguard_pr.py`), not as the agent runtime backend.

## Out of scope (v1)

- Review-comment collection — gated behind a future opt-in flag.
- Per-PR pipeline inclusion — onboarding-only for now.
- Reviewer-override learning — requires productized override data.
- Raw-patch embeddings — semantic records keep summaries, not bodies.
