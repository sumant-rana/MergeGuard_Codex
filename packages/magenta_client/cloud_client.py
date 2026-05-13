"""Sync cloud client for the Magenta tenant API.

Speaks:
    POST /api/v1/invokeWorkspaceStream

Headers:
    Authorization: Bearer <api key>
    X-Project-ID:  <project id>
    X-Workspace-ID: <workspace id>   ← identifies the deployed agent

Body:
    {... agent's input fields ...}  (e.g. ``{"message": "..."}``)

Response is a Server-Sent Events stream. The Magenta tenant emits three
chunk types:

    chunk_type = "metadata"  — start marker, empty content
    chunk_type = "text"      — token-by-token delta of the agent's response
    chunk_type = "done"      — terminal event; ``content`` is the FULL response
                               (not a delta) and ``metadata.status`` is "completed"

We aggregate the deltas and prefer the ``done`` event's full content when it
arrives, falling back to the concatenated deltas if the stream cuts off
before terminating.

Uses stdlib :mod:`urllib.request` for streaming — line-iteration over the
response body keeps memory bounded and lets us short-circuit on ``done``.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from packages.magenta_client.client import InvokeResult, InvokeStatus, OEError

logger = logging.getLogger(__name__)


@dataclass
class CloudCredentials:
    """Per-workspace credentials. One :class:`CloudOEClient` holds one set."""

    api_key: str
    project_id: str
    workspace_id: str


class CloudOEClient:
    """Sync drop-in for :class:`OEClient` that targets the Magenta cloud.

    Construction binds a single workspace because the tenant routes by the
    ``X-Workspace-ID`` header. The ``agent_id`` arg to :meth:`invoke` is
    informational only.

    Usage:
        with CloudOEClient(
            base_url="https://agentic-platform.mongodb.com",
            credentials=CloudCredentials(api_key=..., project_id=..., workspace_id=...),
        ) as oe:
            result = oe.invoke(
                agent_id="review-compression",
                payload={"pull_request": {...}, ...},
                thread_id=run_id,
            )
    """

    DEFAULT_BASE_URL = "https://agentic-platform.mongodb.com"
    STREAM_PATH = "/api/v1/invokeWorkspaceStream"

    def __init__(
        self,
        base_url: str,
        *,
        credentials: CloudCredentials,
        timeout_seconds: float = 180.0,
        max_concurrent_invokes: int | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._creds = credentials
        self._timeout = timeout_seconds
        # The Magenta tenant edge drops in-flight streams when too many land
        # on one workspace at once. Cap concurrent invokes if requested.
        self._semaphore = (
            threading.Semaphore(max_concurrent_invokes)
            if max_concurrent_invokes and max_concurrent_invokes > 0
            else None
        )

    def __enter__(self) -> "CloudOEClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def close(self) -> None:
        pass

    def invoke(
        self,
        agent_id: str,
        payload: dict[str, Any],
        thread_id: str | None = None,
        request_context: dict[str, Any] | None = None,
    ) -> InvokeResult:
        """POST to the tenant stream endpoint and aggregate the response."""
        body: dict[str, Any] = {**payload}
        if thread_id is not None:
            body["thread_id"] = thread_id

        headers = {
            "Authorization": f"Bearer {self._creds.api_key}",
            "X-Project-ID": self._creds.project_id,
            "X-Workspace-ID": self._creds.workspace_id,
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/x-ndjson, */*",
        }
        if request_context:
            for k, v in request_context.items():
                headers[f"X-Mdb-Agentic-Platform-Custom-{k}"] = str(v)

        logger.debug(
            "cloud.invoke: workspace=%s agent_id=%s thread_id=%s",
            self._creds.workspace_id, agent_id, thread_id,
        )

        events: list[dict[str, Any]] = []
        full_text_chunks: list[str] = []

        def _run_stream() -> None:
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                self._base_url + self.STREAM_PATH,
                data=data,
                method="POST",
                headers=headers,
            )
            try:
                resp = urllib.request.urlopen(req, timeout=self._timeout)
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
                raise OEError(
                    f"cloud /invokeWorkspaceStream (workspace={self._creds.workspace_id}) "
                    f"returned {e.code}: {err_body[:500]}"
                ) from e
            except (urllib.error.URLError, OSError) as e:
                raise OEError(f"cloud /invokeWorkspaceStream transport failure: {e}") from e

            try:
                _consume_stream(resp, events, full_text_chunks)
            finally:
                resp.close()

        if self._semaphore is not None:
            with self._semaphore:
                _run_stream()
        else:
            _run_stream()

        # The Magenta tenant edge can emit an ``error`` chunk and still close
        # the stream cleanly (no HTTP error). Detect that here and raise so
        # the retry layer in platform_factory treats it as transient.
        for event in events:
            if event.get("chunk_type") == "error":
                err_msg = str(event.get("error") or event.get("content") or "stream error")
                raise OEError(
                    f"cloud /invokeWorkspaceStream (workspace={self._creds.workspace_id}) "
                    f"emitted chunk_type=error: {err_msg}"
                )

        # Pick the best representation of the agent's output:
        #   1. ``chunk_type: "done"`` terminal → use its ``content`` verbatim.
        #   2. Other final-shaped event with a result/output payload.
        #   3. Fallback: concatenated text deltas (partial stream).
        final = _find_final_event(events)
        if final is not None and final.get("chunk_type") == "done":
            done_content = final.get("content")
            if isinstance(done_content, str) and done_content:
                result_payload: dict[str, Any] = {"response": done_content}
            else:
                result_payload = {"response": "".join(full_text_chunks)}
        elif final is not None:
            result_payload = final.get("result") or final.get("output") or final
            if isinstance(result_payload, dict) and not any(
                k in result_payload for k in ("response", "value", "result")
            ):
                result_payload = {"response": json.dumps(result_payload), **result_payload}
        else:
            result_payload = {"response": "".join(full_text_chunks)}

        if not isinstance(result_payload, dict):
            result_payload = {"response": str(result_payload)}

        return InvokeResult(
            thread_id=thread_id or "",
            status=InvokeStatus.COMPLETED,
            result=result_payload,
            suspend_payload=None,
            raw={"events": events},
        )

    def healthz(self) -> bool:
        try:
            req = urllib.request.Request(self._base_url + "/healthz", method="GET")
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.status < 500
        except urllib.error.HTTPError as e:
            return e.code < 500
        except (urllib.error.URLError, OSError):
            return False


# ── Stream parsing helpers ──────────────────────────────────────────────────


def _consume_stream(
    response: Any,
    events: list[dict[str, Any]],
    full_text_chunks: list[str],
) -> None:
    """Drain an SSE response into the events list + text-delta buffer.

    The response object is an :class:`http.client.HTTPResponse` (from
    :func:`urllib.request.urlopen`). We iterate line-by-line, decoding each
    chunk as it arrives.
    """
    # urllib's HTTPResponse supports iter via .readline()
    while True:
        raw_line_bytes = response.readline()
        if not raw_line_bytes:
            break
        line = raw_line_bytes.decode("utf-8", "replace").strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line.startswith(":"):
            continue
        if line == "[DONE]":
            break
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            full_text_chunks.append(line)
            continue
        events.append(event)
        chunk_text = _extract_text_delta(event)
        if chunk_text:
            full_text_chunks.append(chunk_text)


def _extract_text_delta(event: dict[str, Any]) -> str:
    """Pull an incremental text chunk from a streaming event, if present.

    Skips Magenta ``chunk_type: "metadata"`` (start marker, empty content)
    and ``chunk_type: "done"`` (terminal — has the FULL response, not a
    delta, handled separately by :func:`_find_final_event`).
    """
    chunk_type = event.get("chunk_type")
    if chunk_type in {"metadata", "done"}:
        return ""
    for key in ("content", "delta", "text", "data"):
        v = event.get(key)
        if isinstance(v, str):
            return v
    chunk = event.get("chunk")
    if isinstance(chunk, dict):
        c = chunk.get("content")
        if isinstance(c, str):
            return c
    messages = event.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict):
            c = last.get("content")
            if isinstance(c, str):
                return c
    return ""


def _find_final_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the terminal event in the stream, if there is one."""
    if not events:
        return None
    for event in reversed(events):
        if event.get("chunk_type") == "done":
            return event
    for event in reversed(events):
        kind = str(event.get("type") or event.get("event") or "").lower()
        if kind in {"final", "complete", "completed", "end", "result", "done"}:
            return event
        if "result" in event or "output" in event:
            return event
    return events[-1]
