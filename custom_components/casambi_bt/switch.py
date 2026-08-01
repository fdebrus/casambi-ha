"""Automation switches for Casambi louvre covers and screens.

- Sun tracking: keeps the louvre slats perpendicular to the sun.
- Temperature control: biases the sun tracking toward more shade when the
  terrace is warmer than the setpoint and toward more sun when colder.
- Weather protection: closes the louvres while it rains and retracts
  screens when the wind exceeds a threshold, based on the readings of a
  Casambi sensor platform.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any, Final

from CasambiBt import Unit, UnitControl, UnitControlType

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory, EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity

from . import CasambiApi, CasambiConfigEntry
from .classify import UnitKind, classify_unit
from .const import (
    CONF_LOUVRE_AZIMUTH,
    CONF_TEMPERATURE_ENTITY,
    CONF_WIND_THRESHOLD,
    DEFAULT_LOUVRE_AZIMUTH,
    DEFAULT_WIND_THRESHOLD,
    entry_option,
)
from .entities import CasambiNetworkEntity, CasambiUnitEntity, TypedEntityDescription
from .sensor import PACKET_WIND
from .suntrack import LOUVRE_MAX_ANGLE, compute_louvre_angle, get_sun_position

_LOGGER = logging.getLogger(__name__)

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0

UPDATE_INTERVAL: Final = timedelta(minutes=5)

# Only move the louvres when the target differs enough (raw 0-255 units;
# 5 raw is roughly 3 degrees).
DEADBAND_RAW: Final = 5

CASA_RAW_MAX: Final = 255

# Degrees of additional tilt per degree Celsius of temperature error.
TEMP_GAIN: Final = 10.0

# The user offset and the temperature bias combined are limited to this.
OFFSET_LIMIT: Final = 45.0

PACKET_RAIN: Final = 0
RAIN_THRESHOLD: Final = 2

DEFAULT_TEMP_SETPOINT: Final = 22.0

# Re-arm the wind latch when the wind drops below this fraction of the
# threshold.
WIND_HYSTERESIS: Final = 0.8

# The sensor platform's writable enable bits, in bit-offset order
# (Sensor Platform V4: wind at 34, rain at 35, light at 36, motion at 37).
SENSOR_ENABLE_KEYS: Final = (
    "wind_sensor_enabled",
    "rain_sensor_enabled",
    "light_sensor_enabled",
    "motion_sensor_enabled",
)


def _read_bit(raw: bytes, offset: int) -> bool | None:
    """Read a single bit from the raw state bytes."""
    byte_index, bit_index = divmod(offset, 8)
    if byte_index >= len(raw):
        return None
    return bool(raw[byte_index] >> bit_index & 1)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasambiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create automation switches for louvres and the weather."""
    casa_api = config_entry.runtime_data

    entities: list[SwitchEntity] = []
    louvres: list[Unit] = []
    screens: list[Unit] = []
    sensor_platforms: list[Unit] = []

    for unit in casa_api.get_units():
        kind = classify_unit(unit)
        if kind is UnitKind.LOUVRE:
            louvres.append(unit)
        elif kind is UnitKind.SCREEN:
            screens.append(unit)
        elif kind is UnitKind.SENSOR_PLATFORM:
            sensor_platforms.append(unit)

    for unit in louvres:
        entities.append(CasambiSunTrackingSwitch(casa_api, unit, config_entry))
        entities.append(CasambiTemperatureControlSwitch(casa_api, unit))

    for unit in sensor_platforms:
        enable_controls = sorted(
            (
                c
                for c in unit.unitType.controls
                if c.type == UnitControlType.ONOFF and not c.readonly
            ),
            key=lambda c: c.offset,
        )
        entities.extend(
            CasambiSensorEnableSwitch(casa_api, unit, control, key)
            for control, key in zip(enable_controls, SENSOR_ENABLE_KEYS, strict=False)
        )

    if sensor_platforms and (louvres or screens):
        entities.append(
            CasambiWeatherProtectionSwitch(
                casa_api, config_entry, sensor_platforms, louvres, screens
            )
        )

    async_add_entities(entities)


class CasambiSunTrackingSwitch(CasambiUnitEntity, SwitchEntity, RestoreEntity):
    """Enables automatic sun tracking for a louvre unit.

    While on, the louvre angle follows the sun so that the slats block
    direct sunlight. The sun offset number shifts the result, and the
    temperature control switch adds a temperature-dependent bias.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_is_on = False

    def __init__(
        self, api: CasambiApi, unit: Unit, config_entry: CasambiConfigEntry
    ) -> None:
        """Initialize a sun tracking switch."""
        desc = TypedEntityDescription(
            key=unit.uuid, entity_type="sun-tracking", translation_key="sun_tracking"
        )
        self._obj: Unit
        super().__init__(api, desc, unit)
        self._config_entry = config_entry
        self._cancel_interval: Any = None

    async def async_added_to_hass(self) -> None:
        """Restore the previous state and resume tracking if it was on."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state == "on":
            self._attr_is_on = True
            self._start()

    async def async_will_remove_from_hass(self) -> None:
        """Stop tracking when the entity is removed."""
        self._stop()
        await super().async_will_remove_from_hass()

    def _start(self) -> None:
        if self._cancel_interval is None:
            self._cancel_interval = async_track_time_interval(
                self.hass, self._interval_update, UPDATE_INTERVAL
            )

    def _stop(self) -> None:
        if self._cancel_interval is not None:
            self._cancel_interval()
            self._cancel_interval = None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable sun tracking and apply immediately."""
        self._attr_is_on = True
        self._start()
        self.async_write_ha_state()
        await self._async_apply()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable sun tracking."""
        self._attr_is_on = False
        self._stop()
        self.async_write_ha_state()

    @callback
    def _interval_update(self, _now: Any) -> None:
        self.hass.async_create_task(self._async_apply())

    def _temperature_bias(self) -> float:
        """Return the temperature-dependent offset in degrees."""
        unit = self._obj
        if not self._api.temp_control.get(unit.uuid, False):
            return 0.0

        entity_id = entry_option(self._config_entry, CONF_TEMPERATURE_ENTITY, None)
        if not entity_id:
            return 0.0
        state = self.hass.states.get(entity_id)
        if state is None:
            return 0.0
        try:
            temperature = float(state.state)
        except ValueError:
            return 0.0

        setpoint = self._api.temp_setpoints.get(unit.uuid, DEFAULT_TEMP_SETPOINT)
        return (setpoint - temperature) * TEMP_GAIN

    async def _async_apply(self) -> None:
        """Compute the target louvre angle and move if needed."""
        if not self._attr_is_on or not self._api.available:
            return
        if self._api.rain_active:
            _LOGGER.debug("Sun tracking: raining, weather protection has priority")
            return

        unit = self._obj
        elevation, azimuth = get_sun_position(self.hass)
        louvre_azimuth = entry_option(
            self._config_entry, CONF_LOUVRE_AZIMUTH, DEFAULT_LOUVRE_AZIMUTH
        )
        offset = self._api.sun_offsets.get(unit.uuid, 0.0) + self._temperature_bias()
        offset = min(max(offset, -OFFSET_LIMIT), OFFSET_LIMIT)

        angle = compute_louvre_angle(elevation, azimuth, louvre_azimuth, offset)
        if angle is None:
            _LOGGER.debug("Sun tracking %s: no direct sun, not moving", unit.name)
            return

        target_raw = round(angle / LOUVRE_MAX_ANGLE * CASA_RAW_MAX)
        current_raw = (
            unit.state.slider
            if unit.state is not None and unit.state.slider is not None
            else None
        )
        if current_raw is not None and abs(target_raw - current_raw) < DEADBAND_RAW:
            return

        _LOGGER.debug(
            "Sun tracking %s: angle %.1f deg -> slider %i", unit.name, angle, target_raw
        )
        await self._async_casa_command(self._api.casa.setSlider(unit, target_raw))


class CasambiTemperatureControlSwitch(CasambiUnitEntity, SwitchEntity, RestoreEntity):
    """Biases the sun tracking of a louvre by temperature.

    While on (and sun tracking is on), the louvres tilt toward more shade
    when the temperature source reads above the setpoint and toward more
    sun when it reads below.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_is_on = False

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a temperature control switch."""
        desc = TypedEntityDescription(
            key=unit.uuid,
            entity_type="temperature-control",
            translation_key="temperature_control",
        )
        self._obj: Unit
        super().__init__(api, desc, unit)

    async def async_added_to_hass(self) -> None:
        """Restore the previous state."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state == "on":
            self._attr_is_on = True
            self._api.temp_control[self._obj.uuid] = True

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the temperature bias."""
        self._attr_is_on = True
        self._api.temp_control[self._obj.uuid] = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the temperature bias."""
        self._attr_is_on = False
        self._api.temp_control[self._obj.uuid] = False
        self.async_write_ha_state()


class CasambiSensorEnableSwitch(CasambiUnitEntity, SwitchEntity):
    """Enables or disables one element of a Casambi sensor platform.

    The sensor platform carries writable enable bits for its elements
    (wind, rain, light, motion). The bit gates the element's reporting
    and the network's own use of it - disabling the rain element also
    disables the pergola's built-in rain protection.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, api: CasambiApi, unit: Unit, control: UnitControl, translation_key: str
    ) -> None:
        """Initialize a sensor enable switch."""
        desc = TypedEntityDescription(
            key=unit.uuid,
            entity_type=f"enable-{control.offset}",
            translation_key=translation_key,
        )
        self._obj: Unit
        super().__init__(api, desc, unit)
        self._control = control

    @property
    def is_on(self) -> bool | None:
        """Return True if the sensor element is enabled."""
        unit = self._obj
        if unit.state is None or unit.state.raw_state is None:
            return None
        return _read_bit(unit.state.raw_state, self._control.offset)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the sensor element."""
        await self._async_casa_command(
            self._api.casa.setControlValue(self._obj, self._control, 1)
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the sensor element."""
        await self._async_casa_command(
            self._api.casa.setControlValue(self._obj, self._control, 0)
        )
        self.async_write_ha_state()


class CasambiWeatherProtectionSwitch(CasambiNetworkEntity, SwitchEntity, RestoreEntity):
    """Protects the pergola based on the sensor platform readings.

    While on: rain closes the louvres (sealed roof) and pauses sun
    tracking; wind above the configured threshold retracts the screens.
    Screens are not extended again automatically.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_is_on = False

    def __init__(
        self,
        api: CasambiApi,
        config_entry: CasambiConfigEntry,
        sensor_platforms: list[Unit],
        louvres: list[Unit],
        screens: list[Unit],
    ) -> None:
        """Initialize the weather protection switch."""
        desc = EntityDescription(
            key="weather-protection", translation_key="weather_protection"
        )
        super().__init__(api, desc)
        self._config_entry = config_entry
        self._sensor_platforms = sensor_platforms
        self._louvres = louvres
        self._screens = screens
        self._wind_latched = False

    async def async_added_to_hass(self) -> None:
        """Restore state and start listening to sensor platform updates."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state == "on":
            self._attr_is_on = True
        for unit in self._sensor_platforms:
            self._api.register_unit_updates(unit, self._sensor_update)

    async def async_will_remove_from_hass(self) -> None:
        """Stop listening to sensor platform updates."""
        for unit in self._sensor_platforms:
            self._api.unregister_unit_updates(unit, self._sensor_update)
        await super().async_will_remove_from_hass()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable weather protection and evaluate immediately."""
        self._attr_is_on = True
        self.async_write_ha_state()
        for unit in self._sensor_platforms:
            self._sensor_update(unit)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable weather protection."""
        self._attr_is_on = False
        self._api.rain_active = False
        self._wind_latched = False
        self.async_write_ha_state()

    @callback
    def _sensor_update(self, unit: Unit) -> None:
        """Evaluate new sensor platform readings."""
        if not self._attr_is_on:
            return

        cache = unit.sensor_cache

        rain_raw = cache.get(PACKET_RAIN)
        if rain_raw is not None:
            raining = rain_raw >= RAIN_THRESHOLD
            if raining and not self._api.rain_active:
                self._api.rain_active = True
                _LOGGER.info("Weather protection: rain detected, closing louvres")
                for louvre in self._louvres:
                    self.hass.async_create_task(self._async_close_louvre(louvre))
            elif not raining and self._api.rain_active:
                self._api.rain_active = False
                _LOGGER.info("Weather protection: rain stopped")

        wind_raw = cache.get(PACKET_WIND)
        if wind_raw is not None:
            wind_kmh = wind_raw / 4
            threshold = entry_option(
                self._config_entry, CONF_WIND_THRESHOLD, DEFAULT_WIND_THRESHOLD
            )
            if wind_kmh >= threshold and not self._wind_latched:
                self._wind_latched = True
                _LOGGER.info(
                    "Weather protection: wind %.1f km/h, retracting screens", wind_kmh
                )
                for screen in self._screens:
                    self.hass.async_create_task(self._async_retract_screen(screen))
            elif wind_kmh < threshold * WIND_HYSTERESIS:
                self._wind_latched = False

    async def _async_close_louvre(self, unit: Unit) -> None:
        await self._async_casa_command(self._api.casa.setSlider(unit, 0))

    async def _async_retract_screen(self, unit: Unit) -> None:
        await self._async_casa_command(self._api.casa.setLevel(unit, CASA_RAW_MAX))
