"""Sync clients for invoking Magenta agents from MergeGuard.

Two clients:

- :class:`OEClient` — hits a local-dev Orchestration Engine container's
  ``POST /invoke`` endpoint. Used when the agent stack is brought up via
  ``agentic dev up --all`` and each agent has its own OE service.

- :class:`CloudOEClient` — hits the Magenta tenant API's
  ``POST /api/v1/invokeWorkspaceStream`` SSE endpoint. Used when agents are
  deployed to a Magenta workspace in the cloud.

Both expose ``.invoke(agent_id, payload, *, thread_id) -> InvokeResult``.
The :mod:`packages.orchestration.platform_factory` module wraps either of
these and adapts the result to the legacy ``LocalPlatformClient.invoke``
return dict shape (``{execution_id, thread_id, agent_id, status, result}``).
"""

from packages.magenta_client.client import OEClient, OEError, InvokeResult, InvokeStatus
from packages.magenta_client.cloud_client import CloudOEClient, CloudCredentials

__all__ = [
    "OEClient",
    "OEError",
    "InvokeResult",
    "InvokeStatus",
    "CloudOEClient",
    "CloudCredentials",
]
