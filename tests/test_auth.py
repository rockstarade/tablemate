"""Tests for authentication and credential management."""

from unittest.mock import MagicMock, patch

import pytest

from tablement.auth import KEYRING_SERVICE, AuthManager
from tablement.errors import AuthError


class TestAuthManager:
    def setup_method(self):
        self.auth = AuthManager()

    @patch("tablement.auth.keyring")
    def test_store_credentials(self, mock_keyring):
        self.auth.store_credentials("test@example.com", "password123")

        mock_keyring.set_password.assert_any_call(
            KEYRING_SERVICE, "email", "test@example.com"
        )
        mock_keyring.set_password.assert_any_call(
            KEYRING_SERVICE, "test@example.com", "password123"
        )

    @patch("tablement.auth.keyring")
    def test_load_credentials_success(self, mock_keyring):
        mock_keyring.get_password.side_effect = lambda svc, key: {
            "email": "test@example.com",
            "test@example.com": "password123",
        }.get(key)

        creds = self.auth.load_credentials()

        assert creds.email == "test@example.com"
        assert creds.password.get_secret_value() == "password123"

    @patch("tablement.auth.keyring")
    def test_load_credentials_no_email(self, mock_keyring):
        mock_keyring.get_password.return_value = None

        with pytest.raises(AuthError, match="No credentials found"):
            self.auth.load_credentials()

    @patch("tablement.auth.keyring")
    def test_load_credentials_no_password(self, mock_keyring):
        mock_keyring.get_password.side_effect = lambda svc, key: {
            "email": "test@example.com",
        }.get(key)

        with pytest.raises(AuthError, match="Password not found"):
            self.auth.load_credentials()
