"""Sun tracking of one louvre.

While enabled, the louvre angle follows the sun so that the slats block
direct sunlight: recomputed every five minutes, shifted by the louvre's
sun offset and, with temperature control on, by how far the temperature
is from the setpoint. The frost lock and the weather protection come
first; tracking waits while either holds the louvre.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CASA_RAW_MAX,
    CONF_LOUVRE_AZIMUTH,
    DEFAULT_LOUVRE_AZIMUTH,
    entry_option,
)
from .motor import Motor
from .suntrack import LOUVRE_MAX_ANGLE, compute_louvre_angle, get_sun_position

if TYPE_CHECKING:
    from .pergola import PergolaController

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL: Final = timedelta(minutes=5)

# Only move the louvres when the target differs enough (raw 0-255 units;
# 5 raw is roughly 3 degrees).
DEADBAND_RAW: Final = 5

# Degrees of additional tilt per degree Celsius of temperature error.
TEMP_GAIN: Final = 10.0

# The user offset and the temperature bias combined are limited to this.
OFFSET_LIMIT: Final = 45.0


class SunTracker:
    """Keeps one louvre perpendicular to the sun while enabled."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        pergola: PergolaController,
        motor: Motor,
    ) -> None:
        """Initialize the sun tracking of a louvre."""
        self.hass = hass
        self.entry = entry
        self.pergola = pergola
        self.motor = motor
        self.enabled = False
        self._cancel_interval: CALLBACK_TYPE | None = None

    @callback
    def set_enabled(self, enabled: bool) -> None:
        """Start or stop the periodic update."""
        self.enabled = enabled
        if enabled and self._cancel_interval is None:
            self._cancel_interval = async_track_time_interval(
                self.hass, self._interval_update, UPDATE_INTERVAL
            )
        elif not enabled and self._cancel_interval is not None:
            self._cancel_interval()
            self._cancel_interval = None

    @callback
    def _interval_update(self, _now: datetime) -> None:
        # Owned by the entry, so an unload waits for it.
        self.entry.async_create_task(self.hass, self.async_apply())

    def _temperature_bias(self) -> float:
        """Return the temperature-dependent offset in degrees."""
        settings = self.pergola.settings[self.motor.unit.uuid]
        if not settings.temp_control:
            return 0.0
        # The last reading stands in while the source can't tell: dropping
        # the bias for one reading swings the slats by up to 45°.
        temperature = self.pergola.temperature_c()
        if temperature is None:
            return 0.0
        return (settings.temp_setpoint - temperature) * TEMP_GAIN

    async def async_apply(self) -> None:
        """Compute the louvre angle for the sun and move if needed."""
        unit = self.motor.unit
        if not self.enabled or not self.pergola.hub.available:
            return
        if self.pergola.rain_active or self.pergola.wind_active:
            _LOGGER.debug("Sun tracking: weather protection has priority")
            return
        if self.pergola.frost_lock_temperature(unit) is not None:
            _LOGGER.debug("Sun tracking %s: frost lock, not moving", unit.name)
            return

        elevation, azimuth = get_sun_position(self.hass)
        louvre_azimuth = entry_option(
            self.entry, CONF_LOUVRE_AZIMUTH, DEFAULT_LOUVRE_AZIMUTH
        )
        offset = self.pergola.settings[unit.uuid].sun_offset + self._temperature_bias()
        offset = min(max(offset, -OFFSET_LIMIT), OFFSET_LIMIT)

        angle = compute_louvre_angle(elevation, azimuth, louvre_azimuth, offset)
        if angle is None:
            _LOGGER.debug("Sun tracking %s: no direct sun, not moving", unit.name)
            return

        target_raw = round(angle / LOUVRE_MAX_ANGLE * CASA_RAW_MAX)
        current_raw = self.motor.reported
        if current_raw is not None and abs(target_raw - current_raw) < DEADBAND_RAW:
            return

        _LOGGER.debug(
            "Sun tracking %s: angle %.1f deg -> slider %i", unit.name, angle, target_raw
        )
        try:
            await self.motor.async_set_position(target_raw)
        except HomeAssistantError:
            # The next update tries again; a flaky link is not worth a
            # traceback every five minutes.
            _LOGGER.warning("Sun tracking: could not move %s", unit.name)
