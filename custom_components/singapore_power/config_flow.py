"""Config flow and options flow for the Singapore Power integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ELECTRICITY_PRICE,
    CONF_ENABLE_EXTENDED_INSIGHTS,
    CONF_ENABLE_RECORDER_STATS,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_TOKEN_EXPIRY,
    DEFAULT_ENABLE_EXTENDED_INSIGHTS,
    DEFAULT_ENABLE_RECORDER_STATS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
    parse_electricity_price,
    parse_electricity_price_input,
)
from .pysingaporepower import (
    ApiException,
    AuthException,
    LoginChallengeResult,
    SingaporePowerAuth,
)
from .pysingaporepower.const import (
    OAUTH_ERROR_REQUIRES_VERIFICATION,
    OAUTH_ERROR_TOO_MANY_ATTEMPTS,
)
from .pysingaporepower.utils import fold_text, validate_username

_LOGGER = logging.getLogger(__name__)

_MAX_PASSWORD_CHARS = 1024


def _map_auth_error(err: AuthException, default_key: str = "invalid_auth") -> str:
    """Map AuthException protocol error_code to translated form error keys."""
    if err.error_folded == OAUTH_ERROR_REQUIRES_VERIFICATION:
        return "requires_verification"
    if err.error_folded == OAUTH_ERROR_TOO_MANY_ATTEMPTS:
        return "too_many_attempts"
    return default_key


class SingaporePowerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle config, MFA (TOTP / SMS / Email), reauth, and reconfigure flows for Singapore Power."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._username: str = ""
        self._auth: SingaporePowerAuth | None = None
        self._challenge: LoginChallengeResult | None = None
        self._mfa_method: str = "oob_sms"
        self._reauth_entry: ConfigEntry | None = None
        self._reconfigure_entry: ConfigEntry | None = None

    def _masked_dest_for(self, channel: str) -> str | None:
        """Return masked destination (phone or email) for the given OOB channel if present."""
        if not self._challenge:
            return None
        for auth_opt in self._challenge.authenticators:
            if auth_opt.auth_type == "oob" and auth_opt.oob_channel == channel and auth_opt.name:
                return auth_opt.name
        return self._challenge.masked_destination

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step (username and password)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            raw_username = str(user_input.get(CONF_USERNAME, ""))
            password = str(user_input.get(CONF_PASSWORD, ""))

            try:
                username = validate_username(raw_username)
            except ValueError:
                username = ""
                errors["base"] = "invalid_auth"

            if not errors and (not password or len(password) > _MAX_PASSWORD_CHARS):
                errors["base"] = "invalid_auth"

            if not errors:
                unique_key = fold_text(username)
                target_entry = self._reauth_entry or self._reconfigure_entry
                if target_entry is None:
                    await self.async_set_unique_id(unique_key)
                    self._abort_if_unique_id_configured()
                elif target_entry.unique_id:
                    await self.async_set_unique_id(unique_key)
                    self._abort_if_unique_id_mismatch(reason="unique_id_mismatch")

                session = async_get_clientsession(self.hass)
                self._username = username
                self._auth = SingaporePowerAuth(session, username, password)

                try:
                    challenge = await self._auth.async_login_step1()
                    self._challenge = challenge

                    if challenge.authenticated and not challenge.mfa_required:
                        return self._finish_entry()

                    if len(challenge.available_methods) > 1 and challenge.selected_method is None:
                        return await self.async_step_mfa_method()

                    self._mfa_method = (
                        challenge.selected_method
                        or (challenge.available_methods[0] if challenge.available_methods else "otp")
                    )
                    return await self.async_step_mfa()

                except AuthException as err:
                    _LOGGER.warning("Singapore Power login rejected: %s", err)
                    errors["base"] = _map_auth_error(err, "invalid_auth")
                except ApiException as err:
                    _LOGGER.error("Singapore Power API connection error: %s", err)
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error during Singapore Power login")
                    errors["base"] = "unknown"

        target_entry = self._reauth_entry or self._reconfigure_entry
        default_username = (
            str(target_entry.data.get(CONF_USERNAME, ""))
            if target_entry
            else self._username
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=default_username): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_mfa_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose between Authenticator App (TOTP), SMS OTP, and Email OTP."""
        errors: dict[str, str] = {}

        if user_input is not None:
            method = str(user_input["mfa_method"])
            self._mfa_method = method
            if method in ("oob_sms", "oob_email", "oob") and self._auth is not None:
                channel = "email" if method == "oob_email" else "sms"
                try:
                    await self._auth.async_trigger_oob_challenge(channel=channel)
                except AuthException as err:
                    _LOGGER.warning("MFA challenge rejected: %s", err)
                    errors["base"] = _map_auth_error(err, "invalid_auth")
                except ApiException as err:
                    _LOGGER.error("Failed to trigger %s OTP: %s", channel.upper(), err)
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error triggering %s OTP", channel.upper())
                    errors["base"] = "unknown"

            if not errors:
                return await self.async_step_mfa()

        options: dict[str, str] = {}
        avail = self._challenge.available_methods if self._challenge else []
        if "otp" in avail:
            options["otp"] = "Authenticator App (6-digit TOTP)"
        if "oob_sms" in avail or "oob" in avail:
            sms_dest = self._masked_dest_for("sms")
            suffix = f" ({sms_dest})" if sms_dest else ""
            options["oob_sms"] = f"SMS One-Time Password{suffix}"
        if "oob_email" in avail:
            email_dest = self._masked_dest_for("email")
            suffix = f" ({email_dest})" if email_dest else ""
            options["oob_email"] = f"Email One-Time Password{suffix}"

        if not options:
            options["otp"] = "Authenticator App (6-digit TOTP)"

        schema = vol.Schema(
            {vol.Required("mfa_method", default=next(iter(options))): vol.In(options)}
        )
        return self.async_show_form(
            step_id="mfa_method", data_schema=schema, errors=errors
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the MFA OTP / TOTP code submission step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if self._auth is None:
                return self.async_abort(reason="auth_object_missing")

            otp = str(user_input.get("otp", ""))
            try:
                await self._auth.async_submit_mfa(otp, method=self._mfa_method)
                return self._finish_entry()
            except AuthException as err:
                _LOGGER.warning("MFA verification failed: %s", err)
                errors["base"] = _map_auth_error(err, "invalid_otp")
            except ApiException as err:
                _LOGGER.error("Network error during MFA submission: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during MFA submission")
                errors["base"] = "unknown"

        if self._mfa_method == "otp":
            method_label = "your Authenticator App"
        elif self._mfa_method == "oob_email":
            email_dest = self._masked_dest_for("email")
            method_label = (
                f"email ({email_dest})" if email_dest else "your registered email address"
            )
        else:
            sms_dest = self._masked_dest_for("sms")
            method_label = (
                f"SMS ({sms_dest})" if sms_dest else "your registered mobile number via SMS"
            )

        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema({vol.Required("otp"): str}),
            description_placeholders={"mfa_source": method_label},
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when OAuth refresh token expires or is revoked."""
        self._reauth_entry = self._get_reauth_entry()
        self._username = str(entry_data.get(CONF_USERNAME, ""))
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm re-authentication and proceed through login + MFA."""
        if user_input is not None:
            return await self.async_step_user(user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=self._username): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            description_placeholders={"username": self._username},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle UI reconfiguration of credentials for an existing entry."""
        self._reconfigure_entry = self._get_reconfigure_entry()
        self._username = str(self._reconfigure_entry.data.get(CONF_USERNAME, ""))

        if user_input is not None:
            return await self.async_step_user(user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=self._username): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            description_placeholders={"username": self._username},
        )

    def _finish_entry(self) -> ConfigFlowResult:
        """Persist username + OAuth tokens (never storing password at rest)."""
        assert self._auth is not None
        tokens = self._auth.export_tokens()
        entry_data = {
            CONF_USERNAME: self._username,
            CONF_ACCESS_TOKEN: tokens.get("access_token"),
            CONF_ID_TOKEN: tokens.get("id_token"),
            CONF_REFRESH_TOKEN: tokens.get("refresh_token"),
            CONF_TOKEN_EXPIRY: tokens.get("token_expiry"),
        }

        if self._reauth_entry is not None:
            return self.async_update_reload_and_abort(
                self._reauth_entry,
                data=entry_data,
                reason="reauth_successful",
            )

        if self._reconfigure_entry is not None:
            return self.async_update_reload_and_abort(
                self._reconfigure_entry,
                data=entry_data,
                reason="reconfigure_successful",
            )

        return self.async_create_entry(title=self._username, data=entry_data)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow handler."""
        return SingaporePowerOptionsFlow()


class SingaporePowerOptionsFlow(config_entries.OptionsFlow):
    """Options flow for configuring polling, Recorder statistics, and custom OEM electricity tariff."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage Singapore Power integration options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            raw_price = user_input.get(CONF_ELECTRICITY_PRICE, "")
            try:
                parsed_price = parse_electricity_price_input(raw_price)
            except ValueError:
                parsed_price = None
                errors[CONF_ELECTRICITY_PRICE] = "invalid_electricity_price"

            if not errors:
                saved_options = {
                    CONF_SCAN_INTERVAL_MINUTES: int(
                        user_input[CONF_SCAN_INTERVAL_MINUTES]
                    ),
                    CONF_ENABLE_RECORDER_STATS: bool(
                        user_input[CONF_ENABLE_RECORDER_STATS]
                    ),
                    CONF_ENABLE_EXTENDED_INSIGHTS: bool(
                        user_input[CONF_ENABLE_EXTENDED_INSIGHTS]
                    ),
                    CONF_ELECTRICITY_PRICE: parsed_price,
                }
                return self.async_create_entry(title="", data=saved_options)

        options = self.config_entry.options
        existing_price = parse_electricity_price(options.get(CONF_ELECTRICITY_PRICE))
        suggested_price = "" if existing_price is None else f"{existing_price:g}"

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL_MINUTES,
                    default=options.get(
                        CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES
                    ),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(
                        min=MIN_SCAN_INTERVAL_MINUTES, max=MAX_SCAN_INTERVAL_MINUTES
                    ),
                ),
                vol.Required(
                    CONF_ENABLE_RECORDER_STATS,
                    default=options.get(
                        CONF_ENABLE_RECORDER_STATS, DEFAULT_ENABLE_RECORDER_STATS
                    ),
                ): bool,
                vol.Required(
                    CONF_ENABLE_EXTENDED_INSIGHTS,
                    default=options.get(
                        CONF_ENABLE_EXTENDED_INSIGHTS, DEFAULT_ENABLE_EXTENDED_INSIGHTS
                    ),
                ): bool,
                vol.Optional(
                    CONF_ELECTRICITY_PRICE,
                    description={"suggested_value": suggested_price},
                    default=suggested_price,
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
