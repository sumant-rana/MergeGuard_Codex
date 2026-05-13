"""Sync client for the Magenta Orchestration Engine (local-dev mode).

Speaks the local-dev OE endpoints exposed by ``agentic dev up --all``:

    POST /invoke    — start an agent run
    GET  /healthz   — liveness probe

Each agent has its own OE container, so the caller binds ``base_url`` to
``http://oe-<agent-name>:8000`` (or ``http://127.0.0.1:<mapped-port>`` for
host-networked tests).

Uses stdlib :mod:`urllib.request` to keep MergeGuard dependency-light.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class InvokeStatus(str, Enum):
    COMPLETED = "completed"
    SUSPENDED = "suspended"
    FAILED = "failed"


@dataclass
class InvokeResult:
    """Normalized result envelope. ``raw`` carries the full OE response."""

    thread_id: str
    status: InvokeStatus
    result: dict[str, Any] | None = None
    suspend_payload: dict[str, Any] | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class OEError(Exception):
    """Raised on transport errors or unexpected response shapes."""


class OEClient:
    """Sync OE client (stdlib-only).

    Usage:
        with OEClient("http://oe-review-compression:8000") as oe:
            result = oe.invoke(
                agent_id="review-compression",
                payload={"pull_request": {...}, "changed_files": [...]},
                thread_id=run_id,
            )
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 120.0,
        bearer_token: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._bearer = bearer_token

    def __enter__(self) -> "OEClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        # No persistent connection in urllib; nothing to close.
        pass

    def close(self) -> None:
        pass

    def healthz(self) -> bool:
        for path in ("/healthz", "/"):
            try:
                req = urllib.request.Request(self._base_url + path, method="GET")
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return 200 <= resp.status < 400
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    return e.code < 400
                continue
            except (urllib.error.URLError, OSError):
                continue
        return False

    def invoke(
        self,
        agent_id: str,
        payload: dict[str, Any],
        thread_id: str | None = None,
        request_context: dict[str, Any] | None = None,
    ) -> InvokeResult:
        """POST /invoke and return an :class:`InvokeResult`."""
        body: dict[str, Any] = {**payload}
        if thread_id is not None:
            body["thread_id"] = thread_id
        if request_context:
            body["request_context"] = request_context

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._base_url + "/invoke",
            data=data,
            method="POST",
            headers=self._default_headers(),
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                response_body = resp.read().decode("utf-8")
                status_code = resp.status
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
            raise OEError(
                f"OE /invoke (agent_id={agent_id}) returned {e.code}: "
                f"{err_body[:500]}"
            ) from e
        except (urllib.error.URLError, OSError) as e:
            raise OEError(f"OE /invoke transport failure: {e}") from e

        if status_code >= 400:
            raise OEError(
                f"OE /invoke (agent_id={agent_id}) returned {status_code}: "
                f"{response_body[:500]}"
            )

        try:
            data_json = json.loads(response_body)
        except json.JSONDecodeError as e:
            raise OEError(f"OE /invoke returned non-JSON body: {response_body[:200]}") from e

        return self._parse_result(data_json)

    def _default_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._bearer:
            headers["Authorization"] = f"Bearer {self._bearer}"
        return headers

    @staticmethod
    def _parse_result(data: dict[str, Any]) -> InvokeResult:
        thread_id = str(data.get("thread_id") or data.get("execution_id") or "")
        raw_status = (data.get("status") or "completed").lower()
        if raw_status not in {s.value for s in InvokeStatus}:
            raw_status = InvokeStatus.FAILED.value
        status = InvokeStatus(raw_status)

        result_field = data.get("result")
        if isinstance(result_field, dict):
            result_dict: dict[str, Any] | None = result_field
        elif result_field is not None:
            result_dict = {"value": result_field}
        else:
            result_dict = None

        return InvokeResult(
            thread_id=thread_id,
            status=status,
            result=result_dict,
            suspend_payload=data.get("suspend_payload"),
            error=data.get("error"),
            raw=data,
        )
