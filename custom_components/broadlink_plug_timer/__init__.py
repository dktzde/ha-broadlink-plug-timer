"""Broadlink Plug Timer: write timers directly into Broadlink SP3S plugs.

The integration adds actions only (no entities). Each action names one plug by its switch entity
of the core Broadlink integration; host, MAC and device type come from that config entry.

Main use: a dead-man switch. Call `broadlink_plug_timer.set_deadman` right before switching a load
on. The plug then switches itself off after the given minutes, even if Home Assistant or Wi-Fi is
gone. In normal operation Home Assistant switches off earlier and the one-time "off" has no effect.

All plug I/O blocks (UDP), so it runs in the executor. A lock per plug keeps two actions from
mixing their read - modify - write cycles.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
import logging
import os
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import plug as plug_lib
from .const import (
    ATTR_DAYS,
    ATTR_DRY_RUN,
    ATTR_ENABLED,
    ATTR_ENTRIES,
    ATTR_FILE,
    ATTR_MINUTES,
    ATTR_NOTE,
    ATTR_OFF_TIME,
    ATTR_ON_TIME,
    BACKUP_FOLDER,
    BROADLINK_DOMAIN,
    CONF_BROADLINK_HOST,
    CONF_BROADLINK_MAC,
    CONF_BROADLINK_TYPE,
    DOMAIN,
    SERVICE_BACKUP,
    SERVICE_CLEAR_DEADMAN,
    SERVICE_READ,
    SERVICE_RESTORE,
    SERVICE_SET_DEADMAN,
    SERVICE_SET_ONCE,
    SERVICE_WRITE_WEEKLY,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# -- action schemas ----------------------------------------------------------------------------------

# entity_id accepts a single id or a list (scripts may pass it via `target:`); exactly one is allowed.
_PLUG = {vol.Required(ATTR_ENTITY_ID): cv.entity_ids}


def _time_text(value: Any) -> str:
    """Accept "06:30", "06:30:15" or a time object (time selector) and return "HH:MM:SS"."""
    return cv.time(value).strftime("%H:%M:%S")


WEEKLY_ENTRY_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENABLED, default=True): cv.boolean,
        vol.Optional(ATTR_DAYS): vol.Any(cv.string, [cv.string]),
        vol.Optional(ATTR_ON_TIME): vol.Any(None, _time_text),
        vol.Optional(ATTR_OFF_TIME): vol.Any(None, _time_text),
        vol.Optional(ATTR_NOTE): cv.string,  # free text for the user, not written into the plug
    }
)

SCHEMA_READ = vol.Schema(_PLUG)
SCHEMA_SET_DEADMAN = vol.Schema(
    {
        **_PLUG,
        vol.Required(ATTR_MINUTES): vol.All(vol.Coerce(float), vol.Range(min=1, max=plug_lib.MAX_DEADMAN_MINUTES)),
    }
)
SCHEMA_CLEAR_DEADMAN = vol.Schema(_PLUG)
SCHEMA_SET_ONCE = vol.Schema(
    {**_PLUG, vol.Required(ATTR_ON_TIME): cv.datetime, vol.Required(ATTR_OFF_TIME): cv.datetime}
)
SCHEMA_WRITE_WEEKLY = vol.Schema(
    {
        **_PLUG,
        vol.Required(ATTR_ENTRIES): vol.All(cv.ensure_list, [WEEKLY_ENTRY_SCHEMA]),
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
    }
)
SCHEMA_BACKUP = vol.Schema(_PLUG)
SCHEMA_RESTORE = vol.Schema({**_PLUG, vol.Required(ATTR_FILE): cv.string})


@dataclass
class _Target:
    """Connection data of one plug, taken from its Broadlink config entry."""

    entity_id: str
    host: str
    mac: str
    devtype: int

    def plug(self) -> plug_lib.Plug:
        return plug_lib.Plug(self.host, self.mac, self.devtype)


@dataclass
class _Data:
    """Runtime data kept in hass.data[DOMAIN]."""

    locks: dict[str, asyncio.Lock] = field(default_factory=dict)


# -- setup -----------------------------------------------------------------------------------------------


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions (they check that the config entry is loaded)."""
    hass.data[DOMAIN] = _Data()

    def register(name: str, schema: vol.Schema, handler: Callable, response: SupportsResponse) -> None:
        hass.services.async_register(DOMAIN, name, partial(handler, hass), schema=schema, supports_response=response)

    register(SERVICE_READ, SCHEMA_READ, _async_read, SupportsResponse.ONLY)
    register(SERVICE_SET_DEADMAN, SCHEMA_SET_DEADMAN, _async_set_deadman, SupportsResponse.OPTIONAL)
    register(SERVICE_CLEAR_DEADMAN, SCHEMA_CLEAR_DEADMAN, _async_clear_deadman, SupportsResponse.OPTIONAL)
    register(SERVICE_SET_ONCE, SCHEMA_SET_ONCE, _async_set_once, SupportsResponse.OPTIONAL)
    register(SERVICE_WRITE_WEEKLY, SCHEMA_WRITE_WEEKLY, _async_write_weekly, SupportsResponse.OPTIONAL)
    register(SERVICE_BACKUP, SCHEMA_BACKUP, _async_backup, SupportsResponse.OPTIONAL)
    register(SERVICE_RESTORE, SCHEMA_RESTORE, _async_restore, SupportsResponse.OPTIONAL)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Nothing to set up: the entry only enables the actions."""
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Nothing to unload."""
    return True


# -- helpers ---------------------------------------------------------------------------------------------


def _ensure_loaded(hass: HomeAssistant) -> None:
    if not any(entry.state is ConfigEntryState.LOADED for entry in hass.config_entries.async_entries(DOMAIN)):
        raise ServiceValidationError("Broadlink Plug Timer is not set up (Settings > Devices & services)")


def _resolve(hass: HomeAssistant, call: ServiceCall) -> _Target:
    """Find host, MAC and device type of the plug behind the given Broadlink entity."""
    entity_ids: list[str] = call.data[ATTR_ENTITY_ID]
    if len(entity_ids) != 1:
        raise ServiceValidationError(f"Select exactly one Broadlink plug, not {len(entity_ids)}")
    entity_id = entity_ids[0]
    registry_entry = er.async_get(hass).async_get(entity_id)
    if registry_entry is None or registry_entry.platform != BROADLINK_DOMAIN or not registry_entry.config_entry_id:
        raise ServiceValidationError(f"{entity_id} is not an entity of the Broadlink integration")
    config_entry = hass.config_entries.async_get_entry(registry_entry.config_entry_id)
    if config_entry is None or config_entry.domain != BROADLINK_DOMAIN:
        raise ServiceValidationError(f"No Broadlink config entry found for {entity_id}")
    data = config_entry.data
    devtype = data.get(CONF_BROADLINK_TYPE)
    if devtype not in plug_lib.SUPPORTED_TYPES:
        shown = f"{devtype:#06x}" if isinstance(devtype, int) else repr(devtype)
        raise ServiceValidationError(
            f"{entity_id} is a Broadlink device of type {shown}. Supported are only SP3S plugs "
            f"({', '.join(f'{name} {code:#06x}' for code, name in plug_lib.SUPPORTED_TYPES.items())})."
        )
    host, mac = data.get(CONF_BROADLINK_HOST), data.get(CONF_BROADLINK_MAC)
    if not host or not mac:
        raise ServiceValidationError(f"The Broadlink config entry of {entity_id} has no host or MAC address")
    return _Target(entity_id, str(host), str(mac), devtype)


def _backup_dir(hass: HomeAssistant) -> str:
    return hass.config.path(BACKUP_FOLDER)


def _local(value: datetime) -> datetime:
    """Datetime from an action field: naive values are in the Home Assistant time zone."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_local(value)


async def _async_run(
    hass: HomeAssistant, call: ServiceCall, job: Callable[..., dict[str, Any]], *args: Any
) -> ServiceResponse:
    """Run one blocking plug operation in the executor, one at a time per plug."""
    _ensure_loaded(hass)
    target = _resolve(hass, call)
    data: _Data = hass.data[DOMAIN]
    lock = data.locks.setdefault(target.mac, asyncio.Lock())
    async with lock:
        try:
            result = await hass.async_add_executor_job(partial(job, target.plug(), *args))
        except plug_lib.PlugInputError as err:
            raise ServiceValidationError(str(err)) from err
        except (plug_lib.PlugError, OSError) as err:
            raise HomeAssistantError(f"{target.entity_id}: {err}") from err
    _LOGGER.debug("%s.%s for %s: %s", DOMAIN, call.service, target.entity_id, result)
    result = {"entity_id": target.entity_id, **result}
    return result if call.return_response else None


# -- action handlers -----------------------------------------------------------------------------------


async def _async_read(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    return await _async_run(hass, call, plug_lib.read_report, dt_util.now())


async def _async_set_deadman(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    return await _async_run(hass, call, plug_lib.write_deadman, call.data[ATTR_MINUTES], dt_util.now())


async def _async_clear_deadman(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    return await _async_run(hass, call, plug_lib.write_deadman, None, dt_util.now())


async def _async_set_once(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    on, off = _local(call.data[ATTR_ON_TIME]), _local(call.data[ATTR_OFF_TIME])
    return await _async_run(hass, call, plug_lib.write_once, on, off, dt_util.now(), _backup_dir(hass))


async def _async_write_weekly(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entries = [dict(entry) for entry in call.data[ATTR_ENTRIES]]
    return await _async_run(
        hass, call, plug_lib.write_weekly, entries, dt_util.now(), call.data[ATTR_DRY_RUN], _backup_dir(hass)
    )


async def _async_backup(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    def job(plug: plug_lib.Plug, directory: str) -> dict[str, Any]:
        return {"backup": plug_lib.save_backup(plug, directory)}

    return await _async_run(hass, call, job, _backup_dir(hass))


async def _async_restore(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    # Only files from the backup folder; a bare file name keeps paths out of the action data.
    name = os.path.basename(call.data[ATTR_FILE])
    path = os.path.join(_backup_dir(hass), name)
    if not name or not await hass.async_add_executor_job(os.path.isfile, path):
        raise ServiceValidationError(f"Backup {name!r} not found in {_backup_dir(hass)}")
    return await _async_run(hass, call, plug_lib.restore_backup, path, dt_util.now(), _backup_dir(hass))
