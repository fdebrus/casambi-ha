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
        "custom_components.casambi_bt.cover.monotonic", side_effect=lambda: clock[0]
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
