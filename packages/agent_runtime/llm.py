"""Stdlib-only LLM helper for MergeGuard agents.

Calls an OpenAI-compatible chat completions endpoint with ``response_format=
{"type": "json_object"}`` and returns the parsed JSON dict.

Design constraints:

- **Stdlib only.** Uses :mod:`urllib.request`. This keeps the dependency-light
  promise of MergeGuard's main runtime path (``python3 apps/api/main.py`` and
  ``python3 apps/worker/main.py`` should work on a clean checkout without
  ``pip install``).
- **Returns ``None`` on any failure** — missing API key, transport error,
  non-JSON response, invalid response shape. Callers MUST handle the
  ``None`` case by falling back to their deterministic implementation so
  the dashboard always gets a structured result.
- **No automatic retries.** Transient errors at this layer would compound
  across 12 LLM-using agents per PR. If an agent really wants retries, it
  can wrap this call itself.

Configured via env vars (typically loaded from repo-root .env):
    OPENAI_API_KEY    Bearer token.
    OPENAI_BASE_URL   Endpoint base (default ``https://api.openai.com/v1``).
                      Set to your gateway's URL if using a proxy.
    MERGEGUARD_LLM_MODEL    Default model for all agents
                            (default ``gpt-4o-mini``).
    MERGEGUARD_LLM_TIMEOUT  Request timeout in seconds (default ``60``).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 60.0


def llm_available() -> bool:
    """Quick check: does the runtime have what it needs to make an LLM call?"""
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def call_llm_json(
    *,
    system: str,
    user: str,
    model: str | None = None,
    temperature: float = 0.0,
    timeout: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any] | None:
    """Call the chat completions endpoint, expect a JSON object reply.

    Returns the parsed dict, or ``None`` if anything went wrong (caller
    should fall back to its deterministic path).
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    base_url = (
        os.environ.get("OPENAI_BASE_URL", "").strip()
        or "https://api.openai.com/v1"
    ).rstrip("/")
    chosen_model = (
        model
        or os.environ.get("MERGEGUARD_LLM_MODEL", "").strip()
        or DEFAULT_MODEL
    )
    chosen_timeout = float(timeout or os.environ.get("MERGEGUARD_LLM_TIMEOUT", DEFAULT_TIMEOUT))

    body: dict[str, Any] = {
        "model": chosen_model,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # Grove (MongoDB's Azure-based OpenAI gateway) requires the ``api-key``
    # header alongside the bearer token. Match autopilot's
    # ``build_llm._build_openai`` detection so both projects route to the
    # same gateway with the same auth.
    if "grove-foundry" in base_url or "azure-api.net" in base_url.lower():
        headers["api-key"] = api_key

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base_url + "/chat/completions",
        data=data,
        method="POST",
        headers=headers,
    )

    try:
        with urllib.request.urlopen(req, timeout=chosen_timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        logger.warning("LLM HTTP %d: %s — falling back to deterministic", e.code, err_body)
        return None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        logger.warning("LLM transport failure: %s — falling back to deterministic", e)
        return None

    try:
        envelope = json.loads(raw)
        content = envelope["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        logger.warning("LLM response envelope unparseable: %s", e)
        return None

    if not isinstance(content, str):
        return None

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        logger.warning("LLM returned non-JSON content (%s): %s", e, content[:200])
        return None

    if not isinstance(parsed, dict):
        logger.warning("LLM returned a non-dict JSON value (type=%s)", type(parsed).__name__)
        return None

    return parsed
