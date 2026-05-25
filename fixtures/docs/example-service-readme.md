# example-service

A reference service that mergeguard onboarding fixtures use to exercise
the docs-indexer end-to-end without hitting the real GitHub REST API.

## Getting Started

Install dependencies and start the dev server:

```
make install
make dev
```

## Architecture

See `docs/architecture.md` for the deeper dive. Highlights:

- Stateless HTTP layer.
- Background workers driven by MongoDB queues.
- Vector search powered by Magenta memory.

## Operating notes

Refund failures retry up to 3 times. Customer PII never leaks into
webhook responses. Prompt updates are gated behind a canary check.
