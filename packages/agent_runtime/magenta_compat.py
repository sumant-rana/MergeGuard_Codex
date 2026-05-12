from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any, TypedDict

from .local_app import LocalAgentApp


class _AgentState(TypedDict, total=False):
    messages: list[Any]
    payload: dict[str, Any]
    result: dict[str, Any]


def create_app(agent_id: str, description: str = "") -> Any:
    """Create a Magenta App when available, otherwise a local demo app.

    Deployed agent containers install `magenta-sdklanggraph`, so `app` is a
    real Magenta SDK App there. Local tests in this repo intentionally avoid
    external Python dependencies, so they receive `LocalAgentApp`.
    """

    try:
        from magenta_sdklanggraph import App  # type: ignore
    except Exception:
        return LocalAgentApp(agent_id, description)

    org_id = os.environ.get("ORG_ID", "local-dev")
    return App(app_name=agent_id, org_id=org_id)


def register_entrypoint(app: Any, run_logic: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    """Register one deterministic payload-in/result-out function as an agent.

    For Magenta this builds a one-node LangGraph. The graph accepts either
    `{"payload": ...}`, a direct payload dict, or a JSON platform message,
    then emits the structured result as the final AI message.
    """

    if isinstance(app, LocalAgentApp):
        app.entrypoint(run_logic)
        return

    # Keep local tools/tests convenient when the SDK is installed on a laptop.
    setattr(app, "invoke", run_logic)

    @app.entrypoint
    def build_agent() -> Any:
        from langchain_core.messages import AIMessage
        from langgraph.graph import END, START, StateGraph

        def run_node(state: _AgentState) -> dict[str, Any]:
            result = run_logic(_payload_from_state(state))
            return {
                "result": result,
                "messages": [AIMessage(content=json.dumps(result))],
            }

        builder = StateGraph(_AgentState)
        builder.add_node("agent", run_node)
        builder.add_edge(START, "agent")
        builder.add_edge("agent", END)
        return builder.compile(checkpointer=app.checkpointer())


def _payload_from_state(state: _AgentState) -> dict[str, Any]:
    direct_payload = state.get("payload")
    if direct_payload is not None:
        return direct_payload

    messages = state.get("messages") or []
    if messages:
        message_payload = _payload_from_message(messages[-1])
        if isinstance(message_payload, dict):
            return message_payload

    return {key: value for key, value in state.items() if key not in {"messages", "result"}}


def _payload_from_message(message: Any) -> dict[str, Any]:
    content = (
        message.get("content")
        if isinstance(message, dict)
        else getattr(message, "content", message)
    )
    if isinstance(content, list):
        content = "\n".join(_message_part_to_text(part) for part in content)
    if not isinstance(content, str):
        return {"message": content}

    text = content.strip()
    if not text:
        return {}

    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return {"message": content}

    if isinstance(decoded, dict):
        payload = decoded.get("payload", decoded)
        return payload if isinstance(payload, dict) else {"payload": payload}

    return {"message": decoded}


def _message_part_to_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        value = part.get("text") or part.get("content") or part.get("value")
        return "" if value is None else str(value)
    return str(part)
