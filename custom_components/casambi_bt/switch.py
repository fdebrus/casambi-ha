"""Sun tracking switches for Casambi louvre covers."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any, Final

from CasambiBt import Unit

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity

from . import CasambiApi, CasambiConfigEntry
from .classify import UnitKind, classify_unit
from .const import CONF_LOUVRE_AZIMUTH, DEFAULT_LOUVRE_AZIMUTH, entry_option
from .entities import CasambiUnitEntity, TypedEntityDescription
from .suntrack import LOUVRE_MAX_ANGLE, compute_louvre_angle, get_sun_position

_LOGGER = logging.getLogger(__name__)

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0

UPDATE_INTERVAL: Final = timedelta(minutes=5)

# Only move the louvres when the target differs enough (raw 0-255 units;
# 5 raw is roughly 3 degrees).
DEADBAND_RAW: Final = 5

CASA_RAW_MAX: Final = 255


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasambiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create sun tracking switches for louvre units."""
    casa_api = config_entry.runtime_data

    async_add_entities(
        CasambiSunTrackingSwitch(casa_api, unit, config_entry)
        for unit in casa_api.get_units()
        if classify_unit(unit) is UnitKind.LOUVRE
    )


class CasambiSunTrackingSwitch(CasambiUnitEntity, SwitchEntity, RestoreEntity):
    """Enables automatic sun tracking for a louvre unit.

    While on, the louvre angle follows the sun so that the slats block
    direct sunlight; the sun offset number shifts the result toward more
    sun or more shade.
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

    async def _async_apply(self) -> None:
        """Compute the target louvre angle and move if needed."""
        if not self._attr_is_on or not self._api.available:
            return

        unit = self._obj
        elevation, azimuth = get_sun_position(self.hass)
        louvre_azimuth = entry_option(
            self._config_entry, CONF_LOUVRE_AZIMUTH, DEFAULT_LOUVRE_AZIMUTH
        )
        offset = self._api.sun_offsets.get(unit.uuid, 0.0)

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
