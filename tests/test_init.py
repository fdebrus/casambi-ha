"""Tests for setting up and unloading the Casambi Bluetooth integration."""

from unittest.mock import MagicMock, patch

from CasambiBt.errors import AuthenticationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casambi_bt import async_remove_config_entry_device
from custom_components.casambi_bt.const import DOMAIN
from custom_components.casambi_bt.diagnostics import async_get_config_entry_diagnostics
from homeassistant.components.bluetooth import BluetoothScanningMode
from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .conftest import (
    LIGHT_UUID,
    NETWORK_ADDRESS,
    NETWORK_ID,
    WINSOL_LOUVRE_UUID,
    get_device,
)


async def test_setup_and_unload(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test a successful setup and unload."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    mock_casambi.connect.assert_awaited_once()

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    mock_casambi.disconnect.assert_awaited()


async def test_setup_retries_without_ble_device(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that setup retries when the network is not in bluetooth range."""
    with patch(
        "homeassistant.components.bluetooth.async_ble_device_from_address",
        return_value=None,
    ):
        mock_config_entry.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_auth_failure_starts_reauth(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that an authentication error starts the reauth flow."""
    mock_casambi.connect.side_effect = AuthenticationError

    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == "reauth"
    ]
    assert len(flows) == 1


async def test_diagnostics(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that the diagnostics describe the network and the automation."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diagnostics["entry"]["data"][CONF_PASSWORD] == REDACTED
    assert diagnostics["network"]["id"] == NETWORK_ID
    assert diagnostics["network"]["available"] is True
    assert [unit["uuid"] for unit in diagnostics["units"]] == [
        unit.uuid for unit in mock_casambi.units
    ]
    louvre = next(u for u in diagnostics["units"] if u["uuid"] == WINSOL_LOUVRE_UUID)
    assert louvre["controls"][6]["type"] == "SLIDER"
    assert louvre["state"]["raw_state"] == "0000000000"
    # The automation switches have registered their restored state.
    assert diagnostics["automation"]["frost_lock"] == {WINSOL_LOUVRE_UUID: True}
    assert diagnostics["automation"]["temp_control"] == {WINSOL_LOUVRE_UUID: False}
    assert diagnostics["automation"]["rain_active"] is False


async def test_remove_config_entry_device(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that only devices of removed units can be deleted."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = dr.async_get(hass)
    entry_id = mock_config_entry.entry_id

    network_device = get_device(registry, (DOMAIN, NETWORK_ID), entry_id)
    assert network_device is not None
    assert (
        await async_remove_config_entry_device(hass, mock_config_entry, network_device)
        is False
    )

    unit_device = get_device(registry, (DOMAIN, LIGHT_UUID), entry_id)
    assert unit_device is not None
    assert (
        await async_remove_config_entry_device(hass, mock_config_entry, unit_device)
        is False
    )

    stale_device = registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, "stale-unit-uuid")},
        name="Stale unit",
    )
    assert (
        await async_remove_config_entry_device(hass, mock_config_entry, stale_device)
        is True
    )


async def test_network_watched_with_passive_scanning(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that the network's advertisements are watched passively."""
    with patch(
        "homeassistant.components.bluetooth.async_register_callback",
        return_value=lambda: None,
    ) as register:
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    register.assert_called_once()
    _, _, matcher, mode = register.call_args.args
    assert matcher == {"address": NETWORK_ADDRESS, "connectable": True}
    assert mode is BluetoothScanningMode.PASSIVE


async def test_unit_devices_link_to_network_device(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that unit devices hang off the network device without deprecations."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = dr.async_get(hass)
    entry_id = mock_config_entry.entry_id
    network_device = get_device(registry, (DOMAIN, NETWORK_ID), entry_id)
    assert network_device is not None
    assert mock_config_entry.runtime_data.network_device_id == network_device.id

    unit_device = get_device(registry, (DOMAIN, LIGHT_UUID), entry_id)
    assert unit_device is not None
    assert unit_device.via_device_id == network_device.id

    assert "deprecated `via_device`" not in caplog.text


async def test_binary_sensor_reflects_connection(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that the status binary sensor mirrors the connection state."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    states = [
        s for s in hass.states.async_all("binary_sensor") if "status" in s.entity_id
    ]
    assert len(states) == 1
    assert states[0].state == "on"
