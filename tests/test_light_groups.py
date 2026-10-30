"""Tests for which Casambi groups become lights, and how they are driven."""

from unittest.mock import MagicMock

from CasambiBt import Group, UnitControl, UnitControlType, UnitState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casambi_bt.const import DOMAIN
from homeassistant.components.light import (
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_XY_COLOR,
    DOMAIN as LIGHT_DOMAIN,
    SERVICE_TURN_ON,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import (
    NETWORK_ID,
    WINSOL_LOUVRE_UUID,
    WINSOL_RELAY_UUID,
    WINSOL_SCREEN_UUID,
    _make_unit,
    setup_entry as _setup,
    unit_by_uuid,
)


def _group_light(hass: HomeAssistant, group_id: int) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        LIGHT_DOMAIN, DOMAIN, f"{NETWORK_ID}-group-{group_id}"
    )


def _tunable_light(device_id: int, uuid: str, dimmer: int) -> object:
    state = UnitState()
    state.dimmer = dimmer
    state.temperature = 3000
    state.xy = (0.3, 0.3)
    return _make_unit(
        50 + device_id,
        device_id,
        uuid,
        f"Tunable {device_id}",
        [
            UnitControl(UnitControlType.DIMMER, 0, 8, 0, False),
            UnitControl(UnitControlType.TEMPERATURE, 8, 8, 0, False, 2700, 6500),
            UnitControl(UnitControlType.XY, 16, 16, 0, False),
        ],
        state,
        state_length=4,
    )


async def test_groups_with_motors_or_no_light_get_no_light(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that only groups of lights become light entities.

    A group command reaches every member, and the screen's position is its
    dimmer: "turn off all lights" would have moved the pergola. A group
    with nothing a light command can drive used to report the UNKNOWN
    color mode, which Home Assistant refuses.
    """
    light = mock_casambi.units[0]
    screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
    louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
    relay = unit_by_uuid(mock_casambi, WINSOL_RELAY_UUID)
    mock_casambi.groups += [
        Group(2, "Terrace", [light, screen]),
        Group(3, "Roof", [louvre]),
        Group(4, "Heating", [relay]),
        Group(5, "Empty", []),
    ]
    # A light the screen group used to have is cleaned up.
    mock_config_entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        LIGHT_DOMAIN,
        DOMAIN,
        f"{NETWORK_ID}-group-2",
        config_entry=mock_config_entry,
        suggested_object_id="terrace",
    )

    await _setup(hass, mock_config_entry)

    assert _group_light(hass, 1) is not None
    for group_id in (2, 3, 4, 5):
        assert _group_light(hass, group_id) is None, group_id
    assert hass.states.get("light.terrace") is None


async def test_group_light_ignores_relays_for_its_modes(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a relay in a group of lights does not shape the light."""
    relay = unit_by_uuid(mock_casambi, WINSOL_RELAY_UUID)
    tunable = _tunable_light(30, "unit-tunable-a", 200)
    mock_casambi.units.append(tunable)
    mock_casambi.groups.append(Group(6, "Garden", [relay, tunable]))

    await _setup(hass, mock_config_entry)

    entity_id = _group_light(hass, 6)
    assert entity_id is not None
    modes = hass.states.get(entity_id).attributes["supported_color_modes"]
    assert set(modes) == {"color_temp", "xy"}


async def test_group_color_change_keeps_the_lights_on(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test a color change on a group whose first member is off.

    The brightness used to come from the first member whatever its state,
    and was sent back to the group: a color change switched it off.
    """
    off = _tunable_light(31, "unit-tunable-off", 0)
    off._on = False  # noqa: SLF001
    lit = _tunable_light(32, "unit-tunable-on", 200)
    mock_casambi.units += [off, lit]
    mock_casambi.groups.append(Group(7, "Lounge", [off, lit]))
    await _setup(hass, mock_config_entry)
    entity_id = _group_light(hass, 7)
    assert entity_id is not None
    assert hass.states.get(entity_id).attributes["brightness"] == 200

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_COLOR_TEMP_KELVIN: 4000},
        blocking=True,
    )
    mock_casambi.setTemperature.assert_awaited_once()
    mock_casambi.setLevel.assert_awaited_once()
    assert mock_casambi.setLevel.await_args.args[1] == 200

    # All off: the color is set, then the group is turned on at its last
    # level instead of being sent level 0.
    lit._on = False  # noqa: SLF001
    lit.state.dimmer = 0
    mock_casambi.setLevel.reset_mock()
    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_XY_COLOR: (0.4, 0.4)},
        blocking=True,
    )
    mock_casambi.setColorXY.assert_awaited_once()
    assert mock_casambi.setColorXY.await_args.args[1] == (0.4, 0.4)
    mock_casambi.setLevel.assert_not_awaited()
    mock_casambi.turnOn.assert_awaited_once()


async def test_white_only_unit_gets_no_light(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a unit without a usable color mode creates no light."""
    white = _make_unit(
        90,
        40,
        "unit-white-only",
        "White channel",
        [UnitControl(UnitControlType.WHITE, 0, 8, 0, False)],
        UnitState(),
    )
    mock_casambi.units.append(white)
    await _setup(hass, mock_config_entry)

    assert (
        er.async_get(hass).async_get_entity_id(
            LIGHT_DOMAIN, DOMAIN, f"{NETWORK_ID}-unit-unit-white-only-light"
        )
        is None
    )
    # The rest of the network is still set up.
    assert _group_light(hass, 1) is not None


async def test_group_with_the_heating_relay_reads_off_with_its_lights_off(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a relay in a group of lights does not make it read on.

    The relay ignores the group's light commands, so a group reading on
    while the heating runs could not be switched off from Home Assistant.
    """
    relay = unit_by_uuid(mock_casambi, WINSOL_RELAY_UUID)
    relay.state.onoff = True
    lamp = _tunable_light(33, "unit-tunable-garden", 0)
    lamp._on = False  # noqa: SLF001
    mock_casambi.units.append(lamp)
    mock_casambi.groups.append(Group(8, "Garden", [relay, lamp]))
    await _setup(hass, mock_config_entry)

    entity_id = _group_light(hass, 8)
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "off"
