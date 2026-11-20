"""Authentication handler for Singapore Power API (Auth0 password-realm + SMS/Email OOB & TOTP MFA)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
import inspect
import json
import logging
import math
import time
from typing import Any

import aiohttp

from .const import (
    AUDIENCE,
    CLIENT_ID,
    GRANT_TYPE_MFA_OOB,
    GRANT_TYPE_MFA_OTP,
    GRANT_TYPE_PASSWORD,
    GRANT_TYPE_REFRESH_TOKEN,
    JSON_ENCODING,
    LOGIN_RETRY_COOLDOWN_SECONDS,
    MAX_RESPONSE_BYTES,
    MFA_AUTHENTICATORS_URL,
    MFA_CHALLENGE_URL,
    MFA_FALLBACK_AUTHENTICATORS_URL,
    MFA_FALLBACK_CHALLENGE_URL,
    OAUTH_ERROR_MFA_REQUIRED,
    OAUTH_ERROR_TOO_MANY_ATTEMPTS,
    OAUTH_TOKEN_URL,
    REALM,
    SCOPE,
    TRANSPORT_ERROR_CHARS,
    USER_AGENT,
)
from .exceptions import ApiException, AuthException
from .models import AuthenticatorOption, LoginChallengeResult
from .utils import decode_jwt_exp, fold_text, safe_log_text

_LOGGER = logging.getLogger(__name__)

_EXPIRY_BUFFER_SECONDS = 300

TokenUpdateCallback = Callable[[dict[str, Any]], Awaitable[None] | None]

# Process-wide per-username login cooldown & single-flight guard against Auth0 bot lockouts
_LOGIN_FAILURES: dict[str, float] = {}
_LOGIN_IN_FLIGHT: set[str] = set()


def _claim_login_slot(username: str) -> None:
    """Claim single-flight login slot for username or raise AuthException if throttled."""
    folded = fold_text(username)
    now_mono = time.monotonic()
    failed_at = _LOGIN_FAILURES.get(folded)
    if failed_at is not None:
        remaining = LOGIN_RETRY_COOLDOWN_SECONDS - (now_mono - failed_at)
        if remaining > 0:
            raise AuthException(
                f"Previous sign-in failed; retry in {math.ceil(remaining)}s",
                error_code=OAUTH_ERROR_TOO_MANY_ATTEMPTS,
            )
    if folded in _LOGIN_IN_FLIGHT:
        raise AuthException(
            "Another sign-in for this account is already in progress",
            error_code=OAUTH_ERROR_TOO_MANY_ATTEMPTS,
        )
    _LOGIN_IN_FLIGHT.add(folded)


def _release_login_slot(username: str) -> None:
    _LOGIN_IN_FLIGHT.discard(fold_text(username))


def _record_login_failure(username: str) -> None:
    now_mono = time.monotonic()
    for key, ts in tuple(_LOGIN_FAILURES.items()):
        if now_mono - ts >= LOGIN_RETRY_COOLDOWN_SECONDS:
            del _LOGIN_FAILURES[key]
    _LOGIN_FAILURES[fold_text(username)] = now_mono


def _clear_login_failure(username: str) -> None:
    _LOGIN_FAILURES.pop(fold_text(username), None)


class SingaporePowerAuth:
    """Handles Auth0 login, SMS/Email/TOTP MFA, cooldowns, and mutex-protected token refresh."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str | None = None,
        *,
        access_token: str | None = None,
        id_token: str | None = None,
        refresh_token: str | None = None,
        token_expiry: str | float | datetime | None = None,
        token_update_callback: TokenUpdateCallback | None = None,
    ) -> None:
        """Initialize the auth handler."""
        self._session = session
        self._username = username
        self._password = password
        self._access_token = access_token
        self._id_token = id_token
        self._refresh_token = refresh_token
        self._token_expiry = self._coerce_expiry(token_expiry, access_token)
        self._token_update_callback = token_update_callback
        self._refresh_lock = asyncio.Lock()

        self._mfa_token: str | None = None
        self._oob_code: str | None = None
        self._authenticators: list[AuthenticatorOption] = []

    @staticmethod
    def _auth0_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "Accept-Language": "en_US",
            "User-Agent": USER_AGENT,
        }
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _coerce_expiry(
        raw_expiry: str | float | datetime | None, access_token: str | None
    ) -> datetime:
        """Parse persisted token expiry or derive it from the JWT exp claim."""
        if isinstance(raw_expiry, datetime):
            return (
                raw_expiry.replace(tzinfo=UTC)
                if raw_expiry.tzinfo is None
                else raw_expiry.astimezone(UTC)
            )
        if isinstance(raw_expiry, (int, float)) and raw_expiry > 0:
            return datetime.fromtimestamp(float(raw_expiry), tz=UTC)
        if isinstance(raw_expiry, str) and raw_expiry:
            try:
                parsed = datetime.fromisoformat(raw_expiry)
                return (
                    parsed.replace(tzinfo=UTC)
                    if parsed.tzinfo is None
                    else parsed.astimezone(UTC)
                )
            except ValueError:
                pass

        jwt_exp = decode_jwt_exp(access_token)
        if jwt_exp:
            return datetime.fromtimestamp(jwt_exp - _EXPIRY_BUFFER_SECONDS, tz=UTC)

        return datetime.fromtimestamp(0, tz=UTC)

    def export_tokens(self) -> dict[str, Any]:
        """Export current OAuth tokens for persistence in ConfigEntry.data (no password)."""
        return {
            "access_token": self._access_token,
            "id_token": self._id_token,
            "refresh_token": self._refresh_token,
            "token_expiry": self._token_expiry.isoformat(),
        }

    def get_id_token(self) -> str | None:
        """Return the stored OIDC ID token (sent as X-id-token header)."""
        return self._id_token

    @property
    def refresh_token(self) -> str | None:
        """Return the current refresh token."""
        return self._refresh_token

    async def _notify_token_updated(self) -> None:
        """Invoke optional callback when tokens are obtained or refreshed."""
        if self._token_update_callback is None:
            return
        try:
            res = self._token_update_callback(self.export_tokens())
            if inspect.isawaitable(res):
                await res
        except Exception:
            _LOGGER.exception("Failed executing token_update_callback")

    async def _process_token_response(self, token_data: dict[str, Any]) -> None:
        """Store access, ID, and refresh tokens and notify persistence callback."""
        access_token = token_data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise AuthException("Token response did not contain a valid access_token")

        self._access_token = access_token
        if isinstance(token_data.get("id_token"), str) and token_data["id_token"]:
            self._id_token = token_data["id_token"]
        if isinstance(token_data.get("refresh_token"), str) and token_data["refresh_token"]:
            self._refresh_token = token_data["refresh_token"]

        jwt_exp = decode_jwt_exp(self._access_token)
        if jwt_exp:
            self._token_expiry = datetime.fromtimestamp(
                jwt_exp - _EXPIRY_BUFFER_SECONDS, tz=UTC
            )
        else:
            expires_in = int(token_data.get("expires_in", 3600))
            self._token_expiry = datetime.now(UTC) + timedelta(
                seconds=max(60, expires_in - _EXPIRY_BUFFER_SECONDS)
            )

        _LOGGER.debug("Processed SP tokens; next refresh after %s", self._token_expiry)
        await self._notify_token_updated()

    async def async_login_step1(self) -> LoginChallengeResult:
        """Start password-realm login with cooldown protection and MFA discovery."""
        if not self._password:
            raise AuthException("Password is required for interactive login.")

        _claim_login_slot(self._username)
        try:
            payload = {
                "client_id": CLIENT_ID,
                "scope": SCOPE,
                "username": self._username,
                "password": self._password,
                "grant_type": GRANT_TYPE_PASSWORD,
                "realm": REALM,
                "audience": AUDIENCE,
            }

            try:
                async with self._session.post(
                    OAUTH_TOKEN_URL,
                    json=payload,
                    headers=self._auth0_headers(),
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    data = await self._read_bounded_json(resp)
                    status = resp.status
            except (TimeoutError, aiohttp.ClientError) as err:
                raise ApiException(
                    f"Network error contacting Auth0: {safe_log_text(err, TRANSPORT_ERROR_CHARS)}"
                ) from err

            if status == 200 and isinstance(data, dict) and "access_token" in data:
                _clear_login_failure(self._username)
                await self._process_token_response(data)
                return LoginChallengeResult(authenticated=True, mfa_required=False)

            detail = data if isinstance(data, dict) else {}
            err_code = str(detail.get("error") or detail.get("code") or "invalid_auth")
            err_folded = fold_text(err_code)

            if status == 403 and err_folded == OAUTH_ERROR_MFA_REQUIRED:
                mfa_token = detail.get("mfa_token")
                if not isinstance(mfa_token, str) or not mfa_token:
                    raise AuthException("MFA required but mfa_token missing in response.")
                self._mfa_token = mfa_token
                return await self._discover_and_prepare_mfa()

            error_desc = safe_log_text(
                detail.get("error_description")
                or detail.get("description")
                or err_code
                or f"HTTP {status}"
            )
            if status in (400, 401, 403):
                _record_login_failure(self._username)
                raise AuthException(
                    f"Authentication failed: {error_desc}",
                    status_code=status,
                    error_code=err_code,
                )
            raise ApiException(
                f"Auth0 token endpoint returned HTTP {status}: {error_desc}",
                status_code=status,
            )
        finally:
            _release_login_slot(self._username)

    async def _discover_and_prepare_mfa(self) -> LoginChallengeResult:
        """Fetch active MFA authenticators (handling bare list or dict envelope, TOTP, SMS & Email)."""
        if not self._mfa_token:
            raise AuthException("Missing mfa_token for authenticator discovery.")

        headers = self._auth0_headers({"Authorization": f"Bearer {self._mfa_token}"})
        raw_payload: Any = None
        for url in (MFA_AUTHENTICATORS_URL, MFA_FALLBACK_AUTHENTICATORS_URL):
            try:
                async with self._session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        raw_payload = await self._read_bounded_json(resp)
                        break
            except (TimeoutError, aiohttp.ClientError):
                continue

        # Handle both {"authenticators": [...]} and bare [...] responses
        rows: list[Any] = []
        if isinstance(raw_payload, dict) and isinstance(raw_payload.get("authenticators"), list):
            rows = raw_payload["authenticators"]
        elif isinstance(raw_payload, list):
            rows = raw_payload

        self._authenticators = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            if item.get("active") is False or not item.get("id"):
                continue
            auth_type = fold_text(str(item.get("authenticator_type") or item.get("type") or ""))
            if auth_type == "recovery-code":
                continue
            oob_channel_raw = item.get("oob_channel")
            oob_channel = fold_text(str(oob_channel_raw)) if oob_channel_raw else None
            self._authenticators.append(
                AuthenticatorOption(
                    authenticator_id=str(item["id"]),
                    auth_type=auth_type,
                    oob_channel=oob_channel,
                    name=str(item["name"]) if item.get("name") else None,
                    active=True,
                )
            )

        has_totp = any(a.auth_type in ("otp", "totp") for a in self._authenticators)
        sms_auth = next(
            (a for a in self._authenticators if a.auth_type == "oob" and a.oob_channel == "sms"),
            None,
        )
        email_auth = next(
            (a for a in self._authenticators if a.auth_type == "oob" and a.oob_channel == "email"),
            None,
        )

        methods: list[str] = []
        if has_totp:
            methods.append("otp")
        if sms_auth is not None:
            methods.append("oob_sms")
        if email_auth is not None:
            methods.append("oob_email")

        # Fallback if authenticator discovery failed or returned unknown shapes: allow TOTP entry
        if not methods:
            _LOGGER.warning("No standard MFA authenticators discovered; defaulting to TOTP input")
            return LoginChallengeResult(
                authenticated=False,
                mfa_required=True,
                selected_method="otp",
                available_methods=["otp"],
            )

        if len(methods) == 1:
            sole = methods[0]
            dest: str | None = None
            if sole == "oob_sms" and sms_auth is not None:
                await self.async_trigger_oob_challenge(sms_auth.authenticator_id)
                dest = sms_auth.name
            elif sole == "oob_email" and email_auth is not None:
                await self.async_trigger_oob_challenge(email_auth.authenticator_id)
                dest = email_auth.name
            return LoginChallengeResult(
                authenticated=False,
                mfa_required=True,
                selected_method=sole,
                available_methods=methods,
                authenticators=self._authenticators,
                masked_destination=dest,
            )

        dest = sms_auth.name if sms_auth else (email_auth.name if email_auth else None)
        return LoginChallengeResult(
            authenticated=False,
            mfa_required=True,
            selected_method=None,
            available_methods=methods,
            authenticators=self._authenticators,
            masked_destination=dest,
        )

    async def async_trigger_oob_challenge(
        self,
        authenticator_id: str | None = None,
        channel: str = "sms",
    ) -> str:
        """Trigger an Auth0 OOB (SMS or Email) challenge and verify binding_method == 'prompt'."""
        if not self._mfa_token:
            raise AuthException("Cannot trigger OOB challenge without mfa_token.")

        if not authenticator_id:
            target = next(
                (
                    a
                    for a in self._authenticators
                    if a.auth_type == "oob" and (a.oob_channel == channel or a.oob_channel in ("sms", "email"))
                ),
                None,
            )
            if target is None:
                raise AuthException(f"No {channel.upper()} MFA authenticator available.")
            authenticator_id = target.authenticator_id

        payload = {
            "client_id": CLIENT_ID,
            "mfa_token": self._mfa_token,
            "challenge_type": "oob",
            "authenticator_id": authenticator_id,
        }
        headers = self._auth0_headers({"Authorization": f"Bearer {self._mfa_token}"})

        last_err: Exception | None = None
        for url in (MFA_CHALLENGE_URL, MFA_FALLBACK_CHALLENGE_URL):
            try:
                async with self._session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    data = await self._read_bounded_json(resp)
                    if resp.status == 200 and isinstance(data, dict) and data.get("oob_code"):
                        binding = fold_text(str(data.get("binding_method") or "prompt"))
                        if binding != "prompt":
                            raise AuthException(
                                f"Unsupported Auth0 MFA binding_method: {binding}",
                                error_code="challenge_failed",
                            )
                        self._oob_code = str(data["oob_code"])
                        return self._oob_code
                    last_err = ApiException(
                        f"MFA challenge returned HTTP {resp.status}",
                        status_code=resp.status,
                    )
            except AuthException:
                raise
            except (TimeoutError, aiohttp.ClientError) as err:
                last_err = ApiException(
                    f"Network error challenging MFA: {safe_log_text(err, TRANSPORT_ERROR_CHARS)}"
                )

        raise last_err or ApiException("Failed to trigger MFA challenge")

    # Backwards-compatible alias
    async def async_trigger_sms_challenge(
        self, authenticator_id: str | None = None
    ) -> str:
        """Trigger SMS OOB challenge."""
        return await self.async_trigger_oob_challenge(authenticator_id, channel="sms")

    async def async_submit_mfa(self, code: str, method: str = "oob_sms") -> None:
        """Submit TOTP ('otp') or SMS/Email OOB ('oob' / 'oob_sms' / 'oob_email') code."""
        if not self._mfa_token:
            raise AuthException("Must initiate login before submitting MFA code.")

        cleaned_code = "".join(code.strip().split())
        if method == "otp":
            payload: dict[str, Any] = {
                "client_id": CLIENT_ID,
                "grant_type": GRANT_TYPE_MFA_OTP,
                "mfa_token": self._mfa_token,
                "otp": cleaned_code,
            }
        else:
            channel = "email" if method == "oob_email" else "sms"
            if not self._oob_code:
                await self.async_trigger_oob_challenge(channel=channel)
            payload = {
                "client_id": CLIENT_ID,
                "grant_type": GRANT_TYPE_MFA_OOB,
                "mfa_token": self._mfa_token,
                "oob_code": self._oob_code,
                "binding_code": cleaned_code,
            }

        try:
            async with self._session.post(
                OAUTH_TOKEN_URL,
                json=payload,
                headers=self._auth0_headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await self._read_bounded_json(resp)
                detail = data if isinstance(data, dict) else {}
                if resp.status != 200:
                    err_code = str(detail.get("error") or "invalid_otp")
                    desc = safe_log_text(
                        detail.get("error_description") or err_code or f"HTTP {resp.status}"
                    )
                    if resp.status in (400, 401, 403):
                        raise AuthException(
                            f"MFA verification failed: {desc}",
                            status_code=resp.status,
                            error_code=err_code,
                        )
                    raise ApiException(
                        f"MFA token request failed: {desc}", status_code=resp.status
                    )
                _clear_login_failure(self._username)
                await self._process_token_response(detail)
        except (TimeoutError, aiohttp.ClientError) as err:
            raise ApiException(
                f"Network error submitting MFA code: {safe_log_text(err, TRANSPORT_ERROR_CHARS)}"
            ) from err

    async def async_request_otp(self) -> bool:
        """Backwards-compatible wrapper around async_login_step1()."""
        result = await self.async_login_step1()
        if result.mfa_required and result.selected_method is None:
            if "oob_sms" in result.available_methods:
                await self.async_trigger_oob_challenge(channel="sms")
            elif "oob_email" in result.available_methods:
                await self.async_trigger_oob_challenge(channel="email")
        return result.mfa_required

    async def async_submit_otp(self, otp_code: str) -> None:
        """Backwards-compatible wrapper around async_submit_mfa()."""
        method = "oob_sms" if self._oob_code else "otp"
        await self.async_submit_mfa(otp_code, method=method)

    async def async_force_refresh(self) -> str:
        """Force a token refresh using the stored refresh_token."""
        async with self._refresh_lock:
            await self._refresh_token_unlocked()
            assert self._access_token is not None
            return self._access_token

    async def _refresh_token_unlocked(self) -> None:
        """Refresh the access token using the refresh token (caller must hold _refresh_lock)."""
        if not self._refresh_token:
            raise AuthException("No refresh token available. Re-authentication required.")

        payload = {
            "client_id": CLIENT_ID,
            "grant_type": GRANT_TYPE_REFRESH_TOKEN,
            "refresh_token": self._refresh_token,
            "scope": SCOPE,
        }
        try:
            async with self._session.post(
                OAUTH_TOKEN_URL,
                json=payload,
                headers=self._auth0_headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await self._read_bounded_json(resp)
                detail = data if isinstance(data, dict) else {}
                if resp.status != 200:
                    err_code = str(detail.get("error") or "invalid_grant")
                    desc = safe_log_text(
                        detail.get("error_description") or err_code or f"HTTP {resp.status}"
                    )
                    if resp.status in (400, 401, 403):
                        raise AuthException(
                            f"Refresh token expired or revoked: {desc}",
                            status_code=resp.status,
                            error_code=err_code,
                        )
                    raise ApiException(
                        f"Transient error refreshing token: {desc}",
                        status_code=resp.status,
                    )
                await self._process_token_response(detail)
        except (TimeoutError, aiohttp.ClientError) as err:
            raise ApiException(
                f"Network error refreshing token: {safe_log_text(err, TRANSPORT_ERROR_CHARS)}"
            ) from err

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing with refresh_token when needed."""
        now = datetime.now(UTC)
        if self._access_token and now < self._token_expiry:
            return self._access_token

        async with self._refresh_lock:
            now = datetime.now(UTC)
            if self._access_token and now < self._token_expiry:
                return self._access_token

            if self._refresh_token:
                await self._refresh_token_unlocked()
                assert self._access_token is not None
                return self._access_token

            if self._access_token:
                return self._access_token

            raise AuthException("Not authenticated. Please authenticate first.")

    @staticmethod
    async def _read_bounded_json(resp: aiohttp.ClientResponse) -> Any:
        """Read up to MAX_RESPONSE_BYTES and decode UTF-8-SIG JSON safely."""
        raw_bytes = await resp.content.read(MAX_RESPONSE_BYTES + 1)
        if len(raw_bytes) > MAX_RESPONSE_BYTES:
            raise ApiException(f"Auth0 response exceeded {MAX_RESPONSE_BYTES} bytes")
        if not raw_bytes:
            return {}
        text = raw_bytes.decode(JSON_ENCODING, errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": safe_log_text(text)}
