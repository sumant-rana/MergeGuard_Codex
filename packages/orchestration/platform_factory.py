"""Build the right platform client for the current ``AGENT_MODE``.

Three modes:

- ``AGENT_MODE`` unset or ``in-process`` (default for the demo path):
  returns the existing :class:`LocalPlatformClient`, which loads each agent's
  ``main.py`` and calls ``module.app.invoke(payload)`` in the same process.
  No Docker / no network required.

- ``AGENT_MODE=local``: returns :class:`DockerizedPlatformClient`, which
  holds a :class:`magenta_client.OEClient` per agent and POSTs to each
  agent's local-dev OE container (``http://oe-<agent-name>:8000/invoke``).
  Assumes ``agentic dev up --all`` is running.

- ``AGENT_MODE=cloud``: returns :class:`CloudPlatformClient`, which holds a
  :class:`magenta_client.CloudOEClient` per agent, each bound to a
  workspace ID read from ``WORKSPACE_<UPPER_AGENT_ID>`` env vars. Streams
  via the Magenta tenant API.

All three implement the same surface that the orchestrator depends on:
``invoke(agent_id, payload, *, thread_id) -> dict`` returning an
``{execution_id, thread_id, agent_id, status, result}`` envelope.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from packages.magenta_client import (
    CloudCredentials,
    CloudOEClient,
    InvokeResult,
    InvokeStatus,
    OEClient,
    OEError,
)
from packages.orchestration.local_platform import LocalPlatformClient

logger = logging.getLogger(__name__)


# Retries for transient transport errors. The local-dev agent stack
# (``agentic dev up --all``) hot-reloads its AER processes whenever a .py
# file is touched anywhere in the workspace, so a single 12-agent run
# routinely catches one AER mid-restart and gets a 502 / connection
# refused. Autopilot survives the same thing because MongoQueue retries
# the task; MergeGuard's orchestrator has no queue, so we retry here.
#
# Override via ``OE_INVOKE_MAX_ATTEMPTS`` / ``OE_INVOKE_BACKOFF_INITIAL``.
DEFAULT_MAX_INVOKE_ATTEMPTS = 4
DEFAULT_INVOKE_BACKOFF_INITIAL = 2.0

_TRANSIENT_PATTERNS = (
    "connection refused",
    "connection reset",
    "transport failure",
    "returned 502",
    "returned 503",
    "returned 504",
    "timeout",
    "timed out",
    "peer closed connection",
    # Magenta cloud's tenant edge occasionally drops mid-stream with a gRPC
    # error event. CloudOEClient now raises on chunk_type=error containing
    # this message — treat it as retryable.
    "rst_stream",
    "internal_error",
    "rpc error",
    "chunk_type=error",
    "stream terminated",
)


def _is_transient(err: OEError) -> bool:
    msg = str(err).lower()
    return any(p in msg for p in _TRANSIENT_PATTERNS)


def _invoke_with_retry(
    do_invoke: Callable[[], InvokeResult],
    *,
    agent_id: str,
    max_attempts: int = DEFAULT_MAX_INVOKE_ATTEMPTS,
    initial_backoff: float = DEFAULT_INVOKE_BACKOFF_INITIAL,
) -> InvokeResult:
    """Call ``do_invoke()`` with exponential backoff on transient OEErrors.

    Backoff: ``initial_backoff * 2**(attempt-1)`` seconds before each retry
    (so 2s, 4s, 8s for the default schedule). Non-transient errors raise
    immediately. Always raises the last error if all attempts fail.
    """
    last_err: OEError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return do_invoke()
        except OEError as e:
            last_err = e
            if not _is_transient(e) or attempt >= max_attempts:
                raise
            backoff = initial_backoff * (2 ** (attempt - 1))
            logger.warning(
                "platform_factory: agent=%s attempt=%d/%d transient error (%s); "
                "retrying in %.1fs",
                agent_id, attempt, max_attempts, e, backoff,
            )
            time.sleep(backoff)
    assert last_err is not None  # pragma: no cover
    raise last_err


# The 12 agents in MergeGuard. Used to build per-agent client maps in
# local / cloud modes. Kept in sync with ``AGENT_SEQUENCE`` in engine.py.
AGENT_IDS: list[str] = [
    "review-compression",
    "intent-extractor",
    "semantic-diff-explainer",
    "concept-classifier",
    "slop-detector",
    "policy-gate",
    "prompt-canary",
    "contract-comparator",
    "semantic-evidence-agent",
    "evidence-mapper",
    "test-coverage-validator",
    "truth-report-synthesizer",
]


def _workspace_env_var(agent_id: str) -> str:
    """Map ``"review-compression"`` → ``"WORKSPACE_REVIEW_COMPRESSION"``."""
    return "WORKSPACE_" + agent_id.upper().replace("-", "_")


_STRING_WRAPPER_KEYS = frozenset({"response", "value", "result"})


def _adapt_invoke_result(
    result: InvokeResult,
    *,
    agent_id: str,
    thread_id: str,
) -> dict[str, Any]:
    """Convert an :class:`InvokeResult` to the LocalPlatformClient shape.

    The engine treats ``execution["result"]`` as the agent's structured
    output dict. Both deployment shapes need unwrapping:

    - **Local OE** (POST /invoke): the agent's output comes back as a
      JSON-encoded string in the OE envelope's ``result`` field, which
      our :class:`OEClient` parses to ``InvokeResult.result == {"value": "<json>"}``.
    - **Cloud (SSE)**: the agent's output is the stringified JSON content of
      the ``chunk_type: "done"`` event → ``InvokeResult.result == {"response": "<json>"}``.

    In both cases we try to JSON-decode the inner string. If it decodes to
    a dict, that dict becomes ``execution["result"]`` so downstream agents
    can read fields like ``.output`` and ``.checks``.
    """
    result_payload: Any = result.result or {}
    if (
        isinstance(result_payload, dict)
        and len(result_payload) == 1
        and next(iter(result_payload.keys())) in _STRING_WRAPPER_KEYS
    ):
        only_key = next(iter(result_payload.keys()))
        raw_text = result_payload[only_key]
        if isinstance(raw_text, str):
            try:
                decoded = json.loads(raw_text)
                if isinstance(decoded, dict):
                    result_payload = decoded
            except (json.JSONDecodeError, ValueError):
                # Leave the wrapper dict intact; caller can read raw text.
                pass

    return {
        "execution_id": result.thread_id or f"exec-{uuid.uuid4().hex[:12]}",
        "thread_id": thread_id,
        "agent_id": agent_id,
        "status": result.status.value,
        "result": result_payload,
    }


def _wrap_envelope_for_message_only(envelope: dict[str, Any]) -> dict[str, Any]:
    """Encode a rich agent envelope as a single ``message`` JSON string.

    The Magenta OE (both local-dev and tenant) currently requires a top-level
    ``message`` field. The deployed agent's LangGraph entrypoint decodes the
    JSON back into the original envelope (see
    ``packages/agent_runtime/magenta_compat.py:_payload_from_message``).
    """
    return {"message": json.dumps(envelope)}


class DockerizedPlatformClient:
    """Routes each ``invoke(agent_id, ...)`` to that agent's local OE container."""

    DEFAULT_OE_PORT = 8000

    def __init__(
        self,
        *,
        agent_ids: list[str] = AGENT_IDS,
        base_url_template: str | None = None,
        timeout_seconds: float = 120.0,
        max_attempts: int = DEFAULT_MAX_INVOKE_ATTEMPTS,
        backoff_initial: float = DEFAULT_INVOKE_BACKOFF_INITIAL,
    ) -> None:
        # Default base URL pattern matches the names emitted by
        # ``agentic dev up --all`` (e.g. ``http://oe-review-compression:8000``).
        # Override via OE_BASE_URL_TEMPLATE for host-networked tests:
        #   OE_BASE_URL_TEMPLATE=http://127.0.0.1:{port}
        # (and rely on the ``ports:`` mapping in the generated compose).
        self._template = base_url_template or os.environ.get(
            "OE_BASE_URL_TEMPLATE", "http://oe-{agent}:{port}"
        )
        self._clients: dict[str, OEClient] = {
            agent_id: OEClient(
                self._template.format(agent=agent_id, port=self.DEFAULT_OE_PORT),
                timeout_seconds=timeout_seconds,
            )
            for agent_id in agent_ids
        }
        self._max_attempts = max_attempts
        self._backoff_initial = backoff_initial
        self.executions: list[dict[str, Any]] = []

    def invoke(
        self,
        agent_id: str,
        payload: dict[str, Any],
        *,
        thread_id: str,
    ) -> dict[str, Any]:
        client = self._clients.get(agent_id)
        if client is None:
            raise KeyError(f"Unknown agent in dockerized mode: {agent_id}")
        wire_payload = _wrap_envelope_for_message_only(payload)
        result = _invoke_with_retry(
            lambda: client.invoke(agent_id=agent_id, payload=wire_payload, thread_id=thread_id),
            agent_id=agent_id,
            max_attempts=self._max_attempts,
            initial_backoff=self._backoff_initial,
        )
        execution = _adapt_invoke_result(result, agent_id=agent_id, thread_id=thread_id)
        self.executions.append(execution)
        return execution


class CloudPlatformClient:
    """Routes each ``invoke(agent_id, ...)`` to that agent's Magenta workspace."""

    def __init__(
        self,
        *,
        agent_ids: list[str] = AGENT_IDS,
        base_url: str,
        api_key: str,
        project_id: str,
        workspace_ids: dict[str, str],
        timeout_seconds: float = 180.0,
        max_concurrent_invokes: int | None = None,
        max_attempts: int = DEFAULT_MAX_INVOKE_ATTEMPTS,
        backoff_initial: float = DEFAULT_INVOKE_BACKOFF_INITIAL,
    ) -> None:
        self._clients: dict[str, CloudOEClient] = {}
        for agent_id in agent_ids:
            ws = workspace_ids.get(agent_id)
            if not ws:
                raise RuntimeError(
                    f"AGENT_MODE=cloud requires {_workspace_env_var(agent_id)} env var "
                    f"for agent {agent_id!r}"
                )
            self._clients[agent_id] = CloudOEClient(
                base_url,
                credentials=CloudCredentials(
                    api_key=api_key, project_id=project_id, workspace_id=ws,
                ),
                timeout_seconds=timeout_seconds,
                max_concurrent_invokes=max_concurrent_invokes,
            )
        self._max_attempts = max_attempts
        self._backoff_initial = backoff_initial
        self.executions: list[dict[str, Any]] = []

    def invoke(
        self,
        agent_id: str,
        payload: dict[str, Any],
        *,
        thread_id: str,
    ) -> dict[str, Any]:
        client = self._clients.get(agent_id)
        if client is None:
            raise KeyError(f"Unknown agent in cloud mode: {agent_id}")
        wire_payload = _wrap_envelope_for_message_only(payload)
        result = _invoke_with_retry(
            lambda: client.invoke(agent_id=agent_id, payload=wire_payload, thread_id=thread_id),
            agent_id=agent_id,
            max_attempts=self._max_attempts,
            initial_backoff=self._backoff_initial,
        )
        execution = _adapt_invoke_result(result, agent_id=agent_id, thread_id=thread_id)
        self.executions.append(execution)
        return execution


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"AGENT_MODE=cloud but {name!r} is not set. "
            "Set it in .env or pass it via docker-compose."
        )
    return value


def _read_retry_env() -> tuple[int, float]:
    max_attempts_str = os.environ.get("OE_INVOKE_MAX_ATTEMPTS", "").strip()
    max_attempts = (
        int(max_attempts_str)
        if max_attempts_str.isdigit() and int(max_attempts_str) > 0
        else DEFAULT_MAX_INVOKE_ATTEMPTS
    )
    backoff_str = os.environ.get("OE_INVOKE_BACKOFF_INITIAL", "").strip()
    try:
        backoff = float(backoff_str) if backoff_str else DEFAULT_INVOKE_BACKOFF_INITIAL
        if backoff <= 0:
            backoff = DEFAULT_INVOKE_BACKOFF_INITIAL
    except ValueError:
        backoff = DEFAULT_INVOKE_BACKOFF_INITIAL
    return max_attempts, backoff


def build_platform_client(repo_root: str | Path) -> Any:
    """Inspect ``AGENT_MODE`` and return the appropriate platform client.

    Returns an object with ``.invoke(agent_id, payload, *, thread_id) -> dict``.
    """
    mode = os.environ.get("AGENT_MODE", "in-process").strip().lower()
    max_attempts, backoff_initial = _read_retry_env()

    if mode in {"local", "dockerized", "dockerized-local"}:
        client = DockerizedPlatformClient(
            max_attempts=max_attempts,
            backoff_initial=backoff_initial,
        )
        logger.info(
            "platform_factory: mode=local (dockerized) agents=%d base_url_template=%s "
            "max_attempts=%d backoff_initial=%.1fs",
            len(AGENT_IDS), client._template, max_attempts, backoff_initial,
        )
        return client

    if mode == "cloud":
        base_url = os.environ.get(
            "MAGENTA_BASE_URL", "https://agentic-platform.mongodb.com"
        ).strip()
        api_key = _require_env("MAGENTA_API_KEY")
        project_id = _require_env("MAGENTA_PROJECT_ID")
        workspace_ids = {
            agent_id: _require_env(_workspace_env_var(agent_id))
            for agent_id in AGENT_IDS
        }
        cap_str = os.environ.get("CLOUD_MAX_CONCURRENT_INVOKES", "").strip()
        cap = int(cap_str) if cap_str.isdigit() and int(cap_str) > 0 else None
        client = CloudPlatformClient(
            base_url=base_url,
            api_key=api_key,
            project_id=project_id,
            workspace_ids=workspace_ids,
            max_concurrent_invokes=cap,
            max_attempts=max_attempts,
            backoff_initial=backoff_initial,
        )
        logger.info(
            "platform_factory: mode=cloud base_url=%s project=%s agents=%d "
            "max_concurrent=%s max_attempts=%d backoff_initial=%.1fs",
            base_url, project_id, len(AGENT_IDS),
            cap or "(unlimited)", max_attempts, backoff_initial,
        )
        return client

    # Default: in-process Python orchestration (the dependency-light demo path).
    logger.info("platform_factory: mode=in-process (LocalPlatformClient)")
    return LocalPlatformClient(repo_root)
