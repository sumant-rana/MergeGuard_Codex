"""GitHub App authentication: App JWT + per-installation access tokens.

A GitHub App calls the API either as itself (10-minute App JWT, RS256-signed
with the App's private key) or as one of its installations (1-hour access
token). Repo-scoped calls (read PR files, post comments, post check runs)
require an installation token. Tokens are cached by ``installation_id``.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


class GitHubAuthError(Exception):
    """Raised when the GitHub App cannot mint a JWT or installation token."""


@dataclass
class InstallationToken:
    token: str
    expires_at: datetime
    installation_id: int

    def is_valid(self, buffer_seconds: int = 60) -> bool:
        return datetime.now(timezone.utc) + timedelta(seconds=buffer_seconds) < self.expires_at


class GitHubAppAuth:
    """Mint App JWTs and per-installation access tokens.

    Usage::

        auth = GitHubAppAuth(app_id=123456, private_key_pem=open("key.pem").read())
        token = auth.get_installation_token(installation_id=99)
        # Use ``token.token`` as the Bearer auth on REST calls.
    """

    GITHUB_API = "https://api.github.com"
    JWT_TTL_SECONDS = 600  # GitHub allows up to 10 minutes
    JWT_LEEWAY_SECONDS = 60

    def __init__(
        self,
        app_id: int,
        private_key_pem: str,
        *,
        api_base_url: str | None = None,
    ) -> None:
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._api_base_url = (api_base_url or self.GITHUB_API).rstrip("/")
        self._token_cache: dict[int, InstallationToken] = {}

    def make_app_jwt(self) -> str:
        """Mint a 10-minute JWT signed by the App's private key.

        PyJWT is imported lazily so the rest of the package stays importable
        in environments that don't have it installed.
        """
        try:
            import jwt  # type: ignore[import-not-found]
        except ImportError as e:
            raise GitHubAuthError(
                "PyJWT is required for GitHub App authentication. "
                "Install with: pip install 'pyjwt[crypto]'"
            ) from e

        now = int(time.time())
        payload = {
            "iat": now - self.JWT_LEEWAY_SECONDS,
            "exp": now + self.JWT_TTL_SECONDS,
            "iss": str(self._app_id),
        }
        try:
            return jwt.encode(payload, self._private_key_pem, algorithm="RS256")
        except Exception as e:
            raise GitHubAuthError(f"Failed to mint App JWT: {e}") from e

    def get_installation_token(
        self,
        installation_id: int,
        *,
        force_refresh: bool = False,
    ) -> InstallationToken:
        """Return a cached or freshly minted installation token."""
        cached = self._token_cache.get(installation_id)
        if cached and cached.is_valid() and not force_refresh:
            return cached

        url = f"{self._api_base_url}/app/installations/{installation_id}/access_tokens"
        request = urllib.request.Request(
            url,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.make_app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                status = response.status
        except urllib.error.HTTPError as e:
            raise GitHubAuthError(
                f"GitHub returned {e.code} on access_tokens: {e.read().decode('utf-8', 'replace')[:300]}"
            ) from e
        except urllib.error.URLError as e:
            raise GitHubAuthError(f"Network failure minting installation token: {e}") from e

        if status != 201:
            raise GitHubAuthError(f"GitHub returned {status} on access_tokens: {body[:300]}")

        data = json.loads(body)
        token = data.get("token")
        expires_at_str = data.get("expires_at")
        if not token or not expires_at_str:
            raise GitHubAuthError(f"Missing token/expires_at in GitHub response: {data}")

        expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        installation_token = InstallationToken(
            token=token,
            expires_at=expires_at,
            installation_id=installation_id,
        )
        self._token_cache[installation_id] = installation_token
        return installation_token


def load_app_auth_from_env() -> GitHubAppAuth | None:
    """Build a :class:`GitHubAppAuth` from environment, or return None if not usable.

    Reads ``GITHUB_APP_ID`` and ``GITHUB_APP_PRIVATE_KEY_PATH``. Returns None
    (and logs a warning) when:
      - either env var is missing/empty
      - the path is the documented placeholder (``/absolute/path/...``)
      - the key file does not exist or is unreadable
      - the App ID isn't a valid integer

    Callers can then fall back to ``GITHUB_TOKEN`` or fixture mode without
    surfacing a hard error.
    """
    import logging
    import os

    log = logging.getLogger(__name__)

    app_id_raw = os.environ.get("GITHUB_APP_ID", "").strip()
    key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
    if not app_id_raw or not key_path:
        return None

    # Treat the documented placeholder as "not set" so users can copy .env.example verbatim.
    if key_path.startswith("/absolute/path/"):
        log.warning(
            "GITHUB_APP_PRIVATE_KEY_PATH is the placeholder from .env.example "
            "(%r) — ignoring and falling back to GITHUB_TOKEN.",
            key_path,
        )
        return None

    try:
        app_id = int(app_id_raw)
    except ValueError:
        log.warning(
            "GITHUB_APP_ID is not an integer (%r) — ignoring and falling back to GITHUB_TOKEN.",
            app_id_raw,
        )
        return None

    try:
        with open(key_path) as f:
            private_key_pem = f.read()
    except OSError as e:
        log.warning(
            "Cannot read GITHUB_APP_PRIVATE_KEY_PATH %r (%s) — ignoring and "
            "falling back to GITHUB_TOKEN.",
            key_path,
            e,
        )
        return None

    return GitHubAppAuth(app_id=app_id, private_key_pem=private_key_pem)
