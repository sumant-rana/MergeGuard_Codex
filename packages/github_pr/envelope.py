"""Normalize a verified GitHub webhook into a stable envelope shape."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class WebhookEnvelope:
    delivery_id: str
    event: str
    action: str
    repo: str
    installation_id: int | None
    sender_login: str | None
    raw_payload: dict[str, Any]
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["received_at"] = self.received_at.isoformat()
        return d


def build_envelope(
    delivery_id: str,
    event: str,
    raw_payload: dict[str, Any],
    received_at: datetime | None = None,
) -> WebhookEnvelope:
    """Build a :class:`WebhookEnvelope` from a verified GitHub webhook payload.

    ``delivery_id`` (the ``X-GitHub-Delivery`` header) is used as the
    idempotency key — replays from GitHub will have the same value.
    """
    repository = raw_payload.get("repository") or {}
    repo = repository.get("full_name", "") if isinstance(repository, dict) else ""

    action = raw_payload.get("action", "") or ""

    installation = raw_payload.get("installation") or {}
    installation_id = installation.get("id") if isinstance(installation, dict) else None

    sender = raw_payload.get("sender") or {}
    sender_login = sender.get("login") if isinstance(sender, dict) else None

    return WebhookEnvelope(
        delivery_id=delivery_id,
        event=event,
        action=action,
        repo=repo,
        installation_id=installation_id,
        sender_login=sender_login,
        raw_payload=raw_payload,
        received_at=received_at or datetime.now(timezone.utc),
    )
