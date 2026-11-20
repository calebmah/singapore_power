"""Custom exceptions for the pysingaporepower library."""

from __future__ import annotations

import unicodedata


class ApiException(Exception):
    """Raised when an API request or network call fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthException(ApiException):
    """Raised when authentication fails or credentials/tokens are invalid."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        *,
        error_code: str = "invalid_auth",
        mfa_token: str | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.error_code = error_code
        self.mfa_token = mfa_token

    @property
    def error_folded(self) -> str:
        """Normalized error code for protocol comparisons."""
        return unicodedata.normalize("NFKC", self.error_code).casefold().strip()


class RateLimitException(ApiException):
    """Raised when the Singapore Power API returns HTTP 429 Too Many Requests."""
