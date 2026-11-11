"""Covers: pergola louvres, screens and lights with a vertical control."""

from __future__ import annotations

import logging
from typing import Any

from CasambiBt import Group, Unit, UnitControlType

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .classify import UnitKind
from .const import (
    CASA_RAW_MAX,
    CONF_IMPORT_GROUPS,
    CONF_VERTICAL_AS_COVER,
    DOMAIN,
    entry_option,
)
from .entity import CasambiEntity, CasambiGroupEntity, CasambiUnitEntity
from .hub import CasambiHub
from .models import CasambiConfigEntry
from .motor import Motor
from .pergola import PergolaController

_LOGGER = logging.getLogger(__name__)

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0

_POSITION_FEATURES = (
    CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasambiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the cover entities."""
    data = entry.runtime_data
    hub = data.hub

    entities: list[CoverEntity] = [
        CasambiMotorCover(hub, data.pergola, motor) for motor in data.motors.values()
    ]

    # Light units with a vertical control can optionally be shown as covers.
    if entry_option(entry, CONF_VERTICAL_AS_COVER, False):
        entities.extend(
            CasambiVerticalCoverUnit(hub, unit)
            for unit in hub.get_units([UnitControlType.VERTICAL])
        )
        if entry_option(entry, CONF_IMPORT_GROUPS, True):
            entities.extend(
                CasambiVerticalCoverGroup(hub, group)
                for group in hub.get_groups()
                if any(
                    unit.unitType.get_control(UnitControlType.VERTICAL) is not None
                    for unit in group.units
                )
            )

    async_add_entities(entities)


def _percent(raw: float) -> int:
    return round(raw * 100 / CASA_RAW_MAX)


def _raw(percent: float) -> int:
    return round(percent * CASA_RAW_MAX / 100)


class CasambiVerticalCover(CasambiEntity, CoverEntity):
    """A cover driven by the Casambi vertical control."""

    _attr_device_class = CoverDeviceClass.BLIND
    _attr_supported_features = _POSITION_FEATURES
    _target: Group | Unit

    @property
    def is_closed(self) -> bool | None:
        """Return True if the cover is closed."""
        position = self.current_cover_position
        return None if position is None else position == 0

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        await self.hub.async_command(
            self.hub.casa.setVertical, self._target, CASA_RAW_MAX
        )

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        await self.hub.async_command(self.hub.casa.setVertical, self._target, 0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a position (0-100)."""
        await self.hub.async_command(
            self.hub.casa.setVertical, self._target, _raw(kwargs[ATTR_POSITION])
        )


class CasambiVerticalCoverUnit(CasambiVerticalCover, CasambiUnitEntity):
    """The vertical control of one unit as a cover."""

    _attr_name = None

    def __init__(self, hub: CasambiHub, unit: Unit) -> None:
        """Initialize the cover of a vertical-control unit."""
        super().__init__(hub, unit, "cover")
        self._target = unit

    @property
    def current_cover_position(self) -> int | None:
        """Return the position of the cover (0-100)."""
        state = self._unit.state
        if state is not None and state.vertical is not None:
            return _percent(state.vertical)
        return None


class CasambiVerticalCoverGroup(CasambiVerticalCover, CasambiGroupEntity):
    """The vertical control of a group as a cover."""

    def __init__(self, hub: CasambiHub, group: Group) -> None:
        """Initialize the cover of a group of vertical-control units."""
        super().__init__(hub, group)
        self._target = group

    @property
    def current_cover_position(self) -> int | None:
        """Return the average position of the covers in the group (0-100)."""
        values = [
            unit.state.vertical
            for unit in self._group.units
            if unit.state is not None and unit.state.vertical is not None
        ]
        if values:
            return _percent(sum(values) / len(values))
        return None


class CasambiMotorCover(CasambiUnitEntity, CoverEntity):
    """A louvre or screen motor.

    The entity shows what the motor's model makes of the unit and passes
    the commands on; how the motors move is in ``motor_model``. What it
    adds are the locks: Home Assistant refuses to move a motor out of
    safety while the frost lock or the weather protection holds it. The
    wall button and the Casambi app are not bound by them.

    Home Assistant counts how far a cover is open. The louvres' raw
    position rises as they open; the screens' rises as they extend, i.e.
    close.
    """

    _attr_name = None

    def __init__(
        self, hub: CasambiHub, pergola: PergolaController, motor: Motor
    ) -> None:
        """Initialize the cover of a motor."""
        super().__init__(hub, motor.unit, "cover")
        self._motor = motor
        self._pergola = pergola
        self._is_louvre = motor.kind is UnitKind.LOUVRE
        # Whether a rising raw position opens the cover.
        self._raising_opens = motor.model.raw_open > motor.model.raw_closed
        self._attr_device_class = (
            CoverDeviceClass.BLIND if self._is_louvre else CoverDeviceClass.SHADE
        )
        # A screen can always be stopped, with a position if not with the
        # bit; louvres have had a stop since they have the bit.
        self._attr_supported_features = _POSITION_FEATURES
        if motor.model.has_bit or not self._is_louvre:
            self._attr_supported_features |= CoverEntityFeature.STOP

    async def async_added_to_hass(self) -> None:
        """Follow the motor and the weather lock."""
        await super().async_added_to_hass()
        self.async_on_remove(self._motor.add_listener(self._write_if_changed))
        self.async_on_remove(self._pergola.add_lock_listener(self._write_state))

    @callback
    def _handle_update(self, unit: Unit) -> None:
        """Leave the pushes to the motor, which tells once it took them in."""

    @callback
    def _handle_connection(self) -> None:
        """Leave the link to the motor, which first forgets its motion."""

    def _snapshot(self) -> object:
        """Write only when what the cover shows changed."""
        return (
            self.current_cover_position,
            self.is_opening,
            self.is_closing,
            self.available,
            tuple(self.extra_state_attributes.items()),
        )

    @property
    def current_cover_position(self) -> int | None:
        """Return how far the cover is open (0-100), estimated while moving."""
        raw = self._motor.position
        if raw is None:
            return None
        return _percent(raw) if self._raising_opens else 100 - _percent(raw)

    @property
    def is_closed(self) -> bool | None:
        """Return True if the cover is closed."""
        position = self.current_cover_position
        return None if position is None else position == 0

    @property
    def is_opening(self) -> bool:
        """Return True while the cover opens."""
        travel = self._motor.travel
        return travel is not None and travel == self._raising_opens

    @property
    def is_closing(self) -> bool:
        """Return True while the cover closes."""
        travel = self._motor.travel
        return travel is not None and travel != self._raising_opens

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Show whether, and why, the weather protection holds the motor."""
        reason = self._pergola.weather_lock(self._unit)
        return {
            "weather_lock": reason,
            "weather_lock_value": self._pergola.wind_kmh if reason == "wind" else None,
        }

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover, unless a lock holds it."""
        self._check_frost_lock()
        self._check_weather_lock(self._motor.model.raw_open)
        await self._motor.async_open()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover, unless a lock holds it."""
        self._check_weather_lock(self._motor.model.raw_closed)
        await self._motor.async_close()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover where it is."""
        await self._motor.async_stop_motion()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a position (0-100)."""
        position: int = kwargs[ATTR_POSITION]
        target = _raw(position if self._raising_opens else 100 - position)
        current = self._motor.reported
        if current is None or target > current:
            self._check_frost_lock()
        self._check_weather_lock(target)
        await self._motor.async_set_position(target)

    def _check_frost_lock(self) -> None:
        """Refuse to open louvres that may be frozen to their seals.

        Closing and stopping stay possible: they don't pull frozen slats
        open.
        """
        if not self._is_louvre:
            return
        temperature = self._pergola.frost_lock_temperature(self._unit)
        if temperature is not None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="frost_lock",
                translation_placeholders={"temperature": f"{temperature:g}"},
            )

    def _check_weather_lock(self, raw_target: int) -> None:
        """Refuse to move the motor out of safety while the weather holds it.

        Raw 0 is the safe position of both motors (louvres closed, screen
        retracted), so a target above the reported raw position is refused;
        anything towards safety, and stop, stay possible.
        """
        reason = self._pergola.weather_lock(self._unit)
        if reason is None:
            return
        current = self._motor.reported
        if raw_target == 0 or (current is not None and raw_target <= current):
            # Towards safety, which needs no known position to be safe.
            return
        wind = self._pergola.wind_kmh
        _LOGGER.info(
            "Weather protection: refused to move %s (%s%s)",
            self._unit.name,
            reason,
            f", {wind:g} km/h" if wind is not None else "",
        )
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=f"weather_lock_{reason}",
            translation_placeholders={
                "name": self._unit.name,
                "wind": f"{wind:g}" if wind is not None else "?",
            },
        )
