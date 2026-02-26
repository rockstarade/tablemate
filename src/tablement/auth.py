"""Authentication and credential management via OS keyring."""

from __future__ import annotations

import logging

import keyring

from tablement.api import ResyApiClient
from tablement.errors import AuthError
from tablement.models import ResyCredentials

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "tablement-resy"


class AuthManager:
    """Handles credential storage (macOS Keychain) and login."""

    def store_credentials(self, email: str, password: str) -> None:
        """Store credentials in the OS keyring."""
        keyring.set_password(KEYRING_SERVICE, "email", email)
        keyring.set_password(KEYRING_SERVICE, email, password)
        logger.info("Credentials stored in keyring.")

    def load_credentials(self) -> ResyCredentials:
        """Load credentials from the OS keyring."""
        email = keyring.get_password(KEYRING_SERVICE, "email")
        if not email:
            raise AuthError("No credentials found. Run 'tablement configure' first.")
        password = keyring.get_password(KEYRING_SERVICE, email)
        if not password:
            raise AuthError(f"Password not found for {email}. Run 'tablement configure' again.")
        return ResyCredentials(email=email, password=password)

    async def login(
        self, client: ResyApiClient, credentials: ResyCredentials
    ) -> tuple[str, int]:
        """
        Authenticate with Resy and return (auth_token, payment_method_id).

        Should be called before the snipe window to get a fresh token.
        """
        resp = await client.authenticate(
            email=credentials.email,
            password=credentials.password.get_secret_value(),
        )
        if not resp.payment_methods:
            raise AuthError(
                "No payment methods on account. Add a card at resy.com first."
            )
        # Use the default payment method, or fall back to the first one
        pm = next(
            (p for p in resp.payment_methods if p.is_default),
            resp.payment_methods[0],
        )
        logger.info(
            "Authenticated as %s %s (payment: %s)",
            resp.first_name,
            resp.last_name,
            pm.display or f"id={pm.id}",
        )
        return resp.token, pm.id
