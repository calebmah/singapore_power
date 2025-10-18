"""Custom exceptions for the pysingaporepower library."""

class ApiException(Exception):
    """Raised when an API request fails."""
    pass

class AuthException(ApiException):
    """Raised when an authentication request fails."""
    pass
