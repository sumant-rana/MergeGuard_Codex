"""LLM helper for MergeGuard agents.

Two call paths, picked at runtime:

1. **Registered-LLM path (preferred when an ``app`` is passed)** — uses the
   :class:`langchain_core.language_models.BaseChatModel` registered on the
   Magenta App via :func:`register_default_llm`. Calls flow through the
   Magenta runtime so they show up as traces in the playground / agent
   history, just like autopilot's agents. Requires ``langchain_openai`` to
   be importable (which it is in deployed Magenta containers and in our
   local-Docker agent stack).

2. **Stdlib urllib fallback** — direct ``POST /chat/completions`` against
   the OpenAI-compatible gateway (Grove). Used when no app is provided
   OR when langchain isn't available OR when the runtime call fails.
   Stdlib-only so the demo path
   (``python3 apps/api/main.py`` on a clean host without ``pip install``)
   still works. No tracing on this path — but it always works.

In both paths we ask for ``response_format={"type": "json_object"}`` and
return ``None`` on any failure so callers can fall back to their
deterministic implementation.

Configured via env (typically loaded from repo-root .env):
    OPENAI_API_KEY    Bearer token.
    OPENAI_BASE_URL   Endpoint base. Set to Grove for the MongoDB gateway.
    MERGEGUARD_LLM_MODEL   Default model (default ``gpt-4o-mini``).
    MERGEGUARD_LLM_TIMEOUT Request timeout (default 60s, urllib only).
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

# id(app) → registered runtime LLM (the value returned by app.llm(model)).
# Keyed by id() so we don't keep references that would block GC of test apps.
_REGISTERED_LLMS: dict[int, Any] = {}


def llm_available() -> bool:
    """Quick check: does the runtime have what it needs to make an LLM call?"""
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def _is_claude_4(model_name: str) -> bool:
    """True for Claude 4.x models. They deprecated the ``temperature`` parameter."""
    parts = (model_name or "").lower().split("-")
    return len(parts) >= 3 and parts[0] == "claude" and parts[2] == "4"


def _build_openai_chat(api_key: str, model: str) -> Any | None:
    try:
        from langchain_openai import ChatOpenAI  # type: ignore[import-not-found]
    except ImportError:
        return None
    base_url = (
        os.environ.get("OPENAI_BASE_URL", "").strip()
        or "https://api.openai.com/v1"
    ).rstrip("/")
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "model": model,
        "temperature": 0,
    }
    if "grove-foundry" in base_url:
        kwargs["base_url"] = base_url.split("/v1")[0] + "/v1"
        kwargs["default_headers"] = {"api-key": api_key}
    else:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)


def _build_anthropic_chat(api_key: str, model: str) -> Any | None:
    try:
        from langchain_anthropic import ChatAnthropic  # type: ignore[import-not-found]
    except ImportError:
        return None
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "model_name": model,
    }
    # Claude 4.x deprecated the temperature param — passing it returns HTTP 400.
    if not _is_claude_4(model):
        kwargs["temperature"] = 0
    if base_url:
        if "grove-foundry" in base_url:
            kwargs["base_url"] = base_url.split("/v1")[0].rstrip("/")
            kwargs["default_headers"] = {"api-key": api_key}
        else:
            kwargs["base_url"] = base_url.rstrip("/")
    return ChatAnthropic(**kwargs)


def _resolve_provider_and_model(app: Any) -> tuple[str, str]:
    """Pick provider/model from agent.yaml hints, then env, then sensible defaults.

    The Magenta SDK surfaces ``app.llm_config`` populated from agent.yaml's
    ``config:`` block. We honor that first so changing agent.yaml is enough
    to swap providers without touching agent code.
    """
    llm_config = getattr(app, "llm_config", None)
    provider = (getattr(llm_config, "provider", None) or "").strip().lower()
    model = (getattr(llm_config, "model", None) or "").strip()
    if not provider:
        # Fallback ordering: explicit env override → presence of API keys.
        provider = os.environ.get("MERGEGUARD_LLM_PROVIDER", "").strip().lower()
    if not provider:
        if os.environ.get("ANTHROPIC_API_KEY", "").strip():
            provider = "anthropic"
        elif os.environ.get("OPENAI_API_KEY", "").strip():
            provider = "openai"
        else:
            provider = "openai"
    if not model:
        model = os.environ.get("MERGEGUARD_LLM_MODEL", "").strip()
    if not model:
        model = "claude-sonnet-4-6" if provider == "anthropic" else DEFAULT_MODEL
    return provider, model


def register_default_llm(app: Any) -> Any | None:
    """Register a LangChain chat model on the Magenta App.

    Provider + model are taken from the agent's ``agent.yaml`` (via
    ``app.llm_config``) with env-var fallbacks, mirroring autopilot's
    ``build_llm`` flow. Supports ``provider: openai`` (Grove-aware) and
    ``provider: anthropic`` (Grove-aware).

    Returns the registered LLM, or ``None`` when registration is skipped
    (LocalAgentApp shim, missing API key, missing langchain package, or
    LangChain raised on construction). Callers MUST tolerate ``None`` —
    :func:`call_llm_json` will silently fall back to the urllib path.
    """
    if not callable(getattr(app, "llm", None)):
        return None

    provider, model = _resolve_provider_and_model(app)

    if provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            logger.info(
                "register_default_llm: provider=anthropic but ANTHROPIC_API_KEY missing — "
                "skipping registration",
            )
            return None
        chat = _build_anthropic_chat(api_key, model)
    elif provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        chat = _build_openai_chat(api_key, model)
    else:
        logger.warning("register_default_llm: unknown provider %r — skipping", provider)
        return None

    if chat is None:
        # langchain package for that provider isn't installed.
        return None

    try:
        registered = app.llm(chat)
    except Exception as e:  # noqa: BLE001
        logger.warning("register_default_llm: app.llm() raised (%s) — falling back to urllib", e)
        return None

    _REGISTERED_LLMS[id(app)] = registered
    return registered


def call_llm_json(
    *,
    system: str,
    user: str,
    app: Any | None = None,
    model: str | None = None,
    temperature: float = 0.0,
    timeout: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any] | None:
    """Call an LLM and return its parsed JSON object, or ``None`` on failure.

    Pass ``app=app`` to prefer the Magenta-registered LLM path (gives
    playground tracing). Omit it (or pass ``None``) to go straight to the
    stdlib urllib path.
    """
    # Prefer the runtime-registered LLM so the call is observable.
    if app is not None:
        registered = _REGISTERED_LLMS.get(id(app))
        if registered is not None:
            result = _call_via_runtime_llm(
                registered,
                system=system,
                user=user,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if result is not None:
                return result
            # Runtime call failed (e.g., model returned non-JSON). Fall through.

    # Stdlib urllib fallback.
    return _call_via_urllib(
        system=system,
        user=user,
        model=model,
        temperature=temperature,
        timeout=timeout,
        max_tokens=max_tokens,
    )


def _supports_response_format(runtime: Any) -> bool:
    """Only OpenAI-style chat models accept ``response_format={"type": "json_object"}``.

    Anthropic raises ``Expected code to be unreachable, but got: 'json_object'``
    when this binding is passed. We detect the class name so the same helper
    works for both providers.
    """
    cls_name = type(runtime).__name__.lower()
    return "openai" in cls_name or "azure" in cls_name


def _call_via_runtime_llm(
    runtime: Any,
    *,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int | None,
) -> dict[str, Any] | None:
    """Call a registered LangChain-style runtime LLM, expecting a JSON reply."""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage  # type: ignore[import-not-found]
    except ImportError:
        return None

    # For Anthropic, fold the JSON-only instruction into the system prompt
    # because the response_format bind isn't supported. Claude is very
    # compliant with explicit instructions.
    use_response_format = _supports_response_format(runtime)
    effective_system = system
    if not use_response_format:
        effective_system = (
            system
            + "\n\nIMPORTANT: respond with a SINGLE JSON object only — no "
            "markdown fences, no prose before or after. The response MUST "
            "parse with json.loads()."
        )

    messages = [SystemMessage(content=effective_system), HumanMessage(content=user)]

    # Build bind kwargs only with parameters the runtime supports.
    bound = runtime
    bind_kwargs: dict[str, Any] = {}
    if use_response_format:
        bind_kwargs["response_format"] = {"type": "json_object"}
    if max_tokens is not None:
        bind_kwargs["max_tokens"] = max_tokens
    if bind_kwargs and callable(getattr(runtime, "bind", None)):
        try:
            bound = runtime.bind(**bind_kwargs)
        except Exception:  # noqa: BLE001
            bound = runtime

    try:
        response = bound.invoke(messages)
    except Exception as e:  # noqa: BLE001
        logger.warning("runtime_llm.invoke failed (%s); falling back to urllib", e)
        return None

    content = getattr(response, "content", response)
    if isinstance(content, list):
        # langchain sometimes returns content as a list of message parts
        # (especially Anthropic with mixed text/tool-use blocks).
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or ""))
        content = "".join(parts)
    if not isinstance(content, str):
        return None

    # Anthropic sometimes wraps JSON in a code fence even when asked not to.
    # Strip a single leading ```json ... ``` if present.
    stripped = content.strip()
    if stripped.startswith("```"):
        # remove ``` and optional language tag, then trailing ```
        first_nl = stripped.find("\n")
        if first_nl != -1 and stripped.endswith("```"):
            stripped = stripped[first_nl + 1 : -3].strip()

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as e:
        logger.warning("runtime_llm returned non-JSON content (%s): %s", e, content[:200])
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _call_via_urllib(
    *,
    system: str,
    user: str,
    model: str | None,
    temperature: float,
    timeout: float | None,
    max_tokens: int | None,
) -> dict[str, Any] | None:
    """Stdlib path: POST /chat/completions directly. No Magenta tracing."""
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
