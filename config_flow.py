import logging
import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD

from .const import DOMAIN
from .pysingaporepower import SingaporePowerAuth, AuthException, ApiException

_LOGGER = logging.getLogger(__name__)

class SingaporePowerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Singapore Power."""

    VERSION = 1

    def __init__(self):
        """Initialize the config flow."""
        self.data = {}
        self.auth: SingaporePowerAuth | None = None

    async def async_step_user(self, user_input=None):
        """Handle the initial step (username and password)."""
        errors = {}
        if user_input is not None:
            self.data = user_input
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]

            try:
                # We create a temporary session and auth client to test credentials
                async with aiohttp.ClientSession() as session:
                    self.auth = SingaporePowerAuth(session, username, password)
                    await self.auth.async_request_otp()
                
                # If OTP request is successful, move to the next step
                return await self.async_step_mfa()

            except AuthException as err:
                _LOGGER.error("Authentication failed: %s", err)
                errors["base"] = "invalid_auth"
            except ApiException as err:
                _LOGGER.error("API communication error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("An unexpected error occurred")
                errors["base"] = "unknown"

        # Show the form to the user
        data_schema = vol.Schema({
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
        })
        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

    async def async_step_mfa(self, user_input=None):
        """Handle the MFA (OTP) step."""
        errors = {}
        if user_input is not None:
            otp = user_input["otp"]
            try:
                # The auth object should still be available from the previous step
                if self.auth:
                    await self.auth.async_submit_otp(otp)
                    # If OTP is correct, create the config entry
                    return self.async_create_entry(title=self.data[CONF_USERNAME], data=self.data)
                else:
                    # Should not happen, but as a safeguard
                    return self.async_abort(reason="auth_object_missing")

            except AuthException as err:
                _LOGGER.error("OTP submission failed: %s", err)
                errors["base"] = "invalid_otp"
            except ApiException as err:
                _LOGGER.error("API communication error during OTP submission: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("An unexpected error occurred during OTP submission")
                errors["base"] = "unknown"

        # Show the OTP form
        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema({vol.Required("otp"): str}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Singapore Power does not have options."""
        # Returning an empty options flow handler, as we have no options.
        return config_entries.OptionsFlow()