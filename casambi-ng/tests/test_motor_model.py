"""Tests for how the motor covers follow the screens and louvres they drive."""

from collections.abc import Callable
from datetime import timedelta
from unittest.mock import MagicMock, patch

from CasambiBt import Unit
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.components.cover import (
    ATTR_POSITION,
    DOMAIN as COVER_DOMAIN,
    SERVICE_CLOSE_COVER,
    SERVICE_OPEN_COVER,
    SERVICE_SET_COVER_POSITION,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import (
    NETWORK_ID,
    WINSOL_LOUVRE_UUID,
    WINSOL_SCREEN_UUID,
    entity_id_for as _entity_id,
    setup_entry as _setup,
    unit_by_uuid,
)

SCREEN_COVER_ID = f"{NETWORK_ID}-unit-{WINSOL_SCREEN_UUID}-cover"
LOUVRE_COVER_ID = f"{NETWORK_ID}-unit-{WINSOL_LOUVRE_UUID}-cover"

# SO! states: dimmer (target) bits 28-35, start/stop bit 36.
SCREEN_OPEN = "818b080000"
SCREEN_CLOSED = "818b08f00f"
SCREEN_HALF = "818b080008"  # target 128
SCREEN_RUN_FROM_OPEN = "828b080010"  # bit set, stale target 0


def _bit_writes(mock_casambi: MagicMock) -> list[tuple[int, int]]:
    return [
        (call.args[1].offset, call.args[2])
        for call in mock_casambi.setControlValue.await_args_list
    ]


class _Rig:
    """A set-up entry with a controllable clock."""

    def __init__(self, hass: HomeAssistant, mock_casambi: MagicMock) -> None:
        self.hass = hass
        self.casa = mock_casambi
        self.clock = [1000.0]
        self.handler: Callable[[Unit], None] = (
            mock_casambi.registerUnitChangedHandler.call_args.args[0]
        )

    async def report(self, unit: Unit, packet: str) -> None:
        unit.setStateFromBytes(bytes.fromhex(packet))
        self.handler(unit)
        await self.hass.async_block_till_done()

    async def tick(self, seconds: float) -> None:
        self.clock[0] += seconds
        async_fire_time_changed(self.hass, dt_util.utcnow() + timedelta(seconds=1))
        await self.hass.async_block_till_done()

    async def call(self, service: str, entity_id: str, **data: int) -> None:
        await self.hass.services.async_call(
            COVER_DOMAIN, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
        )


def _patched_clock(clock: list[float]):  # type: ignore[no-untyped-def]
    return patch(
        "custom_components.casambi_bt.motor.monotonic", side_effect=lambda: clock[0]
    )


async def test_screen_rests_where_a_position_move_ended_on_the_estimate(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test open after a close sent as a position, with no push at the end.

    The screen says nothing when it arrives. The rest position used to stay
    at the start of the move, so open thought the screen still open and
    sent nothing.
    """
    clock = [1000.0]
    with _patched_clock(clock):
        await _setup(hass, mock_config_entry)
        rig = _Rig(hass, mock_casambi)
        rig.clock = clock
        screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
        entity_id = _entity_id(hass, COVER_DOMAIN, SCREEN_COVER_ID)

        await rig.report(screen, SCREEN_OPEN)
        await rig.tick(100)
        # Open and at rest, as the next push confirms.
        await rig.report(screen, SCREEN_OPEN)
        await rig.call(SERVICE_SET_COVER_POSITION, entity_id, **{ATTR_POSITION: 0})
        await rig.report(screen, SCREEN_CLOSED)
        await rig.tick(40)
        assert hass.states.get(entity_id).state == "closed"

        mock_casambi.setLevel.reset_mock()
        await rig.call(SERVICE_OPEN_COVER, entity_id)
        assert _bit_writes(mock_casambi) or mock_casambi.setLevel.await_count


async def test_screen_without_a_stop_report_takes_the_next_target_as_a_move(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test a button run whose stop report never came.

    The screen waited for it for ever: a position sent from the app minutes
    later was taken for where the run had stopped, with no motion shown.
    """
    clock = [1000.0]
    with _patched_clock(clock):
        await _setup(hass, mock_config_entry)
        rig = _Rig(hass, mock_casambi)
        rig.clock = clock
        screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
        entity_id = _entity_id(hass, COVER_DOMAIN, SCREEN_COVER_ID)

        await rig.report(screen, SCREEN_OPEN)
        await rig.tick(100)
        await rig.report(screen, SCREEN_RUN_FROM_OPEN)
        await rig.tick(5.8)
        # The bit clears with the stale target; no stop report follows.
        await rig.report(screen, SCREEN_OPEN)
        await rig.tick(60)

        await rig.report(screen, SCREEN_HALF)
        assert hass.states.get(entity_id).state == "closing"


async def test_screen_close_while_retracting_from_the_closed_end_reverses_it(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test close during a move up from fully closed.

    Starting from 255, the end asked for was compared with the start of the
    move instead of with the way it goes, so close sent position 255 to a
    retracting screen, which ignores a position behind it.
    """
    clock = [1000.0]
    with _patched_clock(clock):
        await _setup(hass, mock_config_entry)
        rig = _Rig(hass, mock_casambi)
        rig.clock = clock
        screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
        entity_id = _entity_id(hass, COVER_DOMAIN, SCREEN_COVER_ID)

        await rig.report(screen, SCREEN_CLOSED)
        await rig.tick(100)
        await rig.call(SERVICE_SET_COVER_POSITION, entity_id, **{ATTR_POSITION: 50})
        await rig.report(screen, SCREEN_HALF)
        assert hass.states.get(entity_id).state == "opening"
        mock_casambi.setLevel.reset_mock()

        await rig.tick(3)
        await rig.call(SERVICE_CLOSE_COVER, entity_id)
        assert _bit_writes(mock_casambi) == [(36, 1)]
        mock_casambi.setLevel.assert_not_awaited()


async def test_louvre_double_tap_from_the_closed_end(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the wall button pressed twice quickly from closed louvres.

    The stop between the two presses carries the stale rest, the closed
    end, which made the second run look like another opening.
    """
    await _setup(hass, mock_config_entry)
    rig = _Rig(hass, mock_casambi)
    louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
    entity_id = _entity_id(hass, COVER_DOMAIN, LOUVRE_COVER_ID)

    await rig.report(louvre, "4101000000")
    await rig.report(louvre, "4101000010")
    assert hass.states.get(entity_id).state == "opening"
    await rig.report(louvre, "4101000000")
    await rig.report(louvre, "4101000010")
    assert hass.states.get(entity_id).state == "closing"


async def test_louvre_button_stop_right_after_an_ha_start(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the wall button stopping a run Home Assistant started a second ago.

    Every clear within 3 s of the write was taken for its echo, so the
    louvres kept showing closing after the button had stopped them.
    """
    clock = [1000.0]
    with _patched_clock(clock):
        await _setup(hass, mock_config_entry)
        rig = _Rig(hass, mock_casambi)
        rig.clock = clock
        louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
        entity_id = _entity_id(hass, COVER_DOMAIN, LOUVRE_COVER_ID)

        await rig.report(louvre, "020000f00f")
        await rig.call(SERVICE_CLOSE_COVER, entity_id)
        await rig.tick(0.5)
        await rig.report(louvre, "410100f01f")  # the echo of the set bit
        assert hass.states.get(entity_id).state == "closing"

        await rig.tick(0.5)
        await rig.report(louvre, "410100f00f")  # the button stops it
        await rig.report(louvre, "4101007006")
        state = hass.states.get(entity_id)
        assert state.state == "open"
        assert state.attributes["current_position"] == 40

        # Resting at 40 % after a close: open runs it back with the bit.
        # Taken for the run still going, it was thought fully open and
        # nothing was sent.
        mock_casambi.setControlValue.reset_mock()
        await rig.tick(10)
        await rig.call(SERVICE_OPEN_COVER, entity_id)
        assert _bit_writes(mock_casambi) == [(36, 1)]


async def test_louvre_reversal_echoes_are_not_a_stop(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a reversal's echoes still don't end the run.

    A state the motor sent before it got the write (the bit not yet
    cleared) is no echo of the clear and doesn't consume it.
    """
    clock = [1000.0]
    with _patched_clock(clock):
        await _setup(hass, mock_config_entry)
        rig = _Rig(hass, mock_casambi)
        rig.clock = clock
        louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
        entity_id = _entity_id(hass, COVER_DOMAIN, LOUVRE_COVER_ID)

        await rig.report(louvre, "020000f00f")
        await rig.call(SERVICE_CLOSE_COVER, entity_id)
        await rig.report(louvre, "410100f01f")
        await rig.tick(5)
        await rig.call(SERVICE_OPEN_COVER, entity_id)
        await rig.tick(0.2)
        await rig.report(louvre, "410100f01f")  # sent before the write
        await rig.report(louvre, "410100f00f")  # echo of the clear
        await rig.report(louvre, "410100f01f")  # echo of the set
        assert hass.states.get(entity_id).state == "opening"
        # Every write echoed: the button's stop now counts.
        await rig.tick(0.5)
        await rig.report(louvre, "410100f00f")
        assert hass.states.get(entity_id).state == "open"


async def test_screen_button_stop_right_after_an_ha_start(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the wall button stopping a screen Home Assistant just started."""
    clock = [1000.0]
    with _patched_clock(clock):
        await _setup(hass, mock_config_entry)
        rig = _Rig(hass, mock_casambi)
        rig.clock = clock
        screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
        entity_id = _entity_id(hass, COVER_DOMAIN, SCREEN_COVER_ID)

        await rig.report(screen, SCREEN_OPEN)
        await rig.tick(100)
        await rig.call(SERVICE_CLOSE_COVER, entity_id)
        await rig.tick(0.5)
        await rig.report(screen, SCREEN_RUN_FROM_OPEN)  # the echo
        assert hass.states.get(entity_id).state == "closing"

        await rig.tick(1.5)
        await rig.report(screen, SCREEN_OPEN)  # the button stops it
        await rig.tick(5)
        state = hass.states.get(entity_id)
        assert state.state == "open"
        # Stopped after 2 s of a 35 s travel.
        assert state.attributes["current_position"] == 94


async def test_screen_ignores_a_target_behind_it(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test a position sent from the app just behind a closing screen.

    The screen ignores it and goes on to the end (capture: 52 at 7.1 s into
    a close). The cover took it for a new target and showed the screen
    stopped there, and again each time the unit repeated it.
    """
    clock = [1000.0]
    with _patched_clock(clock):
        await _setup(hass, mock_config_entry)
        rig = _Rig(hass, mock_casambi)
        rig.clock = clock
        screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
        entity_id = _entity_id(hass, COVER_DOMAIN, SCREEN_COVER_ID)

        await rig.report(screen, SCREEN_OPEN)
        await rig.tick(100)
        await rig.report(screen, SCREEN_CLOSED)
        await rig.tick(7.1)
        await rig.report(screen, "818b084003")  # target 52, estimate 51.7
        assert hass.states.get(entity_id).state == "closing"

        await rig.tick(30)
        await rig.report(screen, "818b084003")  # repeated at rest
        state = hass.states.get(entity_id)
        assert state.state == "closed"
        assert state.attributes["current_position"] == 0


async def test_screen_honours_a_target_ahead_of_it(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test a position just ahead of a closing screen (65 at 8.8 s)."""
    clock = [1000.0]
    with _patched_clock(clock):
        await _setup(hass, mock_config_entry)
        rig = _Rig(hass, mock_casambi)
        rig.clock = clock
        screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
        entity_id = _entity_id(hass, COVER_DOMAIN, SCREEN_COVER_ID)

        await rig.report(screen, SCREEN_OPEN)
        await rig.tick(100)
        await rig.report(screen, SCREEN_CLOSED)
        await rig.tick(8.8)
        await rig.report(screen, "818b081004")  # target 65, estimate 64.1
        await rig.tick(5)
        state = hass.states.get(entity_id)
        assert state.state == "open"
        assert state.attributes["current_position"] == 75


# Lamel states: slider (raw, bits 28-35) and start/stop bit 36.
LOUVRE_CLOSED = "4101000000"
LOUVRE_RUN_FROM_CLOSED = "4101000010"  # bit set, slider still 0
LOUVRE_RUN_TO_OPEN = "410100f01f"  # bit set, slider jumped to the end
LOUVRE_OPEN = "410100f00f"
LOUVRE_AT_118 = "4101006007"


async def test_louvre_button_run_is_estimated_until_the_stop_report(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test a button run stopped half way, as captured on the Lamel.

    The slider jumps to the end a second into the run, so the louvres
    showed fully open while still opening, and after the stop until the
    motor reported where they were.
    """
    clock = [1000.0]
    with _patched_clock(clock):
        await _setup(hass, mock_config_entry)
        rig = _Rig(hass, mock_casambi)
        rig.clock = clock
        louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
        entity_id = _entity_id(hass, COVER_DOMAIN, LOUVRE_COVER_ID)

        await rig.report(louvre, LOUVRE_CLOSED)
        await rig.tick(100)
        await rig.report(louvre, LOUVRE_RUN_FROM_CLOSED)
        await rig.tick(1.3)
        await rig.report(louvre, LOUVRE_RUN_TO_OPEN)
        state = hass.states.get(entity_id)
        assert state.state == "opening"
        assert state.attributes["current_position"] == 6

        await rig.tick(3.7)
        assert hass.states.get(entity_id).attributes["current_position"] == 23

        # Stopped 10.3 s in: the end is repeated, the estimate holds.
        await rig.tick(5.3)
        await rig.report(louvre, LOUVRE_OPEN)
        state = hass.states.get(entity_id)
        assert state.state == "open"
        assert state.attributes["current_position"] == 47

        await rig.tick(2)
        await rig.report(louvre, LOUVRE_AT_118)
        assert hass.states.get(entity_id).attributes["current_position"] == 46


async def test_louvre_button_run_to_the_end(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test a button run the motor ends itself, 21.9 s for a full close."""
    clock = [1000.0]
    with _patched_clock(clock):
        await _setup(hass, mock_config_entry)
        rig = _Rig(hass, mock_casambi)
        rig.clock = clock
        louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
        entity_id = _entity_id(hass, COVER_DOMAIN, LOUVRE_COVER_ID)

        await rig.report(louvre, LOUVRE_OPEN)
        await rig.tick(100)
        await rig.report(louvre, LOUVRE_RUN_TO_OPEN)  # bit set, slider 255
        await rig.tick(1.8)
        await rig.report(louvre, LOUVRE_RUN_FROM_CLOSED)  # slider jumps to 0
        state = hass.states.get(entity_id)
        assert state.state == "closing"
        assert state.attributes["current_position"] == 92

        await rig.tick(20.1)
        await rig.report(louvre, LOUVRE_CLOSED)
        state = hass.states.get(entity_id)
        assert state.state == "closed"
        assert state.attributes["current_position"] == 0


async def test_louvre_position_from_the_app_is_estimated(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test a position sent from the app: shown moving, not there at once."""
    clock = [1000.0]
    with _patched_clock(clock):
        await _setup(hass, mock_config_entry)
        rig = _Rig(hass, mock_casambi)
        rig.clock = clock
        louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
        entity_id = _entity_id(hass, COVER_DOMAIN, LOUVRE_COVER_ID)

        await rig.report(louvre, LOUVRE_CLOSED)
        await rig.tick(100)
        await rig.report(louvre, LOUVRE_AT_118)
        state = hass.states.get(entity_id)
        assert state.state == "opening"
        assert state.attributes["current_position"] == 0

        await rig.tick(5)
        assert hass.states.get(entity_id).attributes["current_position"] == 23

        # 118 of 255 takes 10.2 s.
        await rig.tick(6)
        state = hass.states.get(entity_id)
        assert state.state == "open"
        assert state.attributes["current_position"] == 46


async def test_louvre_stop_from_ha_holds_the_estimate(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test stop during a close started from Home Assistant."""
    clock = [1000.0]
    with _patched_clock(clock):
        await _setup(hass, mock_config_entry)
        rig = _Rig(hass, mock_casambi)
        rig.clock = clock
        louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
        entity_id = _entity_id(hass, COVER_DOMAIN, LOUVRE_COVER_ID)

        await rig.report(louvre, LOUVRE_OPEN)
        await rig.tick(100)
        await rig.call(SERVICE_CLOSE_COVER, entity_id)
        await rig.tick(0.5)
        await rig.report(louvre, LOUVRE_RUN_TO_OPEN)  # the echo
        await rig.tick(4.5)
        await rig.call("stop_cover", entity_id)
        state = hass.states.get(entity_id)
        assert state.state == "open"
        assert state.attributes["current_position"] == 77

        await rig.tick(0.5)
        await rig.report(louvre, LOUVRE_OPEN)  # the stop, stale end
        assert hass.states.get(entity_id).attributes["current_position"] == 77
        await rig.tick(1.5)
        await rig.report(louvre, "4101009009")  # stopped at 153
        assert hass.states.get(entity_id).attributes["current_position"] == 60


async def test_louvre_stop_during_a_position_move(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test stop while the louvres open to a position sent from the app.

    Stop only cleared the bit of a button run, so a move to a position went
    on. Moving louvres honour a new position: stop sends the estimate 1 s
    ahead, rounded further along.
    """
    clock = [1000.0]
    with _patched_clock(clock):
        await _setup(hass, mock_config_entry)
        rig = _Rig(hass, mock_casambi)
        rig.clock = clock
        louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
        entity_id = _entity_id(hass, COVER_DOMAIN, LOUVRE_COVER_ID)

        await rig.report(louvre, LOUVRE_CLOSED)
        await rig.tick(100)
        await rig.report(louvre, LOUVRE_OPEN)  # 255 sent from the app
        await rig.tick(10)
        await rig.call("stop_cover", entity_id)

        mock_casambi.setControlValue.assert_not_awaited()
        mock_casambi.setSlider.assert_awaited_once()
        # 11 s of a 22 s travel: raw 127.5, rounded up.
        assert mock_casambi.setSlider.await_args.args[1] == 128


async def test_louvre_stop_during_a_position_close(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test stop while the louvres close to a position: rounded down."""
    clock = [1000.0]
    with _patched_clock(clock):
        await _setup(hass, mock_config_entry)
        rig = _Rig(hass, mock_casambi)
        rig.clock = clock
        louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
        entity_id = _entity_id(hass, COVER_DOMAIN, LOUVRE_COVER_ID)

        await rig.report(louvre, LOUVRE_OPEN)
        await rig.tick(100)
        await rig.report(louvre, LOUVRE_CLOSED)
        await rig.tick(5)
        assert hass.states.get(entity_id).state == "closing"
        await rig.call("stop_cover", entity_id)

        # 6 s of closing from 255: raw 185.5, rounded down.
        assert mock_casambi.setSlider.await_args.args[1] == 185


async def test_louvre_stop_sends_nothing_at_rest_or_on_arrival(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test stop at rest, and within the margin of the target."""
    clock = [1000.0]
    with _patched_clock(clock):
        await _setup(hass, mock_config_entry)
        rig = _Rig(hass, mock_casambi)
        rig.clock = clock
        louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
        entity_id = _entity_id(hass, COVER_DOMAIN, LOUVRE_COVER_ID)

        await rig.report(louvre, LOUVRE_CLOSED)
        await rig.tick(100)
        await rig.call("stop_cover", entity_id)

        # 118 is reached 10.2 s in; 9.5 s in it gets there within 1 s.
        await rig.report(louvre, LOUVRE_AT_118)
        await rig.tick(9.5)
        await rig.call("stop_cover", entity_id)

        mock_casambi.setSlider.assert_not_awaited()
        mock_casambi.setControlValue.assert_not_awaited()
