"""Config flow: adds the integration once. There is nothing to enter.

The plugs come from the core Broadlink integration; every action names the plug by its
Broadlink switch entity.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN, TITLE


class BroadlinkPlugTimerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance config flow (manifest: single_config_entry)."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm and create the entry."""
        if user_input is not None:
            return self.async_create_entry(title=TITLE, data={})
        return self.async_show_form(step_id="user")
