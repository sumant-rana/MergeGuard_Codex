"""Storage adapter for the onboarding ``pr-history-indexer`` agent.

This package isolates persistence behind a small ``PRHistoryStore`` Protocol
so the agent can run unchanged against:

- ``InMemoryPRHistoryStore`` for unit tests and laptop demos,
- ``MongoPRHistoryStore`` for ``storage.mode == "local"`` (docker MongoDB) and
  ``storage.mode == "cloud"`` (Atlas).

The shapes mirror the plan at ``.cursor/plans/pr_history_agent_complete_*``.
"""

from .adapter import (
    InMemoryPRHistoryStore,
    PRHistoryStore,
    pr_file_key,
    pr_key,
)
from .models import (
    HistorySignals,
    OnboardingRun,
    PriorPR,
    PriorPRFile,
)

__all__ = [
    "HistorySignals",
    "InMemoryPRHistoryStore",
    "OnboardingRun",
    "PriorPR",
    "PriorPRFile",
    "PRHistoryStore",
    "pr_file_key",
    "pr_key",
]
