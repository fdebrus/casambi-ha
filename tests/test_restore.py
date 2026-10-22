"""Tests for the automation settings that survive a restart."""

from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from custom_components.casambi_bt.const import DOMAIN
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from .conftest import NETWORK_ID, WINSOL_LOUVRE_UUID

LOUVRE_PREFIX = f"{NETWORK_ID}-unit-{WINSOL_LOUVRE_UUID}"
SWITCHES = {
    "switch.tracking": f"{LOUVRE_PREFIX}-sun-tracking",
    "switch.temperature": f"{LOUVRE_PREFIX}-temperature-control",
    "switch.frost": f"{LOUVRE_PREFIX}-frost-lock",
    "switch.weather": f"{NETWORK_ID}-weather-protection",
}


def _register(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Give the switches known entity ids before they are created."""
    registry = er.async_get(hass)
    for entity_id, unique_id in SWITCHES.items():
        registry.async_get_or_create(
            "switch",
            DOMAIN,
            unique_id,
            config_entry=entry,
            suggested_object_id=entity_id.removeprefix("switch."),
        )


async def test_settings_restored_from_an_outage(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that the settings come back even when the state was unavailable.

    The switches read "unavailable" whenever the network does; a restart
    during an outage used to turn them all off (and the frost lock on).
    """
    mock_config_entry.add_to_hass(hass)
    _register(hass, mock_config_entry)
    mock_restore_cache_with_extra_data(
        hass,
        [
            (State("switch.tracking", STATE_UNAVAILABLE), {"is_on": True}),
            (State("switch.temperature", STATE_UNAVAILABLE), {"is_on": True}),
            (State("switch.frost", STATE_UNAVAILABLE), {"is_on": False}),
            (State("switch.weather", STATE_UNAVAILABLE), {"is_on": True}),
        ],
    )

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    for entity_id in SWITCHES:
        assert hass.states.get(entity_id).state == (
            STATE_OFF if entity_id == "switch.frost" else STATE_ON
        ), entity_id
    # The settings took effect, not just the state.
    api = mock_config_entry.runtime_data
    assert api.temp_control[WINSOL_LOUVRE_UUID] is True
    assert api.frost_lock[WINSOL_LOUVRE_UUID] is False


async def test_settings_restored_from_the_state_alone(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the restore from a version that only stored the state."""
    mock_config_entry.add_to_hass(hass)
    _register(hass, mock_config_entry)
    mock_restore_cache(
        hass,
        [
            State("switch.tracking", STATE_ON),
            State("switch.frost", STATE_OFF),
            # Nothing useful was stored: the defaults stand.
            State("switch.temperature", STATE_UNAVAILABLE),
            State("switch.weather", STATE_UNAVAILABLE),
        ],
    )

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("switch.tracking").state == STATE_ON
    assert hass.states.get("switch.frost").state == STATE_OFF
    assert hass.states.get("switch.temperature").state == STATE_OFF
    assert hass.states.get("switch.weather").state == STATE_OFF


async def test_settings_are_stored_as_extra_data(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test what the switches hand to the restore store."""
    mock_config_entry.add_to_hass(hass)
    _register(hass, mock_config_entry)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    component = hass.data["entity_components"]["switch"]
    frost = component.get_entity("switch.frost")
    weather = component.get_entity("switch.weather")
    assert frost is not None and weather is not None
    assert frost.extra_restore_state_data.as_dict() == {"is_on": True}
    assert weather.extra_restore_state_data.as_dict() == {"is_on": False}
