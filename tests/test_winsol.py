"""Tests for Winsol pergola support: louvre cover, screen cover, sensors."""

from collections import Counter
from dataclasses import replace
from datetime import timedelta
from unittest.mock import MagicMock, patch

from CasambiBt import Unit, UnitControl, UnitControlType
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.casambi_bt.classify import UnitKind, classify_unit
from custom_components.casambi_bt.const import DOMAIN
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    DOMAIN as COVER_DOMAIN,
    SERVICE_CLOSE_COVER,
    SERVICE_OPEN_COVER,
    SERVICE_SET_COVER_POSITION,
    SERVICE_STOP_COVER,
    CoverEntityFeature,
)
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_STATE_CHANGED,
    EVENT_STATE_REPORTED,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util

from .conftest import (
    NETWORK_ID,
    SENSOR_PLATFORM_UUID,
    WINSOL_LOUVRE_UUID,
    WINSOL_RELAY_UUID,
    WINSOL_SCREEN_UUID,
    WINSOL_STAR_LOUVRE_UUID,
    entity_id_for as _entity_id,
    make_light_unit,
    make_sensor_platform_unit,
    make_vertical_light_unit,
    make_winsol_louvre_unit,
    make_winsol_relay_unit,
    make_winsol_screen_unit,
    make_winsol_star_louvre_unit,
    setup_entry as _setup,
    unit_by_uuid,
)


def test_classification() -> None:
    """Test that units are classified by their control layout."""
    assert classify_unit(make_light_unit()) is UnitKind.LIGHT
    assert classify_unit(make_vertical_light_unit()) is UnitKind.LIGHT
    assert classify_unit(make_winsol_louvre_unit()) is UnitKind.LOUVRE
    assert classify_unit(make_winsol_screen_unit()) is UnitKind.SCREEN
    assert classify_unit(make_winsol_relay_unit()) is UnitKind.RELAY
    assert classify_unit(make_sensor_platform_unit()) is UnitKind.SENSOR_PLATFORM
    assert classify_unit(make_winsol_star_louvre_unit()) is UnitKind.UNSUPPORTED_MOTOR


async def test_star_louvre_is_not_driven(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a Lamel Intelligent gets no entities but a repair issue."""
    mock_casambi.units.append(make_winsol_star_louvre_unit())
    await _setup(hass, mock_config_entry)

    registry = er.async_get(hass)
    assert not [
        e
        for e in er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
        if WINSOL_STAR_LOUVRE_UUID in e.unique_id
    ]
    # The tested louvre is still a cover.
    _entity_id(hass, COVER_DOMAIN, f"{NETWORK_ID}-unit-{WINSOL_LOUVRE_UUID}-cover")

    issue_id = (
        f"unsupported_motor_{mock_config_entry.entry_id}_{WINSOL_STAR_LOUVRE_UUID}"
    )
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "unsupported_motor"
    assert issue.translation_placeholders == {
        "name": "Star Louvres",
        "model": "Winsol Lamel Intelligent",
    }

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


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
    """Test that an on/off relay is a switch writing its ONOFF bit.

    The library's turnOn sends a SetLevel, which the Winsol On-Off ignores,
    so both directions write the bit.
    """
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
    mock_casambi.setControlValue.assert_awaited_once()
    target, control, value = mock_casambi.setControlValue.await_args.args
    assert isinstance(target, Unit)
    assert target.uuid == WINSOL_RELAY_UUID
    assert (control.offset, value) == (0, 1)
    mock_casambi.turnOn.assert_not_awaited()
    mock_casambi.setLevel.assert_not_awaited()

    # The network reports the relay closed.
    relay = unit_by_uuid(mock_casambi, WINSOL_RELAY_UUID)
    relay.state.onoff = True
    mock_casambi.registerUnitChangedHandler.call_args.args[0](relay)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_ON

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    target, control, value = mock_casambi.setControlValue.await_args.args
    assert target.uuid == WINSOL_RELAY_UUID
    assert (control.offset, value) == (0, 0)
    mock_casambi.turnOff.assert_not_awaited()


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

    # With the start/stop bit clear (at rest, or moving after a position
    # command, which the louvres don't report) setting the bit would start
    # or reverse the louvres, so stop sends nothing.
    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_STOP_COVER,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    mock_casambi.setControlValue.assert_not_awaited()

    # Running from the wall button (bit 36 set): stop clears the bit.
    louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
    louvre.state._raw_state = bytes.fromhex("1101000010")  # noqa: SLF001
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
    assert value == 0


async def test_louvre_channel_sensors(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the multiplexed sensor channels of the louvre motor.

    Named after the Casambi fixture definition (sensor tag = channel - 1):
    Temperatuur (°C), #Overcurrent, Travel Distance, Module State.
    """
    await _setup(hass, mock_config_entry)

    prefix = f"{mock_casambi.networkId}-unit-{WINSOL_LOUVRE_UUID}"
    registry = er.async_get(hass)
    channel_ids = [
        _entity_id(hass, "sensor", f"{prefix}-channel-{n}") for n in range(1, 5)
    ]
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{prefix}-channel-5") is None

    expected = [
        # (name, device class, unit, state class, category)
        ("Temperature", "temperature", "°C", "measurement", None),
        ("Overcurrent events", None, None, "total_increasing", "diagnostic"),
        ("Travel distance", None, None, "total_increasing", "diagnostic"),
        ("Module state", None, None, "measurement", "diagnostic"),
    ]
    for entity_id, (name, device_class, unit, state_class, category) in zip(
        channel_ids, expected, strict=True
    ):
        entry = registry.async_get(entity_id)
        assert entry is not None
        assert entry.entity_category == category
        attributes = hass.states.get(entity_id).attributes
        assert attributes["friendly_name"] == f"Pergola Louvres {name}"
        assert attributes.get("device_class") == device_class
        assert attributes.get("unit_of_measurement") == unit
        assert attributes["state_class"] == state_class

    # The all-zero initial state carries channel 0, which is nobody's.
    for entity_id in channel_ids:
        assert hass.states.get(entity_id).state == STATE_UNKNOWN

    # One full rotation captured on a Winsol Lamel Standard (louvres closed):
    # 17 °C, no overcurrent, travel distance 29, module state 50.
    louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
    handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]
    for packet in ("1101000000", "0200000000", "d301000000", "2403000000"):
        louvre.state._raw_state = bytes.fromhex(packet)  # noqa: SLF001
        handler(louvre)
        await hass.async_block_till_done()

    assert [hass.states.get(e).state for e in channel_ids] == ["17", "0", "29", "50"]

    # Screens multiplex travel times the same way but get no channel sensors.
    screen_prefix = f"{mock_casambi.networkId}-unit-{WINSOL_SCREEN_UUID}"
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{screen_prefix}-channel-1")
        is None
    )


async def test_louvre_entities_write_only_what_changed(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a pushed state is written only by the entities it changes.

    The louvre motor pushes its whole state every few seconds while it
    cycles through its sensor channels, and every entity of the unit is
    notified each time. A channel sensor writes once, when its channel comes
    round; the automation switches and dials, whose values are not in the
    pushed state, never write; the cover writes on every push.
    """
    await _setup(hass, mock_config_entry)

    prefix = f"{mock_casambi.networkId}-unit-{WINSOL_LOUVRE_UUID}"
    cover_id = _entity_id(hass, COVER_DOMAIN, f"{prefix}-cover")
    channel_ids = [
        _entity_id(hass, "sensor", f"{prefix}-channel-{n}") for n in range(1, 5)
    ]
    config_ids = [
        _entity_id(hass, SWITCH_DOMAIN, f"{prefix}-sun-tracking"),
        _entity_id(hass, SWITCH_DOMAIN, f"{prefix}-temperature-control"),
        _entity_id(hass, SWITCH_DOMAIN, f"{prefix}-frost-lock"),
        _entity_id(hass, "number", f"{prefix}-sun-offset"),
        _entity_id(hass, "number", f"{prefix}-temp-setpoint"),
    ]

    writes: Counter[str] = Counter()

    @callback
    def _count(event: Event) -> None:
        writes[event.data["entity_id"]] += 1

    @callback
    def _any(_data: object) -> bool:
        return True

    hass.bus.async_listen(EVENT_STATE_CHANGED, _count)
    hass.bus.async_listen(EVENT_STATE_REPORTED, _count, event_filter=_any)

    louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
    handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]
    for packet in ("1101000000", "0200000000", "d301000000", "2403000000"):
        louvre.state._raw_state = bytes.fromhex(packet)  # noqa: SLF001
        handler(louvre)
        await hass.async_block_till_done()

    assert [writes[e] for e in channel_ids] == [1, 1, 1, 1]
    assert [writes[e] for e in config_ids] == [0, 0, 0, 0, 0]
    assert writes[cover_id] == 4

    # The same packet again changes nothing a sensor shows.
    handler(louvre)
    await hass.async_block_till_done()
    assert [writes[e] for e in channel_ids] == [1, 1, 1, 1]
    assert writes[cover_id] == 5


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


async def test_screen_shows_estimated_position_while_moving(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a moving screen shows closing and an estimated position.

    The SO! reports its target the moment a close is sent; showing that as
    the position made the screen look closed straight away.
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
        screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
        handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]

        async def report(packet: str) -> None:
            screen.setStateFromBytes(bytes.fromhex(packet))
            handler(screen)
            await hass.async_block_till_done()

        async def tick(seconds: float) -> None:
            clock[0] += seconds
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
            await hass.async_block_till_done()

        # Captured SO! V4.1 states: retracted at rest (channel 1 = Time Up,
        # 35000 ms), let the fixture's own move from 64 finish first.
        await report("818b080000")
        clock[0] = 1020.0
        await tick(0)
        state = hass.states.get(entity_id)
        assert state.state == "open"
        assert state.attributes[ATTR_CURRENT_POSITION] == 100

        # Close: the screen reports target 255 at once.
        await report("818b08f00f")
        state = hass.states.get(entity_id)
        assert state.state == "closing"
        assert state.attributes[ATTR_CURRENT_POSITION] == 100

        # Halfway through the 35 s travel.
        await tick(17.5)
        state = hass.states.get(entity_id)
        assert state.state == "closing"
        assert state.attributes[ATTR_CURRENT_POSITION] == 50

        # Travel time elapsed: closed, and the refresh stops.
        await tick(18)
        state = hass.states.get(entity_id)
        assert state.state == "closed"
        assert state.attributes[ATTR_CURRENT_POSITION] == 0

        # Open again from closed: opening.
        await report("818b080000")
        await tick(7)
        state = hass.states.get(entity_id)
        assert state.state == "opening"
        assert state.attributes[ATTR_CURRENT_POSITION] == 20


async def test_screen_button_run(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test a run started by the wall button: shown, and stopped like it.

    Replays a SO! V4.1 driven by its wall button: press (bit 36 set, the
    screen extends), press (bit clear, then target 42 = where it stopped
    after 5.8 s), press (bit set again, it retracts).
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
        screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
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

        await report("818b080000")
        clock[0] = 1100.0

        # First press: the bit is set, the reported target stays at 0.
        await report("828b080010")
        assert hass.states.get(entity_id).state == "closing"

        clock[0] = 1105.8
        await stop()
        mock_casambi.setLevel.assert_not_awaited()
        mock_casambi.setControlValue.assert_awaited_once()
        _, control, value = mock_casambi.setControlValue.await_args.args
        assert (control.offset, value) == (36, 0)

        # The screen clears the bit, then reports where it stopped.
        await report("818b080000")
        # Frozen at the estimate, 5.8 s of a 35 s travel = raw 42 ...
        state = hass.states.get(entity_id)
        assert state.state == "open"
        assert state.attributes[ATTR_CURRENT_POSITION] == 84
        # ... which is exactly where the screen reports it stopped.
        await report("818b08a002")
        assert hass.states.get(entity_id).attributes[ATTR_CURRENT_POSITION] == 84

        # Third press: the bit is set again and it runs the other way.
        clock[0] = 1110.0
        await report("828b08a012")
        assert hass.states.get(entity_id).state == "opening"


def _bit_writes(mock_casambi: MagicMock) -> list[tuple[int, int]]:
    return [
        (call.args[1].offset, call.args[2])
        for call in mock_casambi.setControlValue.await_args_list
    ]


async def test_screen_open_close_like_wall_button(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that open/close run the screen like its wall button.

    The start/stop bit sends the motor opposite its last run, so open and
    close set it when that is the way asked for (and stop clears it), and
    fall back to a position command when it would go the wrong way.
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
        screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
        handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]

        async def report(packet: str) -> None:
            screen.setStateFromBytes(bytes.fromhex(packet))
            handler(screen)
            await hass.async_block_till_done()

        async def call(service: str) -> None:
            await hass.services.async_call(
                COVER_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
            )

        # Retracted (open) and at rest.
        await report("818b080000")
        clock[0] = 1100.0

        # Close from the open end: only the bit can go down -> set it.
        await call(SERVICE_CLOSE_COVER)
        assert _bit_writes(mock_casambi) == [(36, 1)]
        mock_casambi.setLevel.assert_not_awaited()
        await report("828b080010")
        assert hass.states.get(entity_id).state == "closing"

        # Stop clears the bit at once; the screen reports raw 42.
        clock[0] = 1105.8
        await call(SERVICE_STOP_COVER)
        assert _bit_writes(mock_casambi)[-1] == (36, 0)
        await report("818b080000")
        await report("818b08a002")

        # Last run went down, so the bit goes up: open sets it.
        clock[0] = 1110.0
        await call(SERVICE_OPEN_COVER)
        assert _bit_writes(mock_casambi)[-1] == (36, 1)
        await report("828b08a012")
        assert hass.states.get(entity_id).state == "opening"
        clock[0] = 1111.6
        await call(SERVICE_STOP_COVER)
        assert _bit_writes(mock_casambi)[-1] == (36, 0)
        await report("818b080010")
        await report("818b08e001")  # stopped at raw 30

        # Last run went up, so the bit would go down: open falls back to a
        # position command ...
        clock[0] = 1120.0
        await call(SERVICE_OPEN_COVER)
        mock_casambi.setLevel.assert_awaited_once()
        assert mock_casambi.setLevel.await_args.args[1] == 0
        assert len(_bit_writes(mock_casambi)) == 4

        # ... and close, which goes down, uses the bit again.
        await report("818b080000")
        clock[0] = 1200.0
        await call(SERVICE_CLOSE_COVER)
        assert _bit_writes(mock_casambi)[-1] == (36, 1)


async def test_louvres_open_close_like_wall_button(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that the louvres also run and stop like their wall button."""
    await _setup(hass, mock_config_entry)
    entity_id = _entity_id(
        hass,
        COVER_DOMAIN,
        f"{mock_casambi.networkId}-unit-{WINSOL_LOUVRE_UUID}-cover",
    )
    louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
    handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]

    async def report(packet: str) -> None:
        louvre.setStateFromBytes(bytes.fromhex(packet))
        handler(louvre)
        await hass.async_block_till_done()

    async def call(service: str) -> None:
        await hass.services.async_call(
            COVER_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    # Captured Lamel states: fully open (slider 255) at rest.
    await report("020000f00f")
    await call(SERVICE_CLOSE_COVER)
    assert _bit_writes(mock_casambi) == [(36, 1)]
    mock_casambi.setSlider.assert_not_awaited()

    await report("410100f01f")
    assert hass.states.get(entity_id).state == "closing"
    await call(SERVICE_STOP_COVER)
    assert _bit_writes(mock_casambi)[-1] == (36, 0)

    # The bit clears with the stale rest position (255), then the louvres
    # report where they stopped (slider 103). The last run closed, so open
    # uses the bit, which now goes the other way.
    await report("410100f00f")
    await report("4101007006")
    await call(SERVICE_OPEN_COVER)
    assert _bit_writes(mock_casambi)[-1] == (36, 1)
    mock_casambi.setSlider.assert_not_awaited()


async def test_louvres_reverse_while_running(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test open during a close started from Home Assistant.

    Like pressing the button twice, the bit is cleared and set again. The
    motor echoes both writes (a clear with the stale rest, then the set
    bit); neither is a rest or a new run, so the louvres keep showing
    opening. After the stop that follows, the stop position is the end of
    a run whose direction is known, not a position-driven move the other
    way.
    """
    await _setup(hass, mock_config_entry)
    entity_id = _entity_id(
        hass,
        COVER_DOMAIN,
        f"{mock_casambi.networkId}-unit-{WINSOL_LOUVRE_UUID}-cover",
    )
    louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
    handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]

    async def report(packet: str) -> None:
        louvre.setStateFromBytes(bytes.fromhex(packet))
        handler(louvre)
        await hass.async_block_till_done()

    async def call(service: str) -> None:
        await hass.services.async_call(
            COVER_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    await report("020000f00f")
    await call(SERVICE_CLOSE_COVER)
    await report("410100f01f")
    assert hass.states.get(entity_id).state == "closing"

    await call(SERVICE_OPEN_COVER)
    assert _bit_writes(mock_casambi) == [(36, 1), (36, 0), (36, 1)]
    await report("410100f00f")
    await report("410100f01f")
    assert hass.states.get(entity_id).state == "opening"

    # Stop: the stale rest (255) is echoed, then the real stop at 153.
    await call(SERVICE_STOP_COVER)
    assert _bit_writes(mock_casambi)[-1] == (36, 0)
    await report("410100f00f")
    await report("4101009009")
    state = hass.states.get(entity_id)
    assert state.state == "open"
    assert state.attributes[ATTR_CURRENT_POSITION] == 60

    # The last run opened, so the bit would close: close uses it, open
    # would have to send a position.
    await call(SERVICE_CLOSE_COVER)
    assert _bit_writes(mock_casambi)[-1] == (36, 1)
    mock_casambi.setSlider.assert_not_awaited()


async def test_louvres_forget_the_direction_when_the_link_drops(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a reconnect does not trust the direction learnt before.

    The wall button may have run the louvres any number of times while
    Home Assistant could not see them, so the bit may go either way: open
    and close fall back to a position until a run is seen again.
    """
    await _setup(hass, mock_config_entry)
    api = mock_config_entry.runtime_data
    entity_id = _entity_id(
        hass,
        COVER_DOMAIN,
        f"{mock_casambi.networkId}-unit-{WINSOL_LOUVRE_UUID}-cover",
    )
    louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
    handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]

    async def report(packet: str) -> None:
        louvre.setStateFromBytes(bytes.fromhex(packet))
        handler(louvre)
        await hass.async_block_till_done()

    async def call(service: str) -> None:
        await hass.services.async_call(
            COVER_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    # A button run from fully open stops at 103: the last run closed.
    await report("020000f00f")
    await report("410100f01f")
    await report("410100f00f")
    await report("4101007006")

    mock_casambi.connected = False
    api._notify_connection_state()  # noqa: SLF001
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"
    mock_casambi.connected = True
    api._notify_connection_state()  # noqa: SLF001
    await report("4101007006")

    # Open would have used the bit before the drop; now it sends a position.
    await call(SERVICE_OPEN_COVER)
    mock_casambi.setControlValue.assert_not_awaited()
    mock_casambi.setSlider.assert_awaited_once()
    assert mock_casambi.setSlider.await_args.args[1] == 255


async def test_screen_open_reverses_a_position_move(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test open and close during a move started with a position.

    The screen ignores a position behind it, so the opposite end is
    reached with the bit, which reverses the motor; the same end is just
    sent as a position, or nothing when it is the target already.
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
        screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
        handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]

        async def report(packet: str) -> None:
            screen.setStateFromBytes(bytes.fromhex(packet))
            handler(screen)
            await hass.async_block_till_done()

        async def call(service: str) -> None:
            await hass.services.async_call(
                COVER_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
            )

        # At rest, open. A position command sends the screen to 60 %.
        await report("818b080000")
        clock[0] = 1100.0
        await hass.services.async_call(
            COVER_DOMAIN,
            SERVICE_SET_COVER_POSITION,
            {ATTR_ENTITY_ID: entity_id, ATTR_POSITION: 60},
            blocking=True,
        )
        assert mock_casambi.setLevel.await_args.args[1] == 102
        await report("818b086006")
        assert hass.states.get(entity_id).state == "closing"

        # Close goes the same way: the end is sent as a position.
        clock[0] = 1105.0
        await call(SERVICE_CLOSE_COVER)
        assert mock_casambi.setLevel.await_args.args[1] == 255
        mock_casambi.setControlValue.assert_not_awaited()
        await report("818b08f00f")
        assert hass.states.get(entity_id).state == "closing"
        await call(SERVICE_CLOSE_COVER)
        assert mock_casambi.setLevel.await_count == 2

        # Open goes the other way: only the bit reverses the motor.
        clock[0] = 1110.0
        await call(SERVICE_OPEN_COVER)
        assert _bit_writes(mock_casambi) == [(36, 1)]
        assert mock_casambi.setLevel.await_count == 2
        state = hass.states.get(entity_id)
        assert state.state == "opening"
        assert state.attributes[ATTR_CURRENT_POSITION] == 71

        # The motor echoes the bit with the stale target: still opening.
        await report("828b08f01f")
        assert hass.states.get(entity_id).state == "opening"

        # Stop clears the bit, like any button run.
        clock[0] = 1115.0
        await call(SERVICE_STOP_COVER)
        assert _bit_writes(mock_casambi)[-1] == (36, 0)
        await report("818b08f00f")
        state = hass.states.get(entity_id)
        assert state.state == "open"
        assert state.attributes[ATTR_CURRENT_POSITION] == 86


@pytest.mark.parametrize(
    ("rest", "move", "expected"),
    [
        # Closing from retracted: 7 s + 1.5 s ahead of a 35 s travel,
        # rounded further down the way -> raw 62.
        ("818b080000", "818b08f00f", 62),
        # Opening from extended: same distance the other way -> raw 193.
        ("818b08f00f", "818b080000", 193),
    ],
)
async def test_screen_stop_aims_ahead(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
    rest: str,
    move: str,
    expected: int,
) -> None:
    """Test that stop sends a position just ahead of the moving screen.

    On a Winsol SO! the onoff ($toggle) control reverses the motion and a
    position behind the moving screen is ignored, while one ahead of it
    stops it there.
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
        assert (
            hass.states.get(entity_id).attributes["supported_features"]
            & CoverEntityFeature.STOP
        )
        screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
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

        # At rest (the fixture's own move from 64 has long finished).
        await report(rest)
        clock[0] = 1100.0
        await stop()
        mock_casambi.setLevel.assert_not_awaited()

        await report(move)
        clock[0] = 1107.0
        await stop()

        mock_casambi.setLevel.assert_awaited_once()
        assert mock_casambi.setLevel.await_args.args[1] == expected

        # Within the margin of the end the screen arrives anyway.
        mock_casambi.setLevel.reset_mock()
        await report(rest)
        clock[0] = 1200.0
        await report(move)
        clock[0] = 1234.0
        await stop()
        mock_casambi.setLevel.assert_not_awaited()

    # The toggle, which reverses a SO!, is never used on a screen.
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


async def test_screen_travel_time_per_direction(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that extending uses Time Down and retracting uses Time Up."""
    await _setup(hass, mock_config_entry)
    entity_id = _entity_id(
        hass, COVER_DOMAIN, f"{NETWORK_ID}-unit-{WINSOL_SCREEN_UUID}-cover"
    )
    entity = hass.data["entity_components"][COVER_DOMAIN].get_entity(entity_id)
    assert entity is not None

    # Channel 1 is the fixture's "Time Up", channel 2 its "Time Down".
    entity._travel_ms = {1: 40000, 2: 20000}  # noqa: SLF001
    assert entity._travel_seconds(extending=True) == 20.0  # noqa: SLF001
    assert entity._travel_seconds(extending=False) == 40.0  # noqa: SLF001


async def test_screen_forgets_motion_when_the_link_drops(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a reconnect does not animate a move from a stale target."""
    clock = [1000.0]
    with patch(
        "custom_components.casambi_bt.cover.monotonic", side_effect=lambda: clock[0]
    ):
        await _setup(hass, mock_config_entry)
        api = mock_config_entry.runtime_data
        entity_id = _entity_id(
            hass,
            COVER_DOMAIN,
            f"{mock_casambi.networkId}-unit-{WINSOL_SCREEN_UUID}-cover",
        )
        screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
        handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]

        async def report(packet: str) -> None:
            screen.setStateFromBytes(bytes.fromhex(packet))
            handler(screen)
            await hass.async_block_till_done()

        await report("818b080000")
        clock[0] = 1040.0
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
        await hass.async_block_till_done()

        # A close starts, then the link drops mid-travel.
        await report("818b08f00f")
        assert hass.states.get(entity_id).state == "closing"
        clock[0] += 5
        mock_casambi.connected = False
        api._notify_connection_state()  # noqa: SLF001
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == "unavailable"

        # Back after a while: the screen was driven meanwhile and now reports
        # the retracted end. That is taken as rest, not as a 35 s opening.
        clock[0] += 600
        mock_casambi.connected = True
        api._notify_connection_state()  # noqa: SLF001
        await report("818b080000")
        state = hass.states.get(entity_id)
        assert state.state == "open"
        assert state.attributes[ATTR_CURRENT_POSITION] == 100


async def test_screen_button_stop_keeps_the_run_direction(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that the position reported after a button stop is not a run.

    It differs from the frozen estimate by the estimate's error; read as a
    run, its direction would decide which way the bit sends the motor next.
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
        screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
        handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]

        async def report(packet: str) -> None:
            screen.setStateFromBytes(bytes.fromhex(packet))
            handler(screen)
            await hass.async_block_till_done()

        await report("818b080000")
        clock[0] = 1100.0
        # Button: the screen extends; button again after 5.8 s: it stops,
        # estimated at raw 42.
        await report("828b080010")
        clock[0] = 1105.8
        await report("818b080000")
        # It reports it stopped at 40, two short of the estimate.
        await report("818b088002")
        state = hass.states.get(entity_id)
        assert state.state == "open"
        assert state.attributes[ATTR_CURRENT_POSITION] == 84

        # The last run extended, so the bit would retract: close sends a
        # position instead of the bit.
        await hass.services.async_call(
            COVER_DOMAIN,
            SERVICE_CLOSE_COVER,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )
        mock_casambi.setControlValue.assert_not_awaited()
        mock_casambi.setLevel.assert_awaited_once()
        assert mock_casambi.setLevel.await_args.args[1] == 255


async def test_screen_button_run_across_a_link_drop(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test a button run under way when the link returns.

    Nothing is known about where the screen is or which way it runs, so
    neither an end nor a direction is assumed: the position reported when
    the run ends is taken as is.
    """
    clock = [1000.0]
    with patch(
        "custom_components.casambi_bt.cover.monotonic", side_effect=lambda: clock[0]
    ):
        await _setup(hass, mock_config_entry)
        api = mock_config_entry.runtime_data
        entity_id = _entity_id(
            hass,
            COVER_DOMAIN,
            f"{mock_casambi.networkId}-unit-{WINSOL_SCREEN_UUID}-cover",
        )
        screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
        handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]

        async def report(packet: str) -> None:
            screen.setStateFromBytes(bytes.fromhex(packet))
            handler(screen)
            await hass.async_block_till_done()

        await report("818b080000")
        clock[0] = 1100.0
        await report("828b080010")
        assert hass.states.get(entity_id).state == "closing"
        clock[0] += 5
        mock_casambi.connected = False
        api._notify_connection_state()  # noqa: SLF001
        await hass.async_block_till_done()

        # Back ten minutes later, in the middle of another button run.
        clock[0] += 600
        mock_casambi.connected = True
        api._notify_connection_state()  # noqa: SLF001
        await report("828b080010")
        assert hass.states.get(entity_id).state == "open"

        # The run ends and the screen reports where it stopped: that is the
        # position, not a run from the stale end.
        await report("818b080000")
        await report("818b088007")
        state = hass.states.get(entity_id)
        assert state.state == "open"
        assert state.attributes[ATTR_CURRENT_POSITION] == 53

        # Nothing was learnt about the direction, so close sends a position.
        await hass.services.async_call(
            COVER_DOMAIN,
            SERVICE_CLOSE_COVER,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )
        mock_casambi.setControlValue.assert_not_awaited()
        mock_casambi.setLevel.assert_awaited_once()
