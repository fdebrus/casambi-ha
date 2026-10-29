"""Tests for the weather protection acting as a lock on the motors."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from CasambiBt.errors import BluetoothError
import pytest
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
    SERVICE_STOP_COVER,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from .conftest import (
    NETWORK_ID,
    SENSOR_PLATFORM_UUID,
    WINSOL_LOUVRE_UUID,
    WINSOL_SCREEN_UUID,
    entity_id_for as _entity_id,
    push_platform,
    setup_entry as _setup,
    unit_by_uuid,
)

PROTECTION_ID = f"{NETWORK_ID}-weather-protection"
LOUVRE_COVER_ID = f"{NETWORK_ID}-unit-{WINSOL_LOUVRE_UUID}-cover"
SCREEN_COVER_ID = f"{NETWORK_ID}-unit-{WINSOL_SCREEN_UUID}-cover"


async def _enable(hass: HomeAssistant, mock_casambi: MagicMock) -> None:
    await hass.services.async_call(
        "switch",
        "turn_on",
        {ATTR_ENTITY_ID: _entity_id(hass, "switch", PROTECTION_ID)},
        blocking=True,
    )
    await hass.async_block_till_done()
    mock_casambi.setLevel.reset_mock()
    mock_casambi.setSlider.reset_mock()


def _weather(hass: HomeAssistant, mock_casambi: MagicMock, **readings: int) -> None:
    """Push new sensor platform readings: rain 0/1, wind in km/h.

    The fixture starts dry with exactly the 35 km/h threshold.
    """
    sensor_unit = unit_by_uuid(mock_casambi, SENSOR_PLATFORM_UUID)
    handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]
    push_platform(handler, sensor_unit, **readings)


async def _call(hass: HomeAssistant, service: str, entity_id: str, **data: int) -> None:
    await hass.services.async_call(
        COVER_DOMAIN, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
    )


async def test_wind_locks_the_louvres(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that opening the louvres is refused while the wind holds them."""
    await _setup(hass, mock_config_entry)
    await _enable(hass, mock_casambi)
    louvre_id = _entity_id(hass, COVER_DOMAIN, LOUVRE_COVER_ID)
    assert mock_config_entry.runtime_data.wind_active

    attributes = hass.states.get(louvre_id).attributes
    assert attributes["weather_lock"] == "wind"
    assert attributes["weather_lock_value"] == 35.0

    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, SERVICE_OPEN_COVER, louvre_id)
    assert err.value.translation_key == "weather_lock_wind"
    assert err.value.translation_placeholders == {
        "name": "Pergola Louvres",
        "wind": "35",
    }

    # 60 % is raw 153, above the current 128.
    with pytest.raises(ServiceValidationError):
        await _call(hass, SERVICE_SET_COVER_POSITION, louvre_id, **{ATTR_POSITION: 60})
    mock_casambi.setSlider.assert_not_awaited()
    mock_casambi.setControlValue.assert_not_awaited()


async def test_rain_locks_the_louvres_only(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that rain refuses to open the louvres but leaves the screens alone."""
    await _setup(hass, mock_config_entry)
    # A calm day, so that only the rain holds anything.
    _weather(hass, mock_casambi, wind=10)
    await _enable(hass, mock_casambi)
    louvre_id = _entity_id(hass, COVER_DOMAIN, LOUVRE_COVER_ID)
    screen_id = _entity_id(hass, COVER_DOMAIN, SCREEN_COVER_ID)

    _weather(hass, mock_casambi, rain=1)
    await hass.async_block_till_done()
    mock_casambi.setSlider.assert_awaited_once()
    mock_casambi.setSlider.reset_mock()

    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, SERVICE_OPEN_COVER, louvre_id)
    assert err.value.translation_key == "weather_lock_rain"
    assert hass.states.get(louvre_id).attributes["weather_lock"] == "rain"
    assert hass.states.get(louvre_id).attributes["weather_lock_value"] is None

    # The screen may be extended in the rain.
    assert hass.states.get(screen_id).attributes["weather_lock"] is None
    await _call(hass, SERVICE_CLOSE_COVER, screen_id)
    mock_casambi.setLevel.assert_awaited_once()
    assert mock_casambi.setLevel.await_args.args[1] == 255


async def test_wind_locks_the_screens_against_extending(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a screen can be retracted but not extended in the wind."""
    await _setup(hass, mock_config_entry)
    await _enable(hass, mock_casambi)
    screen_id = _entity_id(hass, COVER_DOMAIN, SCREEN_COVER_ID)
    assert hass.states.get(screen_id).attributes["weather_lock"] == "wind"

    # Close extends the screen (dimmer 255), so does a lower position.
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, SERVICE_CLOSE_COVER, screen_id)
    assert err.value.translation_key == "weather_lock_wind"
    # 20 % open is raw 204, above the current 64.
    with pytest.raises(ServiceValidationError):
        await _call(hass, SERVICE_SET_COVER_POSITION, screen_id, **{ATTR_POSITION: 20})
    mock_casambi.setLevel.assert_not_awaited()

    # Retracting further is fine: 90 % open is raw 26, then fully open.
    await _call(hass, SERVICE_SET_COVER_POSITION, screen_id, **{ATTR_POSITION: 90})
    assert mock_casambi.setLevel.await_args.args[1] == 26
    await _call(hass, SERVICE_OPEN_COVER, screen_id)
    assert mock_casambi.setLevel.await_args.args[1] == 0
    await _call(hass, SERVICE_STOP_COVER, screen_id)


async def test_closing_the_louvres_is_allowed_in_the_wind(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that close, a lower position and stop still work under the lock."""
    await _setup(hass, mock_config_entry)
    await _enable(hass, mock_casambi)
    louvre_id = _entity_id(hass, COVER_DOMAIN, LOUVRE_COVER_ID)

    await _call(hass, SERVICE_CLOSE_COVER, louvre_id)
    assert mock_casambi.setSlider.await_args.args[1] == 0
    # 20 % is raw 51, below the current 128.
    await _call(hass, SERVICE_SET_COVER_POSITION, louvre_id, **{ATTR_POSITION: 20})
    assert mock_casambi.setSlider.await_args.args[1] == 51
    await _call(hass, SERVICE_STOP_COVER, louvre_id)
    assert mock_casambi.setSlider.await_count == 2


async def test_a_motor_moved_elsewhere_is_sent_back(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that the wall button cannot leave the pergola open in the wind.

    A louvre or screen reporting a rest position out of safety is sent
    back once it has stopped, and not twice within the interval.
    """
    clock = [1000.0]
    with (
        patch(
            "custom_components.casambi_bt.cover.monotonic",
            side_effect=lambda: clock[0],
        ),
        patch(
            "custom_components.casambi_bt.switch.monotonic",
            side_effect=lambda: clock[0],
        ),
    ):
        await _setup(hass, mock_config_entry)
        louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
        screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
        handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]

        async def report(unit: object, packet: str) -> None:
            unit.setStateFromBytes(bytes.fromhex(packet))  # type: ignore[attr-defined]
            handler(unit)
            await hass.async_block_till_done()

        # Let the screen fixture's own move from 64 finish, then enable:
        # the gusting start closes and retracts everything once.
        await report(screen, "818b080000")
        clock[0] = 1100.0
        await _enable(hass, mock_casambi)

        # The button opened the louvres to 128 (bit clear: at rest). Within
        # 30 s of the protective command nothing is sent ...
        clock[0] += 10
        await report(louvre, "4101000008")
        mock_casambi.setSlider.assert_not_awaited()
        # ... after it, they are closed again.
        clock[0] += 25
        await report(louvre, "4101000008")
        mock_casambi.setSlider.assert_awaited_once()
        assert mock_casambi.setSlider.await_args.args[1] == 0
        assert "Pergola Louvres was moved to 128 while held by wind (35 km/h)" in (
            caplog.text
        )

        # Still running on the button (bit set): left alone until it stops.
        clock[0] += 40
        await report(louvre, "4101000018")
        mock_casambi.setSlider.assert_awaited_once()
        await report(louvre, "4101000008")
        assert mock_casambi.setSlider.await_count == 2

        # The app extends the screen: it reports target 255 at once, but a
        # position behind a moving screen is ignored, so the retract waits
        # for the 35 s travel to end.
        clock[0] += 40
        await report(screen, "818b08f00f")
        mock_casambi.setLevel.assert_not_awaited()
        clock[0] += 20
        await report(screen, "818b08f00f")
        mock_casambi.setLevel.assert_not_awaited()
        clock[0] += 20
        await report(screen, "818b08f00f")
        mock_casambi.setLevel.assert_awaited_once()
        assert mock_casambi.setLevel.await_args.args[1] == 0


async def test_the_lock_is_released_by_the_hysteresis_and_the_switch(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a lull below 80 % of the threshold, or the switch, unlocks."""
    await _setup(hass, mock_config_entry)
    await _enable(hass, mock_casambi)
    api = mock_config_entry.runtime_data
    louvre_id = _entity_id(hass, COVER_DOMAIN, LOUVRE_COVER_ID)
    screen_id = _entity_id(hass, COVER_DOMAIN, SCREEN_COVER_ID)

    # 30 km/h is above the 28 km/h release point: still locked.
    _weather(hass, mock_casambi, wind=30)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError):
        await _call(hass, SERVICE_OPEN_COVER, louvre_id)

    _weather(hass, mock_casambi, wind=25)
    await hass.async_block_till_done()
    assert api.wind_active is False
    assert hass.states.get(louvre_id).attributes["weather_lock"] is None
    assert hass.states.get(screen_id).attributes["weather_lock"] is None
    await _call(hass, SERVICE_OPEN_COVER, louvre_id)
    mock_casambi.setSlider.assert_awaited_once()

    # Locked again by a gust, released at once by the switch.
    _weather(hass, mock_casambi, wind=40)
    await hass.async_block_till_done()
    assert hass.states.get(screen_id).attributes["weather_lock"] == "wind"
    await hass.services.async_call(
        "switch",
        "turn_off",
        {ATTR_ENTITY_ID: _entity_id(hass, "switch", PROTECTION_ID)},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(louvre_id).attributes["weather_lock"] is None
    assert hass.states.get(screen_id).attributes["weather_lock"] is None
    mock_casambi.setLevel.reset_mock()
    await _call(hass, SERVICE_CLOSE_COVER, screen_id)
    mock_casambi.setLevel.assert_awaited_once()


async def test_a_failed_command_is_retried_on_the_next_motor_push(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a protective command that failed is sent again later.

    With a wind entity that holds steady no new reading comes to retry it,
    and the motor pushes used to skip a unit whose command had failed.
    """
    clock = [1000.0]
    with patch(
        "custom_components.casambi_bt.switch.monotonic",
        side_effect=lambda: clock[0],
    ):
        await _setup(hass, mock_config_entry)
        louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
        handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]
        mock_casambi.setSlider.side_effect = BluetoothError
        await _enable(hass, mock_casambi)
        mock_casambi.setSlider.side_effect = None

        # Same open louvres, no new weather reading: sent again once the
        # interval has passed.
        clock[0] += 40
        louvre.setStateFromBytes(bytes.fromhex("4101000008"))
        handler(louvre)
        await hass.async_block_till_done()
        mock_casambi.setSlider.assert_awaited_once()
        assert mock_casambi.setSlider.await_args.args[1] == 0


async def test_nothing_is_sent_while_the_link_is_down(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that the wind rising during an outage waits for the link.

    A write while disconnected makes the library reconnect on its own,
    past the integration's backoff; the command is kept and sent once the
    network is back.
    """
    await _setup(hass, mock_config_entry)
    api = mock_config_entry.runtime_data
    # Calm, so that enabling sends nothing.
    _weather(hass, mock_casambi, wind=10)
    await _enable(hass, mock_casambi)

    mock_casambi.connected = False
    api._notify_connection_state()  # noqa: SLF001
    await hass.async_block_till_done()
    _weather(hass, mock_casambi, wind=40)
    await hass.async_block_till_done()
    mock_casambi.setLevel.assert_not_awaited()
    mock_casambi.setSlider.assert_not_awaited()

    mock_casambi.connected = True
    api._notify_connection_state()  # noqa: SLF001
    await hass.async_block_till_done()
    mock_casambi.setLevel.assert_awaited_once()
    mock_casambi.setSlider.assert_awaited_once()


async def test_closing_is_allowed_while_the_position_is_unknown(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a move to the safe end needs no known position."""
    louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
    louvre._state = None  # noqa: SLF001
    await _setup(hass, mock_config_entry)
    await _enable(hass, mock_casambi)
    louvre_id = _entity_id(hass, COVER_DOMAIN, LOUVRE_COVER_ID)

    await _call(hass, SERVICE_CLOSE_COVER, louvre_id)
    mock_casambi.setSlider.assert_awaited_once()
    assert mock_casambi.setSlider.await_args.args[1] == 0
    with pytest.raises(ServiceValidationError):
        await _call(hass, SERVICE_OPEN_COVER, louvre_id)


async def test_a_lock_change_during_an_outage_leaves_the_motion_alone(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that refreshing the lock does not replay stale states.

    It used to rerun every callback of the motors, which fed their state
    from before the outage to the screen's motion tracking: after the
    reconnect a screen at rest was shown closing for half a minute.
    """
    clock = [1000.0]
    with patch(
        "custom_components.casambi_bt.cover.monotonic", side_effect=lambda: clock[0]
    ):
        await _setup(hass, mock_config_entry)
        api = mock_config_entry.runtime_data
        screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
        handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]
        screen_id = _entity_id(hass, COVER_DOMAIN, SCREEN_COVER_ID)
        _weather(hass, mock_casambi, wind=10)
        await _enable(hass, mock_casambi)

        screen.setStateFromBytes(bytes.fromhex("818b080000"))
        handler(screen)
        clock[0] += 100
        mock_casambi.connected = False
        api._notify_connection_state()  # noqa: SLF001
        await hass.async_block_till_done()

        # The wind rises while the link is down.
        _weather(hass, mock_casambi, wind=40)
        await hass.async_block_till_done()

        mock_casambi.connected = True
        api._notify_connection_state()  # noqa: SLF001
        screen.setStateFromBytes(bytes.fromhex("818b08800c"))
        handler(screen)
        await hass.async_block_till_done()
        state = hass.states.get(screen_id)
        assert state.state == "open"
        assert state.attributes["weather_lock"] == "wind"


async def test_a_screen_arriving_on_its_estimate_is_sent_back(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the app extending the screen in the wind, with one push only.

    The screen reports its target once and nothing when it arrives; the
    watcher only ran on pushes, so it never sent the screen back.
    """
    clock = [1000.0]
    with (
        patch(
            "custom_components.casambi_bt.cover.monotonic",
            side_effect=lambda: clock[0],
        ),
        patch(
            "custom_components.casambi_bt.switch.monotonic",
            side_effect=lambda: clock[0],
        ),
    ):
        await _setup(hass, mock_config_entry)
        screen = unit_by_uuid(mock_casambi, WINSOL_SCREEN_UUID)
        handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]
        screen.setStateFromBytes(bytes.fromhex("818b080000"))
        handler(screen)
        clock[0] = 1100.0
        await _enable(hass, mock_casambi)

        clock[0] += 40
        screen.setStateFromBytes(bytes.fromhex("818b08f00f"))
        handler(screen)
        await hass.async_block_till_done()
        mock_casambi.setLevel.assert_not_awaited()

        clock[0] += 40
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
        await hass.async_block_till_done()
        mock_casambi.setLevel.assert_awaited_once()
        assert mock_casambi.setLevel.await_args.args[1] == 0


async def test_a_louvre_that_stays_short_of_closed_is_retried_less_often(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test re-securing a louvre an obstacle stops at slider 10.

    It used to be sent back, and logged, every 30 s for as long as the
    wind blew; the interval now doubles while the louvre gets nowhere.
    """
    clock = [1000.0]
    with patch(
        "custom_components.casambi_bt.switch.monotonic", side_effect=lambda: clock[0]
    ):
        await _setup(hass, mock_config_entry)
        louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
        handler = mock_casambi.registerUnitChangedHandler.call_args.args[0]
        await _enable(hass, mock_casambi)

        async def stuck_at_10(after: float) -> int:
            clock[0] += after
            louvre.setStateFromBytes(bytes.fromhex("410100a000"))
            handler(louvre)
            await hass.async_block_till_done()
            return mock_casambi.setSlider.await_count

        assert await stuck_at_10(31) == 1
        assert await stuck_at_10(31) == 2
        # No progress: the next try waits 60 s, not 30.
        assert await stuck_at_10(31) == 2
        assert await stuck_at_10(30) == 3


async def test_a_storm_above_63_kmh_keeps_the_lock(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test sensor platform readings of 64 km/h and more.

    Read from the library's sensor cache they wrapped at 64: a 70 km/h gust
    read as 6 km/h and released the lock in the middle of a storm.
    """
    await _setup(hass, mock_config_entry)
    await _enable(hass, mock_casambi)
    api = mock_config_entry.runtime_data
    screen_id = _entity_id(hass, COVER_DOMAIN, SCREEN_COVER_ID)

    _weather(hass, mock_casambi, wind=70)
    await hass.async_block_till_done()

    assert api.wind_active is True
    assert hass.states.get(screen_id).attributes["weather_lock_value"] == 70.0
    wind_id = _entity_id(
        hass, "sensor", f"{NETWORK_ID}-unit-{SENSOR_PLATFORM_UUID}-wind"
    )
    assert float(hass.states.get(wind_id).state) == 70.0
