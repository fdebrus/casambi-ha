"""Switches for Casambi relays and the pergola automations.

- Relay: a unit whose only control is an on/off bit (e.g. the Winsol
  On-Off driving the heating) is exposed as a plain switch.
- Sun tracking: keeps the louvre slats perpendicular to the sun.
- Temperature control: biases the sun tracking toward more shade when the
  terrace is warmer than the setpoint and toward more sun when colder.
- Weather protection: closes the louvres while it rains and retracts
  screens when the wind exceeds a threshold, based on the readings of a
  Casambi sensor platform. Rain and wind can also come from any Home
  Assistant entities chosen in the options, for pergolas without a sensor
  platform. Wind also closes the louvres.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from time import monotonic
from typing import Any, Final

from CasambiBt import Unit, UnitControl, UnitControlType

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory, EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import (
    ExtraStoredData,
    RestoredExtraData,
    RestoreEntity,
)

from . import CasambiApi, CasambiConfigEntry
from .classify import UnitKind, classify_unit
from .const import (
    CASA_RAW_MAX,
    CONF_LOUVRE_AZIMUTH,
    CONF_RAIN_ENTITY,
    CONF_WIND_ENTITY,
    CONF_WIND_THRESHOLD,
    DEFAULT_LOUVRE_AZIMUTH,
    DEFAULT_TEMP_SETPOINT,
    DEFAULT_WIND_THRESHOLD,
    WEATHER_RESECURE_INTERVAL,
    entry_option,
)
from .entities import (
    CasambiBroadcastUnitEntity,
    CasambiNetworkEntity,
    CasambiUnitConfigEntity,
    CasambiUnitEntity,
    TypedEntityDescription,
)
from .platform_packets import PACKET_RAIN, PACKET_WIND
from .readings import entity_reports_rain, entity_wind_kmh
from .suntrack import LOUVRE_MAX_ANGLE, compute_louvre_angle, get_sun_position

_LOGGER = logging.getLogger(__name__)

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0

UPDATE_INTERVAL: Final = timedelta(minutes=5)

# Only move the louvres when the target differs enough (raw 0-255 units;
# 5 raw is roughly 3 degrees).
DEADBAND_RAW: Final = 5

# Degrees of additional tilt per degree Celsius of temperature error.
TEMP_GAIN: Final = 10.0

# The user offset and the temperature bias combined are limited to this.
OFFSET_LIMIT: Final = 45.0

# Re-arm the wind latch when the wind drops below this fraction of the
# threshold.
WIND_HYSTERESIS: Final = 0.8
# A motor that keeps resting out of safety is sent back at doubling
# intervals, up to WEATHER_RESECURE_INTERVAL * 2**RESECURE_BACKOFF_STEPS.
RESECURE_BACKOFF_STEPS: Final = 5


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
    """Create the relay switches and the automation switches."""
    casa_api = config_entry.runtime_data

    entities: list[SwitchEntity] = []
    louvres: list[Unit] = []
    screens: list[Unit] = []
    sensor_platforms: list[Unit] = []

    for unit in casa_api.get_units():
        kind = classify_unit(unit)
        if kind is UnitKind.RELAY:
            entities.append(CasambiRelaySwitch(casa_api, unit))
        elif kind is UnitKind.LOUVRE:
            louvres.append(unit)
        elif kind is UnitKind.SCREEN:
            screens.append(unit)
        elif kind is UnitKind.SENSOR_PLATFORM:
            sensor_platforms.append(unit)

    for unit in louvres:
        entities.append(CasambiSunTrackingSwitch(casa_api, unit, config_entry))
        entities.append(CasambiTemperatureControlSwitch(casa_api, unit))
        entities.append(CasambiFrostLockSwitch(casa_api, unit))

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

    weather_entities = [
        entry_option(config_entry, key, None)
        for key in (CONF_RAIN_ENTITY, CONF_WIND_ENTITY)
    ]
    if (sensor_platforms or any(weather_entities)) and (louvres or screens):
        entities.append(
            CasambiWeatherProtectionSwitch(
                casa_api, config_entry, sensor_platforms, louvres, screens
            )
        )

    async_add_entities(entities)


class CasambiRestoredSwitch(SwitchEntity, RestoreEntity):
    """A switch whose own on/off setting survives restarts and reloads.

    The state Home Assistant stores at shutdown is whatever the state
    machine holds, and these switches read "unavailable" whenever the
    network does; restored from that, they would come back off after
    every restart or reload during an outage. The setting is therefore
    kept as extra data, the way RestoreNumber keeps its value.
    """

    _attr_is_on: bool | None = False

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """Keep the setting itself, whatever the entity state is."""
        return RestoredExtraData({"is_on": self._attr_is_on})

    async def _async_restore_is_on(self) -> None:
        """Take the setting back from the last run."""
        extra = await self.async_get_last_extra_data()
        if extra is not None:
            self._attr_is_on = bool(extra.as_dict().get("is_on"))
            return
        # Before the extra data existed only the state was stored.
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in (STATE_ON, STATE_OFF):
            self._attr_is_on = last_state.state == STATE_ON


class CasambiRelaySwitch(CasambiUnitEntity, SwitchEntity):
    """A Casambi unit that only switches an external load on and off.

    E.g. the Winsol On-Off relay of a SO! Climate pergola, which drives
    the heating. It carries a single writable ONOFF bit and no dimmer, so
    it is a switch rather than a light.

    The bit is written directly in both directions. The library's turnOn
    sends a SetLevel ("restore last level"), which a unit without a dimmer
    ignores: captured on the Winsol On-Off, ``SetLevel ff05`` left the
    relay off, while turning it off already wrote the ONOFF bit.
    """

    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a relay switch."""
        desc = TypedEntityDescription(key=unit.uuid, name=None, entity_type="relay")
        self._obj: Unit
        super().__init__(api, desc, unit)
        self._control = next(
            c
            for c in unit.unitType.controls
            if c.type == UnitControlType.ONOFF and not c.readonly
        )

    def _snapshot(self) -> object:
        """Write only when the relay state or availability changed."""
        return (self.is_on, self.available)

    @property
    def is_on(self) -> bool:
        """Return True if the relay is on."""
        return self._obj.is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Close the relay."""
        await self._async_casa_command(
            self._api.casa.setControlValue(self._obj, self._control, 1)
        )
        self._write_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Open the relay."""
        await self._async_casa_command(
            self._api.casa.setControlValue(self._obj, self._control, 0)
        )
        self._write_state()


class CasambiLouvreAutomationSwitch(CasambiUnitConfigEntity, CasambiRestoredSwitch):
    """A switch of the louvre automation whose setting survives a restart.

    The setting is restored when the entity is added and made to take
    effect by ``_apply``, which also runs on every change.
    """

    _obj: Unit

    async def async_added_to_hass(self) -> None:
        """Restore the previous setting and make it take effect."""
        await super().async_added_to_hass()
        await self._async_restore_is_on()
        self._apply()

    @callback
    def _apply(self) -> None:
        """Make the current setting take effect."""

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the automation on."""
        self._attr_is_on = True
        self._apply()
        self._write_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the automation off."""
        self._attr_is_on = False
        self._apply()
        self._write_state()


class CasambiSunTrackingSwitch(CasambiLouvreAutomationSwitch):
    """Enables automatic sun tracking for a louvre unit.

    While on, the louvre angle follows the sun so that the slats block
    direct sunlight. The sun offset number shifts the result, and the
    temperature control switch adds a temperature-dependent bias.
    """

    def __init__(
        self, api: CasambiApi, unit: Unit, config_entry: CasambiConfigEntry
    ) -> None:
        """Initialize a sun tracking switch."""
        desc = TypedEntityDescription(
            key=unit.uuid, entity_type="sun-tracking", translation_key="sun_tracking"
        )
        super().__init__(api, desc, unit)
        self._config_entry = config_entry
        self._cancel_interval: Any = None

    async def async_will_remove_from_hass(self) -> None:
        """Stop tracking when the entity is removed."""
        self._stop()
        await super().async_will_remove_from_hass()

    @callback
    def _apply(self) -> None:
        """Run the periodic update while on."""
        if self._attr_is_on:
            self._start()
        else:
            self._stop()

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
        await super().async_turn_on(**kwargs)
        await self._async_apply()

    @callback
    def _interval_update(self, _now: Any) -> None:
        # Owned by the entry, so an unload waits for it.
        self._config_entry.async_create_task(self.hass, self._async_apply())

    def _temperature_bias(self) -> float:
        """Return the temperature-dependent offset in degrees."""
        unit = self._obj
        if not self._api.temp_control.get(unit.uuid, False):
            return 0.0

        # The last reading stands in while the source can't tell: dropping
        # the bias for one reading swings the slats by up to 45°.
        temperature = self._api.temperature_c()
        if temperature is None:
            return 0.0

        setpoint = self._api.temp_setpoints.get(unit.uuid, DEFAULT_TEMP_SETPOINT)
        return (setpoint - temperature) * TEMP_GAIN

    async def _async_apply(self) -> None:
        """Compute the target louvre angle and move if needed."""
        if not self._attr_is_on or not self._api.available:
            return
        if self._api.rain_active or self._api.wind_active:
            _LOGGER.debug("Sun tracking: weather protection has priority")
            return
        if self._api.frost_lock_temperature(self._obj) is not None:
            _LOGGER.debug("Sun tracking %s: frost lock, not moving", self._obj.name)
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
        try:
            await self._async_casa_command(self._api.casa.setSlider(unit, target_raw))
        except HomeAssistantError:
            # The next update tries again; a flaky link is not worth a
            # traceback every five minutes.
            _LOGGER.warning("Sun tracking: could not move %s", unit.name)


class CasambiTemperatureControlSwitch(CasambiLouvreAutomationSwitch):
    """Biases the sun tracking of a louvre by temperature.

    While on (and sun tracking is on), the louvres tilt toward more shade
    when the temperature source reads above the setpoint and toward more
    sun when it reads below.
    """

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a temperature control switch."""
        desc = TypedEntityDescription(
            key=unit.uuid,
            entity_type="temperature-control",
            translation_key="temperature_control",
        )
        super().__init__(api, desc, unit)

    @callback
    def _apply(self) -> None:
        """Tell the sun tracking whether to add the temperature bias."""
        self._api.temp_control[self._obj.uuid] = bool(self._attr_is_on)


class CasambiFrostLockSwitch(CasambiLouvreAutomationSwitch):
    """Keeps a louvre from being opened when it may be frozen.

    While on and the temperature source reads below the frost lock
    threshold, Home Assistant refuses to open the louvres and sun tracking
    pauses; closing and stopping stay possible. On by default.
    """

    _attr_is_on = True

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a frost lock switch."""
        desc = TypedEntityDescription(
            key=unit.uuid, entity_type="frost-lock", translation_key="frost_lock"
        )
        super().__init__(api, desc, unit)

    @callback
    def _apply(self) -> None:
        """Tell the cover and the sun tracking whether the lock is armed."""
        self._api.frost_lock[self._obj.uuid] = bool(self._attr_is_on)


class CasambiSensorEnableSwitch(CasambiBroadcastUnitEntity, SwitchEntity):
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

    def _snapshot(self) -> object:
        """Write only when the enable bit or availability changed."""
        return (self.is_on, self.available)

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
        self._write_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the sensor element."""
        await self._async_casa_command(
            self._api.casa.setControlValue(self._obj, self._control, 0)
        )
        self._write_state()


class CasambiWeatherProtectionSwitch(CasambiNetworkEntity, CasambiRestoredSwitch):
    """Protects the pergola based on the weather readings.

    While on: rain closes the louvres (sealed roof); wind at or above the
    configured threshold retracts the screens and closes the louvres, the
    position in which the roof is rated for wind. Either one pauses sun
    tracking until it is over. Screens are not extended again
    automatically. Each reading comes from the sensor platform and from
    the entity chosen in the options; rain counts while either reports it,
    and the stronger of the two wind readings is used.
    """

    _attr_entity_category = EntityCategory.CONFIG

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
        self._rain_entity: str | None = entry_option(
            config_entry, CONF_RAIN_ENTITY, None
        )
        self._platform_rain = False
        self._entity_rain = False
        self._wind_entity: str | None = entry_option(
            config_entry, CONF_WIND_ENTITY, None
        )
        self._platform_wind: float | None = None
        self._entity_wind: float | None = None
        # Units whose protective command failed, to send again on the next
        # reading or once the network is reachable: uuid -> (unit, retract,
        # the reading it failed on). A command is only retried on a later
        # reading, so a failure that surfaces at once is not sent twice.
        self._pending: dict[str, tuple[Unit, bool, int]] = {}
        self._reading = 0
        # When each motor was last sent to safety, to leave one that was
        # just moved alone until it has stopped and reported its position.
        self._secured_at: dict[str, float] = {}
        # Re-securing a motor that does not get there (an obstacle, an
        # overcurrent stop) backs off: uuid -> (attempts in a row without
        # progress, raw position at the last attempt).
        self._attempts: dict[str, tuple[int, int]] = {}

    async def async_added_to_hass(self) -> None:
        """Restore the setting and start listening to the weather sources."""
        await super().async_added_to_hass()
        await self._async_restore_is_on()
        for unit in self._sensor_platforms:
            self._api.register_unit_updates(unit, self._sensor_update)
        for unit in (*self._louvres, *self._screens):
            # After the covers, which tell whether the motor still moves.
            self._api.register_unit_updates(unit, self._motor_update, late=True)
        if self._rain_entity:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._rain_entity], self._rain_entity_changed
                )
            )
            self._rain_entity_update(self.hass.states.get(self._rain_entity))
        if self._wind_entity:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._wind_entity], self._wind_entity_changed
                )
            )
            self._wind_entity_update(self.hass.states.get(self._wind_entity))

    async def async_will_remove_from_hass(self) -> None:
        """Stop listening to sensor platform and motor updates."""
        for unit in self._sensor_platforms:
            self._api.unregister_unit_updates(unit, self._sensor_update)
        for unit in (*self._louvres, *self._screens):
            self._api.unregister_unit_updates(unit, self._motor_update, late=True)
        await super().async_will_remove_from_hass()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable weather protection and evaluate immediately."""
        self._attr_is_on = True
        self.async_write_ha_state()
        for unit in self._sensor_platforms:
            self._sensor_update(unit)
        if self._rain_entity:
            self._rain_entity_update(self.hass.states.get(self._rain_entity))
        if self._wind_entity:
            self._wind_entity_update(self.hass.states.get(self._wind_entity))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable weather protection."""
        self._attr_is_on = False
        self._api.rain_active = False
        self._api.wind_active = False
        self._platform_rain = False
        self._entity_rain = False
        self._platform_wind = None
        self._entity_wind = None
        self._pending.clear()
        self._secured_at.clear()
        self._attempts.clear()
        self.async_write_ha_state()
        self._api.notify_weather_lock()

    @callback
    def _motor_update(self, unit: Unit) -> None:
        """Send a louvre or screen back to safety when it was moved elsewhere.

        The wall button and the Casambi app bypass Home Assistant's lock.
        A motor the lock holds that rests out of its safe position (raw 0:
        louvres closed, screen retracted) is sent back, once it has stopped
        and not within WEATHER_RESECURE_INTERVAL of the last command to it.
        A screen ignores a position behind it while it moves, so waiting
        for the rest is what makes the command take.
        """
        reason = self._api.weather_lock(unit)
        # A unit whose command failed is retried here too: with a steady
        # wind entity no new reading would come to retry it.
        if reason is None or not self._api.available or not unit.online:
            return
        raw = self._api.unit_rest_position(unit)
        if raw is None:
            # Still moving.
            return
        if raw == 0:
            self._attempts.pop(unit.uuid, None)
            return
        attempts, last_raw = self._attempts.get(unit.uuid, (0, CASA_RAW_MAX + 1))
        interval = WEATHER_RESECURE_INTERVAL * 2 ** min(
            attempts, RESECURE_BACKOFF_STEPS
        )
        last = self._secured_at.get(unit.uuid)
        if last is not None and monotonic() - last < interval:
            return
        # Count the attempts that got the motor nowhere.
        attempts = attempts + 1 if raw >= last_raw else 0
        self._attempts[unit.uuid] = (attempts, raw)
        if attempts == RESECURE_BACKOFF_STEPS:
            _LOGGER.warning(
                "Weather protection: %s does not reach its safe position "
                "(stays at %i); trying again every %d minutes",
                unit.name,
                raw,
                WEATHER_RESECURE_INTERVAL * 2**RESECURE_BACKOFF_STEPS // 60,
            )
        retract = self._api.unit_kind(unit) is UnitKind.SCREEN
        _LOGGER.info(
            "Weather protection: %s was moved to %i while held by %s, %s it again",
            unit.name,
            raw,
            f"wind ({self._api.wind_kmh:g} km/h)"
            if reason == "wind" and self._api.wind_kmh is not None
            else reason,
            "retracting" if retract else "closing",
        )
        self._secure(unit, retract)

    @callback
    def _connection_callback(self) -> None:
        """Send the commands that failed while the network was unreachable."""
        super()._connection_callback()
        self._reading += 1
        self._retry_pending()

    def _secure(self, unit: Unit, retract: bool) -> None:
        """Retract a screen or close a louvre in the background."""
        self._pending.pop(unit.uuid, None)
        self._secured_at[unit.uuid] = monotonic()
        if not self._api.available:
            # The library would try to reconnect inside the write, past the
            # integration's backoff; the connection callback sends it later.
            self._pending[unit.uuid] = (unit, retract, self._reading)
            return
        # Owned by the entry, so an unload waits for it.
        self._config_entry.async_create_task(
            self.hass, self._async_secure(unit, retract)
        )

    async def _async_secure(self, unit: Unit, retract: bool) -> None:
        """Send one protective command; keep it pending if it fails.

        Retracted is raw 0 on the Winsol SO! (the dimmer counts how far the
        screen is extended) and closed is slider 0 on the louvres.
        """
        try:
            if retract:
                await self._async_casa_command(self._api.casa.setLevel(unit, 0))
            else:
                await self._async_casa_command(self._api.casa.setSlider(unit, 0))
        except HomeAssistantError:
            _LOGGER.warning(
                "Weather protection: could not %s %s, will try again",
                "retract" if retract else "close",
                unit.name,
            )
            self._pending[unit.uuid] = (unit, retract, self._reading)

    @callback
    def _retry_pending(self) -> None:
        """Send the pending commands that are still called for."""
        if not self._pending or not self._api.available:
            return
        for unit, retract, reading in list(self._pending.values()):
            wanted = (
                self._api.wind_active
                if retract
                else self._api.rain_active or self._api.wind_active
            )
            if not wanted:
                del self._pending[unit.uuid]
            elif reading < self._reading:
                self._secure(unit, retract)

    @callback
    def _wind_entity_changed(self, event: Event[EventStateChangedData]) -> None:
        self._wind_entity_update(event.data["new_state"])

    @callback
    def _wind_entity_update(self, state: State | None) -> None:
        """Evaluate the wind entity; keep the last reading when it can't tell."""
        if not self._attr_is_on:
            return
        self._reading += 1
        wind_kmh = entity_wind_kmh(state)
        if wind_kmh is not None:
            self._entity_wind = wind_kmh
            self._update_wind()
        # After the reading, so that one showing the weather has passed
        # cancels a pending command instead of sending it.
        self._retry_pending()

    @callback
    def _update_wind(self) -> None:
        """Secure the pergola when the wind reaches the threshold.

        Latched with hysteresis, so a gusty wind around the threshold does
        not send repeated commands.
        """
        readings = [
            w for w in (self._platform_wind, self._entity_wind) if w is not None
        ]
        if not readings:
            return
        wind_kmh = max(readings)
        previous = self._api.wind_kmh
        self._api.wind_kmh = wind_kmh
        threshold = entry_option(
            self._config_entry, CONF_WIND_THRESHOLD, DEFAULT_WIND_THRESHOLD
        )
        if wind_kmh >= threshold and not self._api.wind_active:
            self._api.wind_active = True
            _LOGGER.info(
                "Weather protection: wind %.1f km/h, retracting screens and "
                "closing louvres",
                wind_kmh,
            )
            for screen in self._screens:
                self._secure(screen, retract=True)
            for louvre in self._louvres:
                self._secure(louvre, retract=False)
            self._api.notify_weather_lock()
        elif wind_kmh < threshold * WIND_HYSTERESIS and self._api.wind_active:
            self._api.wind_active = False
            self._attempts.clear()
            _LOGGER.info("Weather protection: wind dropped to %.1f km/h", wind_kmh)
            self._api.notify_weather_lock()
        elif self._api.wind_active and wind_kmh != previous:
            # The covers show the wind that holds them.
            self._api.notify_weather_lock()

    @callback
    def _rain_entity_changed(self, event: Event[EventStateChangedData]) -> None:
        self._rain_entity_update(event.data["new_state"])

    @callback
    def _rain_entity_update(self, state: State | None) -> None:
        """Evaluate the rain entity; keep the last reading when it can't tell."""
        if not self._attr_is_on:
            return
        self._reading += 1
        raining = entity_reports_rain(state)
        if raining is not None:
            self._entity_rain = raining
            self._update_rain()
        self._retry_pending()

    @callback
    def _update_rain(self) -> None:
        """Close the louvres when rain starts and release them when it stops."""
        raining = self._platform_rain or self._entity_rain
        if raining and not self._api.rain_active:
            self._api.rain_active = True
            _LOGGER.info("Weather protection: rain detected, closing louvres")
            for louvre in self._louvres:
                self._secure(louvre, retract=False)
            self._api.notify_weather_lock()
        elif not raining and self._api.rain_active:
            self._api.rain_active = False
            self._attempts.clear()
            _LOGGER.info("Weather protection: rain stopped")
            self._api.notify_weather_lock()

    @callback
    def _sensor_update(self, unit: Unit) -> None:
        """Evaluate new sensor platform readings."""
        if not self._attr_is_on:
            return
        self._reading += 1

        rain = self._api.platform_reading(unit, PACKET_RAIN)
        if rain is not None:
            self._platform_rain = rain >= 1
            self._update_rain()

        wind = self._api.platform_reading(unit, PACKET_WIND)
        if wind is not None:
            self._platform_wind = float(wind)
            self._update_wind()
        self._retry_pending()
