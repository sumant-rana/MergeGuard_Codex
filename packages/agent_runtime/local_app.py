from __future__ import annotations

from collections.abc import Callable
import re
from typing import Any

from packages.core.models import AgentResult


class LocalSemanticMemory:
    """Small in-process stand-in for Magenta memory during local demo runs."""

    _records: list[dict[str, Any]] = []

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id

    def save_semantic(
        self,
        *,
        text: str,
        label: str,
        user_id: str,
        source: str = "agent",
        visibility: str = "shared",
        metadata: dict[str, Any] | None = None,
        upsert: bool = True,
        agent_id: str | None = None,
    ) -> bool:
        record = {
            "label": label,
            "text": text,
            "user_id": user_id,
            "source": source,
            "visibility": visibility,
            "metadata": metadata or {},
            "agent_id": agent_id or self.agent_id,
        }
        if upsert:
            for index, existing in enumerate(self._records):
                if existing["label"] == label and existing.get("user_id") == user_id:
                    self._records[index] = record
                    return True
        self._records.append(record)
        return True

    def search_semantic(
        self,
        *,
        query: str,
        user_id: str | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        query_tokens = _memory_tokens(query)
        scored: list[dict[str, Any]] = []
        for record in self._records:
            if user_id and record.get("user_id") != user_id:
                continue
            text_tokens = _memory_tokens(
                " ".join(
                    [
                        record.get("text", ""),
                        str(record.get("label", "")),
                        " ".join(str(value) for value in record.get("metadata", {}).values()),
                    ]
                )
            )
            if not query_tokens or not text_tokens:
                score = 0.0
            else:
                overlap = query_tokens & text_tokens
                score = len(overlap) / max(len(query_tokens), 1)
                if overlap:
                    score += min(0.25, len(overlap) / max(len(text_tokens), 1))
            if score <= 0:
                continue
            scored.append({**record, "score": round(min(score, 1.0), 3)})
        return sorted(scored, key=lambda item: item["score"], reverse=True)[:top_k]

    def build_context(
        self,
        *,
        query: str,
        user_id: str | None = None,
        top_k: int = 5,
    ) -> str:
        results = self.search_semantic(query=query, user_id=user_id, top_k=top_k)
        return "\n".join(f"- {item['label']}: {item['text']}" for item in results)


def _memory_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.sub(r"[^a-zA-Z0-9_/-]+", " ", text.lower()).split()
        if len(token) >= 3
    }


class LocalAgentApp:
    """Tiny compatibility layer for local demo runs.

    Real Magenta deployments use magenta_sdklanggraph.App from each agent's
    agent.yaml entrypoint. This shim keeps every agent independently runnable
    in this repository when the platform SDK is not installed.
    """

    def __init__(self, agent_id: str, description: str = "") -> None:
        self.agent_id = agent_id
        self.description = description
        self._entrypoint: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self.tools: dict[str, Callable[..., Any]] = {}
        self.memory = LocalSemanticMemory(agent_id)

    def entrypoint(self, func: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
        self._entrypoint = func
        return func

    def tool(self, func: Callable[..., Any] | None = None, *, is_local: bool = True) -> Any:
        def register(inner: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[inner.__name__] = inner
            setattr(inner, "is_local", is_local)
            return inner

        return register(func) if func else register

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._entrypoint is None:
            raise RuntimeError(f"Agent {self.agent_id} has no entrypoint")
        return self._entrypoint(payload)


def make_agent_result(
    agent_id: str,
    output: dict[str, Any],
    *,
    status: str = "completed",
    confidence: float = 1.0,
    messages: list[str] | None = None,
    trace: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return AgentResult(
        agent_id=agent_id,
        status=status,
        output=output,
        confidence=confidence,
        messages=messages or [],
        trace=trace or [],
    ).asdict()
