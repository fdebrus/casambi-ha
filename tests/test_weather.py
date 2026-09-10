"""Tests for temperature control and weather protection."""

from unittest.mock import MagicMock, patch

from CasambiBt import Unit
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casambi_bt.const import CONF_TEMPERATURE_ENTITY, DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import SENSOR_PLATFORM_UUID, WINSOL_LOUVRE_UUID, WINSOL_SCREEN_UUID

TEMP_SENSOR = "sensor.terrace_temperature"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, f"missing entity for {unique_id}"
    return entity_id


def _get_unit(mock_casambi: MagicMock, uuid: str) -> Unit:
    return next(u for u in mock_casambi.units if u.uuid == uuid)


async def test_temperature_control_biases_tracking(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that the temperature bias shifts the louvre angle."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_TEMPERATURE_ENTITY: TEMP_SENSOR}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    prefix = f"{mock_casambi.networkId}-unit-{WINSOL_LOUVRE_UUID}"
    tracking_id = _entity_id(hass, "switch", f"{prefix}-sun-tracking")
    temp_control_id = _entity_id(hass, "switch", f"{prefix}-temperature-control")
    setpoint_id = _entity_id(hass, "number", f"{prefix}-temp-setpoint")

    # Terrace is 26 C with a 22 C setpoint: bias (22-26)*10 = -40 degrees.
    hass.states.async_set(TEMP_SENSOR, "26")
    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: setpoint_id, "value": 22},
        blocking=True,
    )
    await hass.services.async_call(
        "switch",
        "turn_on",
        {ATTR_ENTITY_ID: temp_control_id},
        blocking=True,
    )

    # Sun at 45 deg due south: base angle 45, bias -40 -> 5 degrees.
    with patch(
        "custom_components.casambi_bt.switch.get_sun_position",
        return_value=(45.0, 180.0),
    ):
        await hass.services.async_call(
            "switch",
            "turn_on",
            {ATTR_ENTITY_ID: tracking_id},
            blocking=True,
        )

    mock_casambi.setSlider.assert_awaited_once()
    _, raw = mock_casambi.setSlider.await_args.args
    # angle 5 deg -> raw round(5 / 142 * 255) = 9.
    assert raw == 9


async def test_weather_protection_rain_and_wind(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that rain closes the louvres and wind retracts the screens."""
    await _setup(hass, mock_config_entry)

    api = mock_config_entry.runtime_data
    sensor_unit = _get_unit(mock_casambi, SENSOR_PLATFORM_UUID)

    protection_id = _entity_id(
        hass, "switch", f"{mock_casambi.networkId}-weather-protection"
    )
    await hass.services.async_call(
        "switch",
        "turn_on",
        {ATTR_ENTITY_ID: protection_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    # Default cache is dry with 35 km/h wind (raw 140), exactly at the
    # threshold: screens retract immediately on enable.
    mock_casambi.setLevel.assert_awaited_once()
    screen_target, screen_raw = mock_casambi.setLevel.await_args.args
    assert screen_target.uuid == WINSOL_SCREEN_UUID
    assert screen_raw == 255
    mock_casambi.setLevel.reset_mock()

    # Rain starts: louvres close and sun tracking is paused.
    sensor_unit._sensor_cache[0] = 5  # noqa: SLF001
    api._unit_changed_handler(sensor_unit)  # noqa: SLF001
    await hass.async_block_till_done()

    mock_casambi.setSlider.assert_awaited_once()
    louvre_target, louvre_raw = mock_casambi.setSlider.await_args.args
    assert louvre_target.uuid == WINSOL_LOUVRE_UUID
    assert louvre_raw == 0
    assert api.rain_active is True

    # Rain stops: tracking is allowed again, nothing moves by itself.
    mock_casambi.setSlider.reset_mock()
    sensor_unit._sensor_cache[0] = 1  # noqa: SLF001
    api._unit_changed_handler(sensor_unit)  # noqa: SLF001
    await hass.async_block_till_done()

    assert api.rain_active is False
    mock_casambi.setSlider.assert_not_awaited()

    # Wind stays high: the latch prevents repeated retract commands.
    api._unit_changed_handler(sensor_unit)  # noqa: SLF001
    await hass.async_block_till_done()
    mock_casambi.setLevel.assert_not_awaited()

    # Wind drops below hysteresis, then gusts again: retract fires again.
    sensor_unit._sensor_cache[1] = 40  # noqa: SLF001  # 10 km/h
    api._unit_changed_handler(sensor_unit)  # noqa: SLF001
    sensor_unit._sensor_cache[1] = 160  # noqa: SLF001  # 40 km/h
    api._unit_changed_handler(sensor_unit)  # noqa: SLF001
    await hass.async_block_till_done()
    mock_casambi.setLevel.assert_awaited_once()


async def test_weather_protection_off_does_nothing(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a disabled protection switch ignores sensor updates."""
    await _setup(hass, mock_config_entry)

    api = mock_config_entry.runtime_data
    sensor_unit = _get_unit(mock_casambi, SENSOR_PLATFORM_UUID)

    sensor_unit._sensor_cache[0] = 5  # noqa: SLF001
    api._unit_changed_handler(sensor_unit)  # noqa: SLF001
    await hass.async_block_till_done()

    mock_casambi.setSlider.assert_not_awaited()
    mock_casambi.setLevel.assert_not_awaited()
    assert api.rain_active is False


async def test_sensor_enable_switches(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the sensor element enable switches."""
    await _setup(hass, mock_config_entry)

    prefix = f"{mock_casambi.networkId}-unit-{SENSOR_PLATFORM_UUID}"

    # Wind enable is bit 34; the fixture starts with all enables set.
    wind_id = _entity_id(hass, "switch", f"{prefix}-enable-34")
    state = hass.states.get(wind_id)
    assert state is not None
    assert state.state == "on"

    await hass.services.async_call(
        "switch",
        "turn_off",
        {ATTR_ENTITY_ID: wind_id},
        blocking=True,
    )

    mock_casambi.setControlValue.assert_awaited_once()
    _, control, value = mock_casambi.setControlValue.await_args.args
    assert control.offset == 34
    assert value == 0

    # All four enable switches exist (offsets 34-37).
    for offset in (35, 36, 37):
        assert _entity_id(hass, "switch", f"{prefix}-enable-{offset}") is not None
