"""Tests of the integration: config flow and actions, with a fake plug instead of the network."""

from __future__ import annotations

from datetime import timedelta
import os

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.broadlink_plug_timer import plug as plug_lib
from custom_components.broadlink_plug_timer.const import DOMAIN

from .common import HOST, LEGACY_APP_ENTRY, MAC, FakeDevice, make_memory, plug_now


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom_components/ from this repository."""
    return


@pytest.fixture
async def device(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tmp_path) -> FakeDevice:
    """Integration set up, fake plug in place, backups go to a temporary folder."""
    await hass.config.async_set_time_zone("Europe/Berlin")
    hass.config.config_dir = str(tmp_path)
    fake = FakeDevice(make_memory(plug_now(), weekly=[LEGACY_APP_ENTRY]))
    monkeypatch.setattr(plug_lib, "Plug", fake.factory)
    entry = MockConfigEntry(domain=DOMAIN, title="Broadlink Plug Timer")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return fake


def add_broadlink_switch(hass: HomeAssistant, devtype: int = 0x947A, object_id: str = "pump") -> str:
    """A switch entity of the core Broadlink integration (the integration itself is not loaded)."""
    broadlink_entry = MockConfigEntry(
        domain="broadlink", data={"host": HOST, "mac": MAC, "type": devtype, "timeout": 10}
    )
    broadlink_entry.add_to_hass(hass)
    return (
        er.async_get(hass)
        .async_get_or_create(
            "switch", "broadlink", f"{MAC}-{object_id}", config_entry=broadlink_entry, suggested_object_id=object_id
        )
        .entity_id
    )


async def call(hass: HomeAssistant, service: str, **data):
    return await hass.services.async_call(DOMAIN, service, data, blocking=True, return_response=True)


# -- config flow ------------------------------------------------------------------------------------------------


async def test_config_flow_single_instance(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Broadlink Plug Timer"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


# -- actions ------------------------------------------------------------------------------------------------------


async def test_read(hass: HomeAssistant, device: FakeDevice) -> None:
    entity_id = add_broadlink_switch(hass)
    response = await call(hass, "read", entity_id=entity_id)
    assert response["entity_id"] == entity_id
    assert response["name"] == "Test plug"
    assert response["weekly_local_time"][0]["on_time"] in ("03:14:59", "04:14:59")  # CET or CEST
    assert device.connections == [(HOST, MAC, 0x947A)]
    assert device.writes == 0


async def test_set_and_clear_deadman(hass: HomeAssistant, device: FakeDevice) -> None:
    entity_id = add_broadlink_switch(hass)
    response = await call(hass, "set_deadman", entity_id=entity_id, minutes=8)
    off_local = dt_util.parse_datetime(response["off_local"])
    assert abs(off_local - dt_util.now() - timedelta(minutes=8)) < timedelta(seconds=5)
    assert [entry["deadman"] for entry in plug_lib.once_entries(device.memory)] == [True]

    # without response (as a script step usually calls it)
    await hass.services.async_call(DOMAIN, "set_deadman", {"entity_id": entity_id, "minutes": 3}, blocking=True)
    assert len(plug_lib.once_entries(device.memory)) == 1

    await call(hass, "clear_deadman", entity_id=entity_id)
    assert plug_lib.once_entries(device.memory) == []
    assert device.writes == 3


async def test_entity_id_from_target_list(hass: HomeAssistant, device: FakeDevice) -> None:
    entity_id = add_broadlink_switch(hass)
    response = await hass.services.async_call(
        DOMAIN, "read", {}, target={"entity_id": [entity_id]}, blocking=True, return_response=True
    )
    assert response["entity_id"] == entity_id
    other = add_broadlink_switch(hass, object_id="other")
    with pytest.raises(ServiceValidationError, match="exactly one"):
        await call(hass, "read", entity_id=[entity_id, other])


async def test_set_once(hass: HomeAssistant, device: FakeDevice) -> None:
    entity_id = add_broadlink_switch(hass)
    on = dt_util.now().replace(tzinfo=None, microsecond=0) + timedelta(days=1)
    response = await call(hass, "set_once", entity_id=entity_id, on_time=on, off_time=on + timedelta(minutes=5))
    [entry] = plug_lib.once_entries(device.memory)
    assert entry["on"] == response["on_plug_time"]
    assert os.path.isfile(response["backup"])
    with pytest.raises(ServiceValidationError, match="after"):
        await call(hass, "set_once", entity_id=entity_id, on_time=on, off_time=on)
    gap = dt_util.parse_datetime("2027-03-28 02:30:00")  # does not exist in Europe/Berlin
    with pytest.raises(ServiceValidationError, match="does not exist"):
        await call(hass, "set_once", entity_id=entity_id, on_time=gap, off_time=gap + timedelta(hours=2))


async def test_write_weekly(hass: HomeAssistant, device: FakeDevice) -> None:
    entity_id = add_broadlink_switch(hass)
    entries = [{"days": "mon-fri", "on_time": "06:30", "off_time": "06:35", "note": "test"}]
    response = await call(hass, "write_weekly", entity_id=entity_id, entries=entries, dry_run=True)
    assert (response["changed"], response["written"], device.writes) == (True, False, 0)

    response = await call(hass, "write_weekly", entity_id=entity_id, entries=entries)
    assert (response["written"], device.writes) == (True, 1)
    assert response["weekly_local_time"][0]["on_time"] == "06:30"
    assert os.path.isfile(response["backup"])

    response = await call(hass, "write_weekly", entity_id=entity_id, entries=entries)
    assert (response["changed"], response["written"], device.writes) == (False, False, 1)

    with pytest.raises(ServiceValidationError, match="off_time"):
        await call(hass, "write_weekly", entity_id=entity_id, entries=[{"on_time": "06:30"}])


async def test_backup_and_restore(hass: HomeAssistant, device: FakeDevice) -> None:
    entity_id = add_broadlink_switch(hass)
    response = await call(hass, "backup", entity_id=entity_id)
    name = os.path.basename(response["backup"])
    assert name.startswith(MAC)
    before = bytes(device.memory)

    await call(hass, "set_deadman", entity_id=entity_id, minutes=8)
    assert bytes(device.memory) != before
    await call(hass, "restore", entity_id=entity_id, file=name)
    assert bytes(device.memory) == before

    with pytest.raises(ServiceValidationError, match="not found"):
        await call(hass, "restore", entity_id=entity_id, file="missing.bin")


async def test_not_a_broadlink_entity(hass: HomeAssistant, device: FakeDevice) -> None:
    other = er.async_get(hass).async_get_or_create("switch", "demo", "x1", suggested_object_id="demo")
    with pytest.raises(ServiceValidationError, match="not an entity of the Broadlink"):
        await call(hass, "read", entity_id=other.entity_id)


async def test_unsupported_device_type(hass: HomeAssistant, device: FakeDevice) -> None:
    entity_id = add_broadlink_switch(hass, devtype=0x7547)
    with pytest.raises(ServiceValidationError, match="0x7547"):
        await call(hass, "read", entity_id=entity_id)
    assert device.connections == []


async def test_plug_not_answering(hass: HomeAssistant, device: FakeDevice) -> None:
    entity_id = add_broadlink_switch(hass)
    device.fail = True
    with pytest.raises(HomeAssistantError, match="No answer"):
        await call(hass, "set_deadman", entity_id=entity_id, minutes=8)


async def test_actions_need_loaded_entry(hass: HomeAssistant, device: FakeDevice) -> None:
    entity_id = add_broadlink_switch(hass)
    [entry] = hass.config_entries.async_entries(DOMAIN)
    await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(ServiceValidationError, match="not set up"):
        await call(hass, "read", entity_id=entity_id)
