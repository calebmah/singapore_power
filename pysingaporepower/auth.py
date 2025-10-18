"""Authentication handler for Singapore Power API."""

import aiohttp
import logging
from datetime import datetime, timedelta

from .const import (
    AUDIENCE,
    CLIENT_ID,
    GRANT_TYPE_MFA_OOB,
    GRANT_TYPE_PASSWORD,
    MFA_AUTHENTICATORS_URL,
    MFA_CHALLENGE_URL,
    OAUTH_TOKEN_URL,
    REALM,
    SCOPE,
)
from .exceptions import AuthException, ApiException

_LOGGER = logging.getLogger(__name__)

class SingaporePowerAuth:
    """Handles the multi-step MFA authentication for the Singapore Power API."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str):
        """Initialize the auth handler."""
        self._session = session
        self._username = username
        self._password = password
        
        self._access_token = None
        self._id_token = None
        self._refresh_token = None
        self._token_expiry = datetime.now()
        self._mfa_token = None
        self._oob_code = None

    def _process_token_response(self, token_data: dict):
        """Process a successful token response to store tokens and expiry."""
        _LOGGER.debug("Processing token response")
        self._access_token = token_data["access_token"]
        self._id_token = token_data.get("id_token")
        
        if "refresh_token" in token_data:
            self._refresh_token = token_data["refresh_token"]
            _LOGGER.debug("Stored new refresh token")
        
        expires_in = token_data.get("expires_in", 3600)
        self._token_expiry = datetime.now() + timedelta(seconds=expires_in - 600)
        _LOGGER.info("Successfully processed and stored tokens. New expiry: %s", self._token_expiry)

    def get_id_token(self) -> str | None:
        """Return the stored ID token."""
        return self._id_token

    async def async_request_otp(self) -> bool:
        """Start the login process, handle the MFA requirement, and trigger the OTP."""
        _LOGGER.info("Auth Step 1: Starting authentication for %s", self._username)
        
        payload = {
            "client_id": CLIENT_ID,
            "scope": SCOPE,
            "username": self._username,
            "password": self._password,
            "grant_type": GRANT_TYPE_PASSWORD,
            "realm": REALM,
            "audience": AUDIENCE,
        }
        
        _LOGGER.debug("Requesting initial token for MFA")
        async with self._session.post(OAUTH_TOKEN_URL, json=payload) as resp:
            data = await resp.json()
            if resp.status == 403 and data.get("error") == "mfa_required":
                self._mfa_token = data["mfa_token"]
                _LOGGER.debug("MFA is required. Got mfa_token.")
            else:
                _LOGGER.error("Expected MFA challenge, but got status %s: %s", resp.status, data)
                raise AuthException(f"Expected MFA challenge, but got status {resp.status}")

        _LOGGER.debug("Getting available authenticators")
        headers = {"Authorization": f"Bearer {self._mfa_token}"}
        async with self._session.get(MFA_AUTHENTICATORS_URL, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to get authenticators: %s", resp.status)
                raise ApiException(f"Failed to get authenticators: {resp.status}")
            authenticators = await resp.json()
        
        sms_authenticator = next((a for a in authenticators if a.get("oob_channel") == "sms"), None)
        if not sms_authenticator:
            _LOGGER.error("No SMS authenticator found in the list")
            raise AuthException("No SMS authenticator found.")
        
        _LOGGER.debug("Found SMS authenticator: %s", sms_authenticator["id"])
        payload = {
            "client_id": CLIENT_ID,
            "mfa_token": self._mfa_token,
            "challenge_type": "oob",
            "authenticator_id": sms_authenticator["id"],
        }
        
        _LOGGER.debug("Challenging MFA to trigger OTP")
        async with self._session.post(MFA_CHALLENGE_URL, json=payload) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to challenge MFA: %s", resp.status)
                raise ApiException(f"Failed to challenge MFA: {resp.status}")
            self._oob_code = (await resp.json())["oob_code"]
            _LOGGER.info("Successfully challenged MFA and received oob_code. OTP sent.")
        
        return True

    async def async_submit_otp(self, otp_code: str):
        """Submit the OTP to complete login and get access/refresh tokens."""
        if not self._mfa_token or not self._oob_code:
            _LOGGER.error("Attempted to submit OTP without a valid mfa_token or oob_code")
            raise AuthException("Must call async_request_otp() before submitting OTP.")

        _LOGGER.info("Auth Step 2: Submitting OTP")
        payload = {
            "client_id": CLIENT_ID,
            "grant_type": GRANT_TYPE_MFA_OOB,
            "mfa_token": self._mfa_token,
            "oob_code": self._oob_code,
            "binding_code": otp_code,
        }

        async with self._session.post(OAUTH_TOKEN_URL, json=payload) as resp:
            if resp.status != 200:
                _LOGGER.error("OTP submission failed: %s %s", resp.status, await resp.text())
                raise AuthException(f"OTP submission failed: {resp.status}")
            self._process_token_response(await resp.json())
            _LOGGER.info("Authentication successful!")

    async def _refresh_token(self):
        """Refresh the access token using the refresh token."""
        _LOGGER.info("Refreshing access token")
        payload = {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "scope": SCOPE,
        }
        async with self._session.post(OAUTH_TOKEN_URL, json=payload) as resp:
            if resp.status != 200:
                _LOGGER.error("Token refresh failed: %s %s", resp.status, await resp.text())
                raise AuthException(f"Token refresh failed: {resp.status}")
            self._process_token_response(await resp.json())
            _LOGGER.info("Token refresh successful")

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing if necessary."""
        if not self._access_token:
            _LOGGER.warning("Access token requested but not available")
            raise AuthException("Not authenticated. Call login methods first.")
        
        if datetime.now() >= self._token_expiry:
            _LOGGER.debug("Access token expired, initiating refresh")
            await self._refresh_token()
            
        return self._access_token