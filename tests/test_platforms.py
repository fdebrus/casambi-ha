"""Tests for the light, number, and scene platforms."""

from unittest.mock import MagicMock

from CasambiBt import Unit
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casambi_bt.const import DOMAIN
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    DOMAIN as LIGHT_DOMAIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.components.scene import DOMAIN as SCENE_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import LIGHT_UUID, LOUVRE_UUID, NETWORK_ID


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_light_state_and_services(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the light entity state and turn on/off."""
    await _setup(hass, mock_config_entry)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        LIGHT_DOMAIN, DOMAIN, f"{NETWORK_ID}-unit-{LIGHT_UUID}-light"
    )
    assert entity_id is not None

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "on"
    assert state.attributes[ATTR_BRIGHTNESS] == 128

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_BRIGHTNESS: 200},
        blocking=True,
    )
    mock_casambi.setUnitState.assert_awaited_once()
    target, unit_state = mock_casambi.setUnitState.await_args.args
    assert isinstance(target, Unit)
    assert unit_state.dimmer == 200

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    mock_casambi.setLevel.assert_awaited_once()
    _, level = mock_casambi.setLevel.await_args.args
    assert level == 0


async def test_vertical_number(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the vertical number entity in the default (non-cover) mode."""
    await _setup(hass, mock_config_entry)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        NUMBER_DOMAIN, DOMAIN, f"{NETWORK_ID}-unit-{LOUVRE_UUID}-vertical"
    )
    assert entity_id is not None

    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == 128.0

    # No cover should exist in this mode.
    assert (
        registry.async_get_entity_id(
            "cover", DOMAIN, f"{NETWORK_ID}-unit-{LOUVRE_UUID}-cover"
        )
        is None
    )

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: 200},
        blocking=True,
    )
    mock_casambi.setVertical.assert_awaited_once()
    _, vertical = mock_casambi.setVertical.await_args.args
    assert vertical == 200


async def test_scene_activation(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that activating a scene calls the library."""
    await _setup(hass, mock_config_entry)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        SCENE_DOMAIN, DOMAIN, f"{NETWORK_ID}-scene-1"
    )
    assert entity_id is not None

    await hass.services.async_call(
        SCENE_DOMAIN,
        "turn_on",
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    mock_casambi.switchToScene.assert_awaited_once()
