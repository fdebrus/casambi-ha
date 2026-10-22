"""Tests for temperature control and weather protection."""

from unittest.mock import MagicMock, patch

from CasambiBt.errors import BluetoothError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casambi_bt.const import (
    CONF_RAIN_ENTITY,
    CONF_TEMPERATURE_ENTITY,
    CONF_WIND_ENTITY,
    DOMAIN,
)
from custom_components.casambi_bt.readings import (
    entity_reports_rain,
    entity_temperature_c,
    entity_wind_kmh,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_UNIT_OF_MEASUREMENT,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .conftest import (
    SENSOR_PLATFORM_UUID,
    WINSOL_LOUVRE_UUID,
    WINSOL_SCREEN_UUID,
    entity_id_for as _entity_id,
    setup_entry as _setup,
    unit_by_uuid,
)

TEMP_SENSOR = "sensor.terrace_temperature"


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

    # Terrace is 26 C (shown in the system's unit, here 78.8 F) with a 22 C
    # setpoint: bias (22-26)*10 = -40 degrees.
    hass.states.async_set(
        TEMP_SENSOR,
        "78.8",
        {ATTR_UNIT_OF_MEASUREMENT: UnitOfTemperature.FAHRENHEIT},
    )
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
    sensor_unit = unit_by_uuid(mock_casambi, SENSOR_PLATFORM_UUID)

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
    # threshold: screens retract and louvres close immediately on enable.
    mock_casambi.setLevel.assert_awaited_once()
    screen_target, screen_raw = mock_casambi.setLevel.await_args.args
    assert screen_target.uuid == WINSOL_SCREEN_UUID
    # Retracted is raw 0: the dimmer measures how far the screen is out.
    assert screen_raw == 0
    mock_casambi.setSlider.assert_awaited_once()
    louvre_target, louvre_raw = mock_casambi.setSlider.await_args.args
    assert louvre_target.uuid == WINSOL_LOUVRE_UUID
    assert louvre_raw == 0
    assert api.wind_active is True
    mock_casambi.setLevel.reset_mock()
    mock_casambi.setSlider.reset_mock()

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


async def test_rain_pauses_sun_tracking(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that sun tracking waits while it rains and resumes after."""
    await _setup(hass, mock_config_entry)
    api = mock_config_entry.runtime_data
    sensor_unit = unit_by_uuid(mock_casambi, SENSOR_PLATFORM_UUID)
    # A calm day, so that only the rain holds the louvres.
    sensor_unit._sensor_cache[1] = 40  # noqa: SLF001

    protection_id = _entity_id(
        hass, "switch", f"{mock_casambi.networkId}-weather-protection"
    )
    tracking_id = _entity_id(
        hass,
        "switch",
        f"{mock_casambi.networkId}-unit-{WINSOL_LOUVRE_UUID}-sun-tracking",
    )
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: protection_id}, blocking=True
    )
    await hass.async_block_till_done()
    mock_casambi.setSlider.assert_not_awaited()

    sensor_unit._sensor_cache[0] = 5  # noqa: SLF001
    api._unit_changed_handler(sensor_unit)  # noqa: SLF001
    await hass.async_block_till_done()
    mock_casambi.setSlider.assert_awaited_once()
    assert mock_casambi.setSlider.await_args.args[1] == 0
    mock_casambi.setSlider.reset_mock()

    with patch(
        "custom_components.casambi_bt.switch.get_sun_position",
        return_value=(45.0, 180.0),
    ):
        await hass.services.async_call(
            "switch", "turn_on", {ATTR_ENTITY_ID: tracking_id}, blocking=True
        )
        mock_casambi.setSlider.assert_not_awaited()

        # Dry again: tracking moves the louvres at its next update.
        sensor_unit._sensor_cache[0] = 1  # noqa: SLF001
        api._unit_changed_handler(sensor_unit)  # noqa: SLF001
        await hass.async_block_till_done()
        assert api.rain_active is False
        await hass.services.async_call(
            "switch", "turn_off", {ATTR_ENTITY_ID: tracking_id}, blocking=True
        )
        await hass.services.async_call(
            "switch", "turn_on", {ATTR_ENTITY_ID: tracking_id}, blocking=True
        )
    mock_casambi.setSlider.assert_awaited_once()
    # Sun at 45 deg due south: angle 45 -> raw round(45 / 142 * 255) = 81.
    assert mock_casambi.setSlider.await_args.args[1] == 81


async def test_weather_protection_off_does_nothing(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a disabled protection switch ignores sensor updates."""
    await _setup(hass, mock_config_entry)

    api = mock_config_entry.runtime_data
    sensor_unit = unit_by_uuid(mock_casambi, SENSOR_PLATFORM_UUID)

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


RAIN_ENTITY = "binary_sensor.terrace_rain"


async def _setup_with_rain_entity(
    hass: HomeAssistant, entry: MockConfigEntry, rain_entity: str
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_RAIN_ENTITY: rain_entity}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_rain_entity_without_sensor_platform(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a rain entity drives weather protection on its own."""
    mock_casambi.units = [
        u for u in mock_casambi.units if u.uuid != SENSOR_PLATFORM_UUID
    ]
    hass.states.async_set(RAIN_ENTITY, "off")
    await _setup_with_rain_entity(hass, mock_config_entry, RAIN_ENTITY)

    api = mock_config_entry.runtime_data
    protection_id = _entity_id(
        hass, "switch", f"{mock_casambi.networkId}-weather-protection"
    )

    # Rain while protection is off is ignored.
    hass.states.async_set(RAIN_ENTITY, "on")
    await hass.async_block_till_done()
    mock_casambi.setSlider.assert_not_awaited()

    # Enabling it while raining closes the louvres at once.
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: protection_id}, blocking=True
    )
    await hass.async_block_till_done()
    mock_casambi.setSlider.assert_awaited_once()
    louvre_target, louvre_raw = mock_casambi.setSlider.await_args.args
    assert louvre_target.uuid == WINSOL_LOUVRE_UUID
    assert louvre_raw == 0
    assert api.rain_active is True
    # No wind source: the screens are never touched.
    mock_casambi.setLevel.assert_not_awaited()

    # An unavailable sensor keeps the last reading.
    hass.states.async_set(RAIN_ENTITY, "unavailable")
    await hass.async_block_till_done()
    assert api.rain_active is True

    hass.states.async_set(RAIN_ENTITY, "off")
    await hass.async_block_till_done()
    assert api.rain_active is False
    mock_casambi.setSlider.assert_awaited_once()


async def test_rain_entity_and_sensor_platform_combine(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that rain counts while either source reports it."""
    hass.states.async_set(RAIN_ENTITY, "on")
    await _setup_with_rain_entity(hass, mock_config_entry, RAIN_ENTITY)

    api = mock_config_entry.runtime_data
    sensor_unit = unit_by_uuid(mock_casambi, SENSOR_PLATFORM_UUID)
    protection_id = _entity_id(
        hass, "switch", f"{mock_casambi.networkId}-weather-protection"
    )
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: protection_id}, blocking=True
    )
    await hass.async_block_till_done()
    assert api.rain_active is True

    # The sensor platform reads dry: the rain entity still reports rain.
    sensor_unit._sensor_cache[0] = 1  # noqa: SLF001
    api._unit_changed_handler(sensor_unit)  # noqa: SLF001
    await hass.async_block_till_done()
    assert api.rain_active is True

    hass.states.async_set(RAIN_ENTITY, "off")
    await hass.async_block_till_done()
    assert api.rain_active is False


async def test_no_weather_protection_without_rain_source(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that weather protection needs a sensor platform or rain entity."""
    mock_casambi.units = [
        u for u in mock_casambi.units if u.uuid != SENSOR_PLATFORM_UUID
    ]
    await _setup(hass, mock_config_entry)

    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            "switch", DOMAIN, f"{mock_casambi.networkId}-weather-protection"
        )
        is None
    )


@pytest.mark.parametrize(
    ("entity_id", "value", "expected"),
    [
        ("binary_sensor.rain", "on", True),
        ("binary_sensor.rain", "off", False),
        ("weather.home", "rainy", True),
        ("weather.home", "pouring", True),
        ("weather.home", "cloudy", False),
        ("sensor.precipitation", "0.4", True),
        ("sensor.precipitation", "0", False),
        ("sensor.precipitation", "n/a", None),
        ("sensor.precipitation", "unknown", None),
        ("binary_sensor.rain", "unavailable", None),
    ],
)
def test_entity_reports_rain(entity_id: str, value: str, expected: bool | None) -> None:
    """Test how each kind of rain entity is read."""
    assert entity_reports_rain(State(entity_id, value)) is expected


def test_entity_reports_rain_missing() -> None:
    """Test that a missing entity can't tell."""
    assert entity_reports_rain(None) is None


WIND_ENTITY = "sensor.terrace_wind"


async def test_wind_entity_without_sensor_platform(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a wind entity retracts screens, closes louvres, pauses tracking."""
    mock_casambi.units = [
        u for u in mock_casambi.units if u.uuid != SENSOR_PLATFORM_UUID
    ]
    kmh = {ATTR_UNIT_OF_MEASUREMENT: "km/h"}
    hass.states.async_set(WIND_ENTITY, "10", kmh)
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_WIND_ENTITY: WIND_ENTITY}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    api = mock_config_entry.runtime_data
    protection_id = _entity_id(
        hass, "switch", f"{mock_casambi.networkId}-weather-protection"
    )
    tracking_id = _entity_id(
        hass,
        "switch",
        f"{mock_casambi.networkId}-unit-{WINSOL_LOUVRE_UUID}-sun-tracking",
    )
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: protection_id}, blocking=True
    )
    await hass.async_block_till_done()
    mock_casambi.setLevel.assert_not_awaited()
    mock_casambi.setSlider.assert_not_awaited()

    # A storm: screens retract and the roof closes.
    hass.states.async_set(WIND_ENTITY, "40", kmh)
    await hass.async_block_till_done()
    mock_casambi.setLevel.assert_awaited_once()
    screen_target, screen_raw = mock_casambi.setLevel.await_args.args
    assert screen_target.uuid == WINSOL_SCREEN_UUID
    assert screen_raw == 0
    mock_casambi.setSlider.assert_awaited_once()
    louvre_target, louvre_raw = mock_casambi.setSlider.await_args.args
    assert louvre_target.uuid == WINSOL_LOUVRE_UUID
    assert louvre_raw == 0
    assert api.wind_active is True
    mock_casambi.setSlider.reset_mock()

    # Sun tracking stays paused while it blows.
    with patch(
        "custom_components.casambi_bt.switch.get_sun_position",
        return_value=(45.0, 180.0),
    ):
        await hass.services.async_call(
            "switch", "turn_on", {ATTR_ENTITY_ID: tracking_id}, blocking=True
        )
    mock_casambi.setSlider.assert_not_awaited()

    # An unavailable sensor and a lull above the hysteresis keep it latched.
    hass.states.async_set(WIND_ENTITY, "unavailable")
    hass.states.async_set(WIND_ENTITY, "30", kmh)
    await hass.async_block_till_done()
    assert api.wind_active is True
    mock_casambi.setLevel.assert_awaited_once()

    # The wind drops below 80% of the threshold: tracking may resume.
    hass.states.async_set(WIND_ENTITY, "20", kmh)
    await hass.async_block_till_done()
    assert api.wind_active is False


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        ("36", "km/h", 36.0),
        ("10", "m/s", 36.0),
        ("36", None, 36.0),
        ("unavailable", "km/h", None),
        ("36", "furlongs", None),
    ],
)
def test_entity_wind_kmh(value: str, unit: str | None, expected: float | None) -> None:
    """Test that wind readings are converted to km/h."""
    attributes = {ATTR_UNIT_OF_MEASUREMENT: unit} if unit else {}
    result = entity_wind_kmh(State(WIND_ENTITY, value, attributes))
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        ("21.5", "°C", 21.5),
        ("70.7", "°F", 21.5),
        ("21.5", None, 21.5),
        ("unknown", "°C", None),
        ("21.5", "furlongs", None),
    ],
)
def test_entity_temperature_c(
    value: str, unit: str | None, expected: float | None
) -> None:
    """Test that temperature readings are converted to °C."""
    attributes = {ATTR_UNIT_OF_MEASUREMENT: unit} if unit else {}
    result = entity_temperature_c(State(TEMP_SENSOR, value, attributes))
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


async def test_frost_lock_reads_the_system_unit(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a temperature shown in °F is compared with the °C threshold."""
    hass.states.async_set(
        TEMP_SENSOR, "33.8", {ATTR_UNIT_OF_MEASUREMENT: UnitOfTemperature.FAHRENHEIT}
    )
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_TEMPERATURE_ENTITY: TEMP_SENSOR}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    api = mock_config_entry.runtime_data
    louvre = unit_by_uuid(mock_casambi, WINSOL_LOUVRE_UUID)
    # 33.8 F is 1 C, below the 2 C default.
    assert api.frost_lock_temperature(louvre) == pytest.approx(1.0)


async def test_frost_lock(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that cold weather blocks opening but not closing the louvres."""
    hass.states.async_set(TEMP_SENSOR, "0.5")
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_TEMPERATURE_ENTITY: TEMP_SENSOR}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    prefix = f"{mock_casambi.networkId}-unit-{WINSOL_LOUVRE_UUID}"
    cover_id = _entity_id(hass, "cover", f"{prefix}-cover")
    lock_id = _entity_id(hass, "switch", f"{prefix}-frost-lock")
    tracking_id = _entity_id(hass, "switch", f"{prefix}-sun-tracking")
    assert hass.states.get(lock_id).state == "on"

    # Opening, fully or part way past the current position, is refused.
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            "cover", "open_cover", {ATTR_ENTITY_ID: cover_id}, blocking=True
        )
    assert err.value.translation_key == "frost_lock"
    assert err.value.translation_placeholders == {"temperature": "0.5"}
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "cover",
            "set_cover_position",
            {ATTR_ENTITY_ID: cover_id, "position": 80},
            blocking=True,
        )
    mock_casambi.setSlider.assert_not_awaited()
    mock_casambi.setControlValue.assert_not_awaited()

    # Closing further is allowed.
    await hass.services.async_call(
        "cover",
        "set_cover_position",
        {ATTR_ENTITY_ID: cover_id, "position": 20},
        blocking=True,
    )
    mock_casambi.setSlider.assert_awaited_once()
    assert mock_casambi.setSlider.await_args.args[1] == 51
    await hass.services.async_call(
        "cover", "close_cover", {ATTR_ENTITY_ID: cover_id}, blocking=True
    )
    mock_casambi.setSlider.reset_mock()
    mock_casambi.setControlValue.reset_mock()

    # Sun tracking pauses.
    with patch(
        "custom_components.casambi_bt.switch.get_sun_position",
        return_value=(45.0, 180.0),
    ):
        await hass.services.async_call(
            "switch", "turn_on", {ATTR_ENTITY_ID: tracking_id}, blocking=True
        )
    mock_casambi.setSlider.assert_not_awaited()

    # A dropped temperature source keeps its last reading.
    hass.states.async_set(TEMP_SENSOR, "unavailable")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "cover", "open_cover", {ATTR_ENTITY_ID: cover_id}, blocking=True
        )

    # Above the threshold the louvres open again.
    hass.states.async_set(TEMP_SENSOR, "3")
    await hass.services.async_call(
        "cover",
        "set_cover_position",
        {ATTR_ENTITY_ID: cover_id, "position": 80},
        blocking=True,
    )
    mock_casambi.setSlider.assert_awaited_once()
    mock_casambi.setSlider.reset_mock()

    # Turning the lock off lets them open in the cold.
    hass.states.async_set(TEMP_SENSOR, "-4")
    await hass.services.async_call(
        "switch", "turn_off", {ATTR_ENTITY_ID: lock_id}, blocking=True
    )
    await hass.services.async_call(
        "cover",
        "set_cover_position",
        {ATTR_ENTITY_ID: cover_id, "position": 80},
        blocking=True,
    )
    mock_casambi.setSlider.assert_awaited_once()


async def test_weather_protection_retries_failed_commands(
    hass: HomeAssistant,
    mock_casambi: MagicMock,
    mock_bluetooth: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that a protective command that fails is sent again.

    With the link down during a gust the retract used to be lost until the
    wind dropped and rose again.
    """
    mock_casambi.units = [
        u for u in mock_casambi.units if u.uuid != SENSOR_PLATFORM_UUID
    ]
    kmh = {ATTR_UNIT_OF_MEASUREMENT: "km/h"}
    hass.states.async_set(WIND_ENTITY, "10", kmh)
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_WIND_ENTITY: WIND_ENTITY}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    api = mock_config_entry.runtime_data
    protection_id = _entity_id(
        hass, "switch", f"{mock_casambi.networkId}-weather-protection"
    )
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: protection_id}, blocking=True
    )

    # The gust arrives while the Bluetooth link is failing.
    mock_casambi.setLevel.side_effect = BluetoothError
    mock_casambi.setSlider.side_effect = BluetoothError
    hass.states.async_set(WIND_ENTITY, "40", kmh)
    await hass.async_block_till_done()
    mock_casambi.setLevel.assert_awaited_once()
    mock_casambi.setSlider.assert_awaited_once()
    assert api.wind_active is True

    # The next reading, still above the threshold, sends them again.
    mock_casambi.setLevel.reset_mock()
    mock_casambi.setSlider.reset_mock()
    mock_casambi.setLevel.side_effect = None
    mock_casambi.setSlider.side_effect = None
    hass.states.async_set(WIND_ENTITY, "41", kmh)
    await hass.async_block_till_done()
    mock_casambi.setLevel.assert_awaited_once()
    mock_casambi.setSlider.assert_awaited_once()

    # Once sent, a further reading does not repeat them.
    hass.states.async_set(WIND_ENTITY, "42", kmh)
    await hass.async_block_till_done()
    mock_casambi.setLevel.assert_awaited_once()

    # A failure followed by the link coming back is retried from the
    # connection update.
    mock_casambi.setLevel.reset_mock()
    mock_casambi.setSlider.reset_mock()
    mock_casambi.setLevel.side_effect = BluetoothError
    mock_casambi.setSlider.side_effect = BluetoothError
    hass.states.async_set(WIND_ENTITY, "20", kmh)
    hass.states.async_set(WIND_ENTITY, "45", kmh)
    await hass.async_block_till_done()
    mock_casambi.setLevel.assert_awaited_once()
    mock_casambi.setLevel.reset_mock()
    mock_casambi.setSlider.reset_mock()
    mock_casambi.setLevel.side_effect = None
    mock_casambi.setSlider.side_effect = None
    api._notify_connection_state()  # noqa: SLF001
    await hass.async_block_till_done()
    mock_casambi.setLevel.assert_awaited_once()
    mock_casambi.setSlider.assert_awaited_once()

    # A pending command is dropped once the wind has passed.
    mock_casambi.setLevel.reset_mock()
    mock_casambi.setLevel.side_effect = BluetoothError
    hass.states.async_set(WIND_ENTITY, "20", kmh)
    hass.states.async_set(WIND_ENTITY, "50", kmh)
    await hass.async_block_till_done()
    mock_casambi.setLevel.reset_mock()
    mock_casambi.setLevel.side_effect = None
    hass.states.async_set(WIND_ENTITY, "5", kmh)
    await hass.async_block_till_done()
    assert api.wind_active is False
    api._notify_connection_state()  # noqa: SLF001
    await hass.async_block_till_done()
    mock_casambi.setLevel.assert_not_awaited()
