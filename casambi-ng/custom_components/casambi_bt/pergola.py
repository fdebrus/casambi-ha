"""The pergola automations and the locks they put on the motors.

Each louvre has its settings (sun tracking, temperature control, frost
lock and their dials) and the network has the weather protection. They
work in this order: frost lock > weather protection > temperature control
> sun tracking. The switch and number entities only set and show what is
kept here; the covers ask here whether a motor may be moved.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from CasambiBt import Unit

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.event import async_track_state_change_event

from .classify import UnitKind
from .const import (
    CONF_FROST_LOCK_TEMPERATURE,
    CONF_RAIN_ENTITY,
    CONF_TEMPERATURE_ENTITY,
    CONF_WIND_ENTITY,
    DEFAULT_FROST_LOCK_TEMPERATURE,
    DEFAULT_TEMP_SETPOINT,
    entry_option,
)
from .hub import CasambiHub
from .motor import Motor
from .readings import entity_temperature_c
from .sun_tracking import SunTracker
from .weather import WeatherProtection


@dataclass(slots=True)
class LouvreSettings:
    """The automation settings of one louvre.

    The defaults are what holds while an entity is disabled in the
    registry: its switch or dial then never sets anything. For the frost
    lock that means off, since nothing could turn it off.
    """

    # Degrees added to the sun tracking angle; positive lets in more sun.
    sun_offset: float = 0.0
    temp_control: bool = False
    temp_setpoint: float = DEFAULT_TEMP_SETPOINT
    frost_lock: bool = False


class PergolaController:
    """Holds the automations of one network."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        hub: CasambiHub,
        motors: dict[str, Motor],
    ) -> None:
        """Initialize the automations for the motors of a network."""
        self.hass = hass
        self.entry = entry
        self.hub = hub
        self.motors = motors
        louvres = [m for m in motors.values() if m.kind is UnitKind.LOUVRE]
        self.settings = {motor.unit.uuid: LouvreSettings() for motor in louvres}
        self.trackers = {
            motor.unit.uuid: SunTracker(hass, entry, self, motor) for motor in louvres
        }

        sensor_platforms = [
            unit
            for unit in hub.get_units()
            if hub.kind(unit) is UnitKind.SENSOR_PLATFORM
        ]
        has_source = bool(sensor_platforms) or any(
            entry_option(entry, key, None)
            for key in (CONF_RAIN_ENTITY, CONF_WIND_ENTITY)
        )
        # Only with something to read and something to protect.
        self.weather: WeatherProtection | None = (
            WeatherProtection(hass, entry, self, sensor_platforms)
            if has_source and motors
            else None
        )

        # Last numeric reading of the temperature source, for the frost lock
        # and the temperature control; kept current by a state listener so
        # that an unavailable source falls back to its real last reading.
        self.last_temperature: float | None = None
        self._lock_listeners: list[Callable[[], None]] = []
        self._unsubscribe: list[CALLBACK_TYPE] = []

    @callback
    def async_start(self) -> None:
        """Start following the temperature and the weather."""
        entity_id = self._temperature_entity
        if entity_id:

            @callback
            def _changed(event: Event[EventStateChangedData]) -> None:
                reading = entity_temperature_c(event.data["new_state"])
                if reading is not None:
                    self.last_temperature = reading

            self.last_temperature = entity_temperature_c(
                self.hass.states.get(entity_id)
            )
            self._unsubscribe.append(
                async_track_state_change_event(self.hass, [entity_id], _changed)
            )
        if self.weather is not None:
            self.weather.async_start()

    @callback
    def async_stop(self) -> None:
        """Stop every automation."""
        for tracker in self.trackers.values():
            tracker.set_enabled(False)
        if self.weather is not None:
            self.weather.async_stop()
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []

    # --- Temperature and the frost lock --------------------------------

    @property
    def _temperature_entity(self) -> str | None:
        entity_id: str | None = entry_option(self.entry, CONF_TEMPERATURE_ENTITY, None)
        return entity_id

    def temperature_c(self) -> float | None:
        """Return the temperature source in °C, or its last reading.

        When the source can't tell (unavailable, not numeric) its last
        reading counts, so a dropped weather station neither unlocks the
        frost lock nor swings the temperature control.
        """
        entity_id = self._temperature_entity
        if not entity_id:
            return None
        reading = entity_temperature_c(self.hass.states.get(entity_id))
        if reading is not None:
            self.last_temperature = reading
        return self.last_temperature

    def frost_lock_temperature(self, unit: Unit) -> float | None:
        """Return the temperature that locks a louvre against frost, or None.

        The louvres are locked while their frost lock is on and the
        temperature source reads below the configured threshold.
        """
        settings = self.settings.get(unit.uuid)
        if settings is None or not settings.frost_lock:
            return None
        temperature = self.temperature_c()
        if temperature is None:
            return None
        threshold: float = entry_option(
            self.entry, CONF_FROST_LOCK_TEMPERATURE, DEFAULT_FROST_LOCK_TEMPERATURE
        )
        return temperature if temperature < threshold else None

    # --- The weather lock ----------------------------------------------

    @property
    def rain_active(self) -> bool:
        """Return True while the rain holds the louvres."""
        return self.weather is not None and self.weather.rain_active

    @property
    def wind_active(self) -> bool:
        """Return True while the wind holds the louvres and the screens."""
        return self.weather is not None and self.weather.wind_active

    @property
    def wind_kmh(self) -> float | None:
        """Return the last wind reading the weather protection acted on."""
        return None if self.weather is None else self.weather.wind_kmh

    def weather_lock(self, unit: Unit) -> str | None:
        """Return why the weather protection holds a motor, or None.

        Wind holds the louvres and the screens, rain only the louvres;
        wind wins when both apply. Turning the weather protection off
        clears both at once, so an off switch never locks anything.
        """
        kind = self.hub.kind(unit)
        if kind is UnitKind.LOUVRE:
            if self.wind_active:
                return "wind"
            if self.rain_active:
                return "rain"
        elif kind is UnitKind.SCREEN and self.wind_active:
            return "wind"
        return None

    @callback
    def add_lock_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call ``listener`` when the weather lock changes.

        The lock changes with the weather, not with a pushed state, so the
        covers that show it are told here.
        """
        self._lock_listeners.append(listener)
        return lambda: self._lock_listeners.remove(listener)

    @callback
    def notify_lock(self) -> None:
        """Tell the covers that the weather lock changed."""
        for listener in list(self._lock_listeners):
            listener()
