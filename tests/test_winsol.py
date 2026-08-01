"""Tests for Winsol pergola support: louvre cover, screen cover, sensors."""

from unittest.mock import MagicMock

from CasambiBt import Unit, UnitControl
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casambi_bt.classify import UnitKind, classify_unit
from custom_components.casambi_bt.const import DOMAIN
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    DOMAIN as COVER_DOMAIN,
    SERVICE_SET_COVER_POSITION,
    SERVICE_STOP_COVER,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import (
    SENSOR_PLATFORM_UUID,
    WINSOL_LOUVRE_UUID,
    WINSOL_SCREEN_UUID,
    make_light_unit,
    make_louvre_unit,
    make_sensor_platform_unit,
    make_winsol_louvre_unit,
    make_winsol_screen_unit,
)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, f"missing entity for {unique_id}"
    return entity_id


def test_classification() -> None:
    """Test that units are classified by their control layout."""
    assert classify_unit(make_light_unit()) is UnitKind.LIGHT
    assert classify_unit(make_louvre_unit()) is UnitKind.LIGHT
    assert classify_unit(make_winsol_louvre_unit()) is UnitKind.LOUVRE
    assert classify_unit(make_winsol_screen_unit()) is UnitKind.SCREEN
    assert classify_unit(make_sensor_platform_unit()) is UnitKind.SENSOR_PLATFORM


async def test_motor_units_are_not_lights(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that louvre/screen/sensor units don't create light entities."""
    await _setup(hass, mock_config_entry)

    registry = er.async_get(hass)
    for uuid in (WINSOL_LOUVRE_UUID, WINSOL_SCREEN_UUID, SENSOR_PLATFORM_UUID):
        assert (
            registry.async_get_entity_id(
                "light", DOMAIN, f"{mock_casambi.networkId}-unit-{uuid}-light"
            )
            is None
        )


async def test_louvre_cover(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the slider-based louvre cover."""
    await _setup(hass, mock_config_entry)

    entity_id = _entity_id(
        hass, COVER_DOMAIN, f"{mock_casambi.networkId}-unit-{WINSOL_LOUVRE_UUID}-cover"
    )
    state = hass.states.get(entity_id)
    assert state is not None
    # slider 128 of 255 -> 50%
    assert state.attributes[ATTR_CURRENT_POSITION] == 50

    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_SET_COVER_POSITION,
        {ATTR_ENTITY_ID: entity_id, ATTR_POSITION: 40},
        blocking=True,
    )
    mock_casambi.setSlider.assert_awaited_once()
    target, value = mock_casambi.setSlider.await_args.args
    assert isinstance(target, Unit)
    assert target.uuid == WINSOL_LOUVRE_UUID
    assert value == 102

    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_STOP_COVER,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    mock_casambi.setControlValue.assert_awaited_once()
    target, control, value = mock_casambi.setControlValue.await_args.args
    assert isinstance(control, UnitControl)
    assert control.offset == 36
    assert value == 1


async def test_screen_cover(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the dimmer-based screen cover."""
    await _setup(hass, mock_config_entry)

    entity_id = _entity_id(
        hass, COVER_DOMAIN, f"{mock_casambi.networkId}-unit-{WINSOL_SCREEN_UUID}-cover"
    )
    state = hass.states.get(entity_id)
    assert state is not None
    # dimmer 64 of 255 -> 25%
    assert state.attributes[ATTR_CURRENT_POSITION] == 25

    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_SET_COVER_POSITION,
        {ATTR_ENTITY_ID: entity_id, ATTR_POSITION: 80},
        blocking=True,
    )
    mock_casambi.setLevel.assert_awaited_once()
    target, value = mock_casambi.setLevel.await_args.args
    assert isinstance(target, Unit)
    assert target.uuid == WINSOL_SCREEN_UUID
    assert value == 204


async def test_environment_sensors(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the sensor platform entities."""
    await _setup(hass, mock_config_entry)

    network_id = mock_casambi.networkId
    prefix = f"{network_id}-unit-{SENSOR_PLATFORM_UUID}"

    # sensor_cache: {0: 1 (dry), 1: 140 (wind), 2: 40 (solar), 3: 0 (no PIR)}
    wind = hass.states.get(_entity_id(hass, "sensor", f"{prefix}-wind"))
    assert wind is not None
    assert float(wind.state) == 35.0

    solar = hass.states.get(_entity_id(hass, "sensor", f"{prefix}-solar"))
    assert solar is not None
    assert float(solar.state) == 10.0

    lux = hass.states.get(_entity_id(hass, "sensor", f"{prefix}-illuminance"))
    assert lux is not None
    assert float(lux.state) == 1234

    rain = hass.states.get(_entity_id(hass, "binary_sensor", f"{prefix}-rain"))
    assert rain is not None
    assert rain.state == "off"

    motion = hass.states.get(_entity_id(hass, "binary_sensor", f"{prefix}-motion"))
    assert motion is not None
    assert motion.state == "off"

    presence = hass.states.get(_entity_id(hass, "binary_sensor", f"{prefix}-presence"))
    assert presence is not None
    assert presence.state == "on"
