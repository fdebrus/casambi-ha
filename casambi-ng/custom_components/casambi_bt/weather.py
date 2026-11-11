"""Weather protection of the pergola.

While enabled: rain closes the louvres (sealed roof); wind at or above the
configured threshold retracts the screens and closes the louvres, the
position in which the roof is rated for wind. Either holds the motors as a
lock until it is over, and pauses sun tracking. Screens are not extended
again automatically.

Each reading comes from the sensor platform and from the entity chosen in
the options; rain counts while either reports it, and the stronger of the
two wind readings is used.

The lock only binds Home Assistant. The wall button and the Casambi app
are not blocked; a motor they move out of safety is sent back once it
rests.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from time import monotonic
from typing import TYPE_CHECKING, Final

from CasambiBt import Unit

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .classify import UnitKind
from .const import (
    CASA_RAW_MAX,
    CONF_RAIN_ENTITY,
    CONF_WIND_ENTITY,
    CONF_WIND_THRESHOLD,
    DEFAULT_WIND_THRESHOLD,
    WEATHER_RESECURE_INTERVAL,
    entry_option,
)
from .motor import Motor
from .platform_packets import PACKET_RAIN, PACKET_WIND
from .readings import entity_reports_rain, entity_wind_kmh

if TYPE_CHECKING:
    from .pergola import PergolaController

_LOGGER = logging.getLogger(__name__)

# Re-arm the wind latch when the wind drops below this fraction of the
# threshold.
WIND_HYSTERESIS: Final = 0.8
# A motor that keeps resting out of safety is sent back at doubling
# intervals, up to WEATHER_RESECURE_INTERVAL * 2**RESECURE_BACKOFF_STEPS.
RESECURE_BACKOFF_STEPS: Final = 5
# Rain holds the louvres until it has been dry this long: a shower comes
# and goes, and a sensor drying in the wind reports dry between drops.
RAIN_RELEASE_DELAY: Final = timedelta(minutes=15)


class WeatherProtection:
    """Secures the motors of a network against rain and wind."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        pergola: PergolaController,
        sensor_platforms: list[Unit],
    ) -> None:
        """Initialize the weather protection; it starts disabled."""
        self.hass = hass
        self.entry = entry
        self.pergola = pergola
        self.hub = pergola.hub
        motors = list(pergola.motors.values())
        self._louvres = [m for m in motors if m.kind is UnitKind.LOUVRE]
        self._screens = [m for m in motors if m.kind is UnitKind.SCREEN]
        self._sensor_platforms = sensor_platforms
        self._rain_entity: str | None = entry_option(entry, CONF_RAIN_ENTITY, None)
        self._wind_entity: str | None = entry_option(entry, CONF_WIND_ENTITY, None)

        self.enabled = False
        self.rain_active = False
        self.wind_active = False
        # Last wind reading (km/h) acted on.
        self.wind_kmh: float | None = None

        self._platform_rain = False
        self._entity_rain = False
        self._platform_wind: float | None = None
        self._entity_wind: float | None = None
        # Motors whose protective command failed, to send again on the next
        # reading or once the network is reachable: uuid -> (motor, the
        # reading it failed on). A command is only retried on a later
        # reading, so a failure that surfaces at once is not sent twice.
        self._pending: dict[str, tuple[Motor, int]] = {}
        self._reading = 0
        # When each motor was last sent to safety, to leave one that was
        # just moved alone until it has stopped and reported its position.
        self._secured_at: dict[str, float] = {}
        # Re-securing a motor that does not get there (an obstacle, an
        # overcurrent stop) backs off: uuid -> (attempts in a row without
        # progress, raw position at the last attempt).
        self._attempts: dict[str, tuple[int, int]] = {}
        # The release of the rain lock once it has been dry long enough.
        self._cancel_rain_release: CALLBACK_TYPE | None = None
        self._unsubscribe: list[CALLBACK_TYPE] = []

    # --- Lifecycle -----------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Start listening to the weather sources and the motors."""
        self._unsubscribe = [
            self.hub.subscribe_connection(self._handle_connection),
            *(
                self.hub.subscribe_unit(unit, self._sensor_update)
                for unit in self._sensor_platforms
            ),
            *(
                motor.add_watcher(self._motor_update)
                for motor in (*self._louvres, *self._screens)
            ),
        ]
        if self._rain_entity:
            self._unsubscribe.append(
                async_track_state_change_event(
                    self.hass, [self._rain_entity], self._rain_entity_changed
                )
            )
        if self._wind_entity:
            self._unsubscribe.append(
                async_track_state_change_event(
                    self.hass, [self._wind_entity], self._wind_entity_changed
                )
            )

    @callback
    def async_stop(self) -> None:
        """Stop listening."""
        self._stop_rain_release()
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []

    @callback
    def enable(self) -> None:
        """Enable the protection and act on what the sources read now."""
        self.enabled = True
        for unit in self._sensor_platforms:
            self._sensor_update(unit)
        if self._rain_entity:
            self._rain_entity_update(self.hass.states.get(self._rain_entity))
        if self._wind_entity:
            self._wind_entity_update(self.hass.states.get(self._wind_entity))

    @callback
    def disable(self) -> None:
        """Disable the protection and release everything at once."""
        self.enabled = False
        self.rain_active = False
        self.wind_active = False
        self._platform_rain = False
        self._entity_rain = False
        self._platform_wind = None
        self._entity_wind = None
        self._pending.clear()
        self._secured_at.clear()
        self._attempts.clear()
        self._stop_rain_release()
        self.pergola.notify_lock()

    # --- Sending motors to safety --------------------------------------

    @callback
    def _motor_update(self, motor: Motor) -> None:
        """Send a louvre or screen back to safety when it was moved elsewhere.

        A motor the lock holds that rests out of its safe position (raw 0:
        louvres closed, screen retracted) is sent back, once it has stopped
        and not within WEATHER_RESECURE_INTERVAL of the last command to it.
        """
        unit = motor.unit
        reason = self.pergola.weather_lock(unit)
        # A motor whose command failed is retried here too: with a steady
        # wind entity no new reading would come to retry it.
        if reason is None or not self.hub.available or not unit.online:
            return
        raw = motor.rest_position
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
        _LOGGER.info(
            "Weather protection: %s was moved to %i while held by %s, %s it again",
            unit.name,
            raw,
            f"wind ({self.wind_kmh:g} km/h)"
            if reason == "wind" and self.wind_kmh is not None
            else reason,
            "retracting" if motor.kind is UnitKind.SCREEN else "closing",
        )
        self._secure(motor)

    @callback
    def _handle_connection(self) -> None:
        """Send the commands that failed while the network was unreachable."""
        self._reading += 1
        self._retry_pending()

    def _secure(self, motor: Motor) -> None:
        """Retract a screen or close a louvre in the background."""
        uuid = motor.unit.uuid
        self._pending.pop(uuid, None)
        self._secured_at[uuid] = monotonic()
        if not self.hub.available:
            # The library would try to reconnect inside the write, past the
            # hub's backoff; the command is sent once the link is back.
            self._pending[uuid] = (motor, self._reading)
            return
        # Owned by the entry, so an unload waits for it.
        self.entry.async_create_task(self.hass, self._async_secure(motor))

    async def _async_secure(self, motor: Motor) -> None:
        """Send one protective command; keep it pending if it fails."""
        try:
            await motor.async_secure()
        except HomeAssistantError:
            _LOGGER.warning(
                "Weather protection: could not %s %s, will try again",
                "retract" if motor.kind is UnitKind.SCREEN else "close",
                motor.unit.name,
            )
            self._pending[motor.unit.uuid] = (motor, self._reading)

    @callback
    def _retry_pending(self) -> None:
        """Send the pending commands that are still called for."""
        if not self._pending or not self.hub.available:
            return
        for motor, reading in list(self._pending.values()):
            wanted = (
                self.wind_active
                if motor.kind is UnitKind.SCREEN
                else self.rain_active or self.wind_active
            )
            if not wanted:
                del self._pending[motor.unit.uuid]
            elif reading < self._reading:
                self._secure(motor)

    # --- Wind ----------------------------------------------------------

    @callback
    def _wind_entity_changed(self, event: Event[EventStateChangedData]) -> None:
        self._wind_entity_update(event.data["new_state"])

    @callback
    def _wind_entity_update(self, state: State | None) -> None:
        """Evaluate the wind entity; keep the last reading when it can't tell."""
        if not self.enabled:
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
        previous = self.wind_kmh
        self.wind_kmh = wind_kmh
        threshold = entry_option(
            self.entry, CONF_WIND_THRESHOLD, DEFAULT_WIND_THRESHOLD
        )
        if wind_kmh >= threshold and not self.wind_active:
            self.wind_active = True
            _LOGGER.info(
                "Weather protection: wind %.1f km/h, retracting screens and "
                "closing louvres",
                wind_kmh,
            )
            for motor in (*self._screens, *self._louvres):
                self._secure(motor)
            self.pergola.notify_lock()
        elif wind_kmh < threshold * WIND_HYSTERESIS and self.wind_active:
            self.wind_active = False
            self._attempts.clear()
            _LOGGER.info("Weather protection: wind dropped to %.1f km/h", wind_kmh)
            self.pergola.notify_lock()
        elif self.wind_active and wind_kmh != previous:
            # The covers show the wind that holds them.
            self.pergola.notify_lock()

    # --- Rain ----------------------------------------------------------

    @callback
    def _rain_entity_changed(self, event: Event[EventStateChangedData]) -> None:
        self._rain_entity_update(event.data["new_state"])

    @callback
    def _rain_entity_update(self, state: State | None) -> None:
        """Evaluate the rain entity; keep the last reading when it can't tell."""
        if not self.enabled:
            return
        self._reading += 1
        raining = entity_reports_rain(state)
        if raining is not None:
            self._entity_rain = raining
            self._update_rain()
        self._retry_pending()

    @callback
    def _update_rain(self) -> None:
        """Close the louvres when rain starts; release them once it's dry.

        The lock lifts RAIN_RELEASE_DELAY after the last wet reading, so a
        pause in a shower doesn't let sun tracking open the roof into the
        next one.
        """
        raining = self._platform_rain or self._entity_rain
        if raining:
            if self._cancel_rain_release is not None:
                self._stop_rain_release()
                _LOGGER.info("Weather protection: raining again, louvres stay closed")
            if not self.rain_active:
                self.rain_active = True
                _LOGGER.info("Weather protection: rain detected, closing louvres")
                for motor in self._louvres:
                    self._secure(motor)
                self.pergola.notify_lock()
        elif self.rain_active and self._cancel_rain_release is None:
            _LOGGER.info(
                "Weather protection: rain stopped, releasing the louvres in %d "
                "minutes if it stays dry",
                RAIN_RELEASE_DELAY.total_seconds() // 60,
            )
            self._cancel_rain_release = async_call_later(
                self.hass, RAIN_RELEASE_DELAY, self._release_rain
            )

    @callback
    def _release_rain(self, _now: datetime) -> None:
        """Lift the rain lock: it has been dry for RAIN_RELEASE_DELAY."""
        self._cancel_rain_release = None
        if self._platform_rain or self._entity_rain or not self.rain_active:
            return
        self.rain_active = False
        self._attempts.clear()
        _LOGGER.info(
            "Weather protection: dry for %d minutes, louvres released",
            RAIN_RELEASE_DELAY.total_seconds() // 60,
        )
        self.pergola.notify_lock()
        # A command still pending for the rain alone is no longer wanted.
        self._retry_pending()

    def _stop_rain_release(self) -> None:
        if self._cancel_rain_release is not None:
            self._cancel_rain_release()
            self._cancel_rain_release = None

    # --- The sensor platform -------------------------------------------

    @callback
    def _sensor_update(self, unit: Unit) -> None:
        """Evaluate new sensor platform readings."""
        if not self.enabled:
            return
        self._reading += 1

        rain = self.hub.platform_reading(unit, PACKET_RAIN)
        if rain is not None:
            self._platform_rain = rain >= 1
            self._update_rain()

        wind = self.hub.platform_reading(unit, PACKET_WIND)
        if wind is not None:
            self._platform_wind = float(wind)
            self._update_wind()
        self._retry_pending()
