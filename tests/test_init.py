"""Tests for setting up and unloading the Casambi Bluetooth integration."""

from unittest.mock import MagicMock, patch

from CasambiBt.errors import AuthenticationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant


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
