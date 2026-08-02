"""
MSAL client-credentials auth for Power BI Admin APIs.
"""

import logging
import time
from typing import Optional

import msal

from catalog_service import catalog_config as config

logger = logging.getLogger(__name__)


class PowerBIAuth:
    """Acquire and refresh app-only tokens for Power BI."""

    def __init__(
        self,
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ):
        self.tenant_id = tenant_id or config.TENANT_ID
        self.client_id = client_id or config.CLIENT_ID
        self.client_secret = client_secret or config.CLIENT_SECRET
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._app: Optional[msal.ConfidentialClientApplication] = None

    def _get_app(self) -> msal.ConfidentialClientApplication:
        if self._app is None:
            authority = f"https://login.microsoftonline.com/{self.tenant_id}"
            self._app = msal.ConfidentialClientApplication(
                self.client_id,
                authority=authority,
                client_credential=self.client_secret,
            )
        return self._app

    def get_token(self, force_refresh: bool = False) -> str:
        # Refresh 5 min before expiry
        if (
            not force_refresh
            and self._token
            and time.time() < (self._expires_at - 300)
        ):
            return self._token

        logger.info("Acquiring Power BI access token (client credentials)...")
        result = self._get_app().acquire_token_for_client(scopes=config.PBI_SCOPE)

        if "access_token" not in result:
            error = result.get("error", "unknown")
            desc = result.get("error_description", "")
            raise RuntimeError(f"Auth failed: {error} — {desc}")

        self._token = result["access_token"]
        # expires_in is seconds from now
        self._expires_at = time.time() + int(result.get("expires_in", 3600))
        logger.info("Access token acquired (expires_in=%ss)", result.get("expires_in"))
        return self._token

    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "Content-Type": "application/json",
        }
