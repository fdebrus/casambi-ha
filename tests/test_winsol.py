"""Tests for Winsol pergola support: louvre cover, screen cover, sensors."""

from dataclasses import replace
from unittest.mock import MagicMock, patch

from CasambiBt import Unit, UnitControl, UnitControlType
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
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import (
    NETWORK_ID,
    SENSOR_PLATFORM_UUID,
    WINSOL_LOUVRE_UUID,
    WINSOL_RELAY_UUID,
    WINSOL_SCREEN_UUID,
    make_light_unit,
    make_louvre_unit,
    make_sensor_platform_unit,
    make_winsol_louvre_unit,
    make_winsol_relay_unit,
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
    assert classify_unit(make_winsol_relay_unit()) is UnitKind.RELAY
    assert classify_unit(make_sensor_platform_unit()) is UnitKind.SENSOR_PLATFORM


def test_onoff_luminaire_stays_a_light() -> None:
    """Test that an on/off unit outside EXT/ mode is still a light."""
    relay = make_winsol_relay_unit()
    relay.unitType = replace(relay.unitType, mode="Relay/1ch")
    assert classify_unit(relay) is UnitKind.LIGHT

    # A read-only on/off bit cannot be switched, so it is not a relay and
    # keeps the previous behavior.
    readonly = make_winsol_relay_unit()
    readonly.unitType = replace(
        readonly.unitType,
        controls=[UnitControl(UnitControlType.ONOFF, 0, 1, 0, True)],
    )
    assert classify_unit(readonly) is UnitKind.LIGHT


async def test_motor_units_are_not_lights(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that louvre/screen/relay/sensor units don't create lights."""
    await _setup(hass, mock_config_entry)

    registry = er.async_get(hass)
    for uuid in (
        WINSOL_LOUVRE_UUID,
        WINSOL_SCREEN_UUID,
        WINSOL_RELAY_UUID,
        SENSOR_PLATFORM_UUID,
    ):
        assert (
            registry.async_get_entity_id(
                "light", DOMAIN, f"{mock_casambi.networkId}-unit-{uuid}-light"
            )
            is None
        )


async def test_relay_switch(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that an on/off relay is a switch driven by turnOn/turnOff."""
    await _setup(hass, mock_config_entry)

    entity_id = _entity_id(
        hass, SWITCH_DOMAIN, f"{mock_casambi.networkId}-unit-{WINSOL_RELAY_UUID}-relay"
    )
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_OFF
    assert state.attributes["device_class"] == "switch"

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    mock_casambi.turnOn.assert_awaited_once()
    (target,) = mock_casambi.turnOn.await_args.args
    assert isinstance(target, Unit)
    assert target.uuid == WINSOL_RELAY_UUID
    mock_casambi.setLevel.assert_not_awaited()

    # The network reports the relay closed.
    relay = next(u for u in mock_casambi.units if u.uuid == WINSOL_RELAY_UUID)
    relay.state.onoff = True
    mock_casambi.registerUnitChangedHandler.call_args.args[0](relay)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_ON

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    mock_casambi.turnOff.assert_awaited_once()
    (target,) = mock_casambi.turnOff.await_args.args
    assert target.uuid == WINSOL_RELAY_UUID


async def test_relay_stale_light_is_removed(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that the light entity a relay used to have is cleaned up."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    stale_unique_id = f"{NETWORK_ID}-unit-{WINSOL_RELAY_UUID}-light"
    stale = registry.async_get_or_create(
        LIGHT_DOMAIN,
        DOMAIN,
        stale_unique_id,
        config_entry=mock_config_entry,
        suggested_object_id="on_off",
    )
    assert stale.entity_id == "light.on_off"

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, stale_unique_id) is None
    assert hass.states.get("light.on_off") is None
    assert (
        registry.async_get_entity_id(
            SWITCH_DOMAIN, DOMAIN, f"{NETWORK_ID}-unit-{WINSOL_RELAY_UUID}-relay"
        )
        == "switch.on_off"
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


async def test_louvre_channel_sensors(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the raw multiplexed sensor channels of the louvre motor."""
    await _setup(hass, mock_config_entry)

    prefix = f"{mock_casambi.networkId}-unit-{WINSOL_LOUVRE_UUID}"
    registry = er.async_get(hass)
    channel_ids = [
        _entity_id(hass, "sensor", f"{prefix}-channel-{n}") for n in range(1, 5)
    ]
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{prefix}-channel-5") is None
    entry = registry.async_get(channel_ids[0])
    assert entry is not None
    assert entry.entity_category is er.EntityCategory.DIAGNOSTIC

    # The all-zero initial state carries channel 0, which is nobody's.
    for entity_id in channel_ids:
        assert hass.states.get(entity_id).state == STATE_UNKNOWN

    # Packets captured on a Winsol Lamel Standard: channel 1 = 30 while
    # the slider reads 109, then channel 3 = 20.
    louvre = next(u for u in mock_casambi.units if u.uuid == WINSOL_LOUVRE_UUID)
    handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]
    for packet in ("e10100d006", "430100d006"):
        louvre.state._raw_state = bytes.fromhex(packet)  # noqa: SLF001
        handler(louvre)
        await hass.async_block_till_done()

    assert hass.states.get(channel_ids[0]).state == "30"
    assert hass.states.get(channel_ids[1]).state == STATE_UNKNOWN
    assert hass.states.get(channel_ids[2]).state == "20"
    assert hass.states.get(channel_ids[3]).state == STATE_UNKNOWN

    # Screens multiplex travel times the same way but get no channel sensors.
    screen_prefix = f"{mock_casambi.networkId}-unit-{WINSOL_SCREEN_UUID}"
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{screen_prefix}-channel-1")
        is None
    )


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
    # The dimmer counts how far the screen is extended, so it is inverted
    # against the Home Assistant position: dimmer 64 of 255 -> 75% open.
    assert state.attributes[ATTR_CURRENT_POSITION] == 75

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
    # 80% open -> 20% extended -> raw 51.
    assert value == 51


async def test_screen_stop_sends_reached_position(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that stop holds the screen where it is instead of reversing it.

    On a Winsol SO! the onoff ($toggle) control reverses the motion, so
    stop must send the reached position rather than toggle it.
    """
    clock = [1000.0]
    with patch(
        "custom_components.casambi_bt.cover.monotonic", side_effect=lambda: clock[0]
    ):
        await _setup(hass, mock_config_entry)
        entity_id = _entity_id(
            hass,
            COVER_DOMAIN,
            f"{mock_casambi.networkId}-unit-{WINSOL_SCREEN_UUID}-cover",
        )
        screen = next(u for u in mock_casambi.units if u.uuid == WINSOL_SCREEN_UUID)
        handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]

        async def report(packet: str) -> None:
            screen.setStateFromBytes(bytes.fromhex(packet))
            handler(screen)
            await hass.async_block_till_done()

        async def stop() -> None:
            await hass.services.async_call(
                COVER_DOMAIN,
                SERVICE_STOP_COVER,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )

        # States captured on a SO! V4.1: at rest retracted (channel 1 =
        # 35000 ms travel), then extending after a close (target 255).
        await report("818b080000")
        # Retracting from the fixture's 64 takes under 9 s; then it rests.
        clock[0] = 1020.0
        await stop()
        mock_casambi.setLevel.assert_not_awaited()

        clock[0] = 1030.0
        await report("818b08f00f")
        clock[0] = 1037.0
        await stop()

    mock_casambi.setControlValue.assert_not_awaited()
    mock_casambi.setLevel.assert_awaited_once()
    _, value = mock_casambi.setLevel.await_args.args
    # 7 s + 0.3 s latency of a 35 s full travel from 0 -> raw 53.
    assert value == round(7.3 / 35 * 255)


async def test_screen_stop_after_motion_ends_does_nothing(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that stop leaves a screen alone once it has reached its target."""
    clock = [1000.0]
    with patch(
        "custom_components.casambi_bt.cover.monotonic", side_effect=lambda: clock[0]
    ):
        await _setup(hass, mock_config_entry)
        entity_id = _entity_id(
            hass,
            COVER_DOMAIN,
            f"{mock_casambi.networkId}-unit-{WINSOL_SCREEN_UUID}-cover",
        )
        screen = next(u for u in mock_casambi.units if u.uuid == WINSOL_SCREEN_UUID)
        handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]

        screen.setStateFromBytes(bytes.fromhex("818b080000"))
        handler(screen)
        screen.setStateFromBytes(bytes.fromhex("818b08f00f"))
        handler(screen)
        await hass.async_block_till_done()

        clock[0] = 1040.0
        await hass.services.async_call(
            COVER_DOMAIN,
            SERVICE_STOP_COVER,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )

    mock_casambi.setLevel.assert_not_awaited()
    mock_casambi.setControlValue.assert_not_awaited()


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


def test_sensor_platform_detected_regardless_of_mode() -> None:
    """Test that classification does not depend on the mode string.

    Installations differ in how the mode is named, so a sensor platform
    must be recognised by its controls alone.
    """
    unit = make_sensor_platform_unit()
    unit.unitType.__dict__["mode"] = "DALI Sensor{Presence,Daylight}"
    assert classify_unit(unit) is UnitKind.SENSOR_PLATFORM

    unit.unitType.__dict__["mode"] = ""
    assert classify_unit(unit) is UnitKind.SENSOR_PLATFORM


async def test_sensor_entities_available_while_unit_offline(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that sensor platform entities ignore the unit online flag.

    Sensor platforms are passive broadcasters and always report
    online=False, which must not make their entities unavailable.
    """
    sensor_unit = make_sensor_platform_unit()
    sensor_unit._online = False  # noqa: SLF001
    mock_casambi.units = [make_light_unit(), sensor_unit]

    await _setup(hass, mock_config_entry)

    prefix = f"{mock_casambi.networkId}-unit-{SENSOR_PLATFORM_UUID}"
    for domain, key in (
        ("sensor", "wind"),
        ("sensor", "illuminance"),
        ("binary_sensor", "rain"),
        ("binary_sensor", "presence"),
        ("switch", "enable-34"),
    ):
        state = hass.states.get(_entity_id(hass, domain, f"{prefix}-{key}"))
        assert state is not None
        assert state.state != "unavailable", f"{domain}.{key} is unavailable"
