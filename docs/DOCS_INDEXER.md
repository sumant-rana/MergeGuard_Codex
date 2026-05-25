# Docs Indexer

`docs-indexer` is an **onboarding-only** Magenta agent. It fetches
documentation from a target repository (README, `docs/`, and any
user-requested paths), persists each full document in MongoDB/Atlas,
splits the docs into semantic chunks, and writes each chunk into
Magenta semantic memory keyed by `repo_key`. Downstream agents
(`semantic-evidence-agent`, the future hybrid `review-compression`,
etc.) read these chunks to answer "what do the docs say about this?"
during normal PR analysis.

It is **not** part of the per-PR 12-agent sequence. It runs during
onboarding and on demand.

## Why we store the full docs *and* the embeddings

Two paths were considered:

| approach | storage cost | re-index needs network? | hybrid search? | context expansion on a hit? |
|---|---|---|---|---|
| (A) **Store docs + embeddings** *(chosen)* | small (docs are KBs) | no (re-embed from Mongo) | yes (Atlas Search + Vector Search) | yes (fetch full doc on a chunk hit) |
| (B) Embeddings only | smaller | yes (re-fetch from GitHub) | no | no (only the chunk text) |

(B) sounds cheaper but is misleading: vector stores already keep the
chunk text inside each record, so "only embeddings" still means the
chunk text is duplicated in the vector index. (A) adds the full
document on top — at trivial cost — and unlocks three concrete things:
re-embedding without re-pulling from GitHub, hybrid lexical+semantic
search, and surrounding-context expansion. We picked (A).

## Repo-scoped embeddings

Every embedding write passes:
- `user_id=repo_key`  — Magenta's tenant-scoping field that
  `search_semantic` filters on.
- `metadata.repo_key=<owner>/<name>` — belt-and-braces so any direct
  Mongo query can still filter by repo.

A regression test pins this for **both** onboarding agents
(`pr-history-indexer` and `docs-indexer`). Downstream
`semantic-evidence-agent` already searches with `user_id=repo_key`,
which means retrieval is naturally limited to the current repo.

## Runtime modes

Same as `pr-history-indexer`: `storage.mode` must be `local` (docker
MongoDB) or `cloud` (Atlas). Anything else is a hard rejection.

Required env in both modes:

- `MONGODB_URI`
- `GITHUB_TOKEN` (or `credentials.github_token` in the request body)

## Input contract

```json
{
  "onboarding_run_id": "onb_docs_123",
  "repository": {
    "owner": "mongodb",
    "name": "example-service",
    "full_name": "mongodb/example-service",
    "default_branch": "main"
  },
  "source": {
    "provider": "github",
    "mode": "token",
    "api_base_url": "https://api.github.com"
  },
  "scan": {
    "paths": ["ARCHITECTURE.md", "wiki/"]
  },
  "storage": {"mode": "local", "repo_key": "mongodb/example-service"},
  "credentials": {"github_token": "..."}
}
```

`scan.paths` is **additive** — `README.md` and `docs/` are always
prepended. Folder entries end in `/` and are walked recursively up to
`max_dir_depth=5` (configurable).

## Output contract

```json
{
  "repository": "mongodb/example-service",
  "mode": "local",
  "scan_summary": {
    "paths_requested": ["README.md", "docs/", "ARCHITECTURE.md"],
    "files_seen": 14,
    "files_skipped": 2,
    "docs_indexed": 12,
    "chunks_indexed": 38,
    "embeddings_written": 38,
    "embedding_provider": "LocalSemanticMemory | MagentaMemory",
    "status": "completed"
  },
  "retrieval_ready": true,
  "warnings": [],
  "errors": []
}
```

## HTTP API

| method | path                                                          | purpose |
|--------|---------------------------------------------------------------|---------|
| POST   | `/api/onboarding/{session_id}/docs/start`                     | start a background scan |
| GET    | `/api/onboarding/{session_id}/docs/status`                    | poll the onboarding run |
| POST   | `/api/onboarding/{session_id}/docs/retry`                     | re-run with the same (or new) payload |

Validation, credential redaction, and background-thread spawn use the
same shared helpers as the PR-history endpoints.

## Collections (MongoDB / Atlas)

In addition to the onboarding/PR collections, `docs-indexer` writes:

| collection           | unique key                  | notes |
|----------------------|-----------------------------|-------|
| `repo_docs`          | `(repo_key, path)`          | full document blob + metadata |
| `doc_chunk_records`  | `label`                     | audit metadata for every chunk that hit the vector store |

Each chunk's `label` follows
`mergeguard:doc:{repo_key}::{path}#chunk{N}-{heading-slug}` so the
metadata records, the Mongo entry, and the Magenta memory entry can be
joined for debugging.

## Chunking

- **Markdown** (`.md`, `.mdx`): split on ATX headings (`#`, `##`,
  `###`). Each section becomes one or more chunks of up to
  `max_chunk_chars=1200` (default) with `chunk_overlap_chars=200`
  overlap. Pre-heading content is captured as a synthetic preamble
  chunk.
- **Plain text / RST / AsciiDoc**: overlapping character windows of the
  same dimensions.
- A document is capped at `max_chunks=25` to keep large reference docs
  from dominating the index.

## Defaults

| concern | default | how to override |
|---|---|---|
| paths | `["README.md", "docs/"]` | append entries to `scan.paths` |
| extensions accepted | `.md, .mdx, .rst, .txt, .adoc` (+ bare `README`) | not configurable in v1 |
| per-file cap | 256 KB | `max_bytes_per_file` arg |
| docs per scan | 1000 | `max_files` arg |
| chunks per doc | 25 | `max_chunks` arg |
| recursion depth | 5 | `max_dir_depth` arg |

## Local development

```bash
python3 -m unittest discover -s tests_agentic -p '*_test.py'
```

Mongo integration tests (`history_store_mongo_test.py`) cover the new
`repo_docs` / `doc_chunk_records` collections and skip when Docker is
absent.

## Out of scope (v1)

- Non-textual docs (PDFs, images, Confluence).
- Doc freshness checks / re-fetching only changed files.
- Cross-repo doc ranking — retrieval is single-repo by design.
- Custom chunkers per repo.
