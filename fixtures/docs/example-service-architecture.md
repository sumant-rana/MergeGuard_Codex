# Architecture

## Overview

The example service receives refund webhooks, performs idempotent
processing, and forwards outcomes to downstream notification systems.

## Components

### HTTP layer

Stateless. Validates webhook signatures, normalizes payloads, and
enqueues work onto MongoDB-backed queues.

### Worker layer

Long-running consumers that:

- pull work from the queue,
- call the payment gateway with retry semantics,
- persist the outcome,
- emit follow-up events.

### Memory layer

All semantic context — prior PRs, repo docs, recurring incidents — lives
in Magenta memory, keyed by repository. Downstream agents (review
compression, semantic evidence) scope their queries by `repo_key`.

## Failure modes

If the payment gateway returns a 5xx, the worker retries up to 3 times
with exponential backoff. Beyond that, the work item is parked in a
dead-letter collection for human inspection.
