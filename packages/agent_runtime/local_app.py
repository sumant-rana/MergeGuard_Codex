from __future__ import annotations

from collections.abc import Callable
from typing import Any

from packages.core.models import AgentResult


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
