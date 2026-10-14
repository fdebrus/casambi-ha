"""Support for Casambi covers (pergola louvres, screens, vertical controls)."""

from __future__ import annotations

from abc import ABCMeta
import logging
from time import monotonic
from typing import Any, Final

from CasambiBt import Group, Unit, UnitControlType

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CasambiApi, CasambiConfigEntry
from .classify import UnitKind, classify_unit
from .const import CONF_IMPORT_GROUPS, CONF_VERTICAL_AS_COVER, entry_option
from .entities import (
    CasambiEntity,
    CasambiNetworkGroup,
    CasambiUnitEntity,
    TypedEntityDescription,
)
from .sensor import decode_louvre_channel

_LOGGER = logging.getLogger(__name__)

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0

CASA_RAW_MAX: Final = 255

# Winsol SO! screens report their full-range travel time in milliseconds on
# sensor channels 1 and 2 (35000 on the hardware this was captured on, the
# same both ways, so which channel is which direction is an assumption).
TRAVEL_CHANNEL_EXTEND: Final = 1
TRAVEL_CHANNEL_RETRACT: Final = 2
DEFAULT_TRAVEL_MS: Final = 35000

# Time between sending a command and the motor acting on it.
STOP_LATENCY_SECONDS: Final = 0.3


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasambiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the Casambi cover entities."""
    casa_api = config_entry.runtime_data

    entities: list[CoverEntity] = []

    # Motor-driven units (e.g. Winsol pergola louvres and SO! screens) are
    # always covers - they are never lights.
    for unit in casa_api.get_units():
        kind = classify_unit(unit)
        if kind is UnitKind.LOUVRE:
            entities.append(CasambiLouvreCover(casa_api, unit))
        elif kind is UnitKind.SCREEN:
            entities.append(CasambiScreenCover(casa_api, unit))

    # Light units with a vertical control can optionally be shown as covers.
    if entry_option(config_entry, CONF_VERTICAL_AS_COVER, False):
        entities.extend(
            CasambiCoverUnit(casa_api, u)
            for u in casa_api.get_units([UnitControlType.VERTICAL])
        )
        if entry_option(config_entry, CONF_IMPORT_GROUPS, True):
            entities.extend(
                CasambiCoverGroup(casa_api, g)
                for g in casa_api.get_groups()
                if any(
                    u.unitType.get_control(UnitControlType.VERTICAL) is not None
                    for u in g.units
                )
            )

    async_add_entities(entities)


class CasambiCover(CasambiEntity, CoverEntity, metaclass=ABCMeta):
    """Defines a Casambi cover entity base class."""

    def __init__(
        self,
        api: CasambiApi,
        description: TypedEntityDescription,
        obj: Group | Unit,
    ) -> None:
        """Initialize a Casambi cover entity base class."""

        self._attr_device_class = CoverDeviceClass.BLIND
        self._attr_supported_features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.SET_POSITION
        )

        self._obj: Group | Unit
        super().__init__(api, description, obj)

    @property
    def is_closed(self) -> bool | None:
        """Return True if the cover is closed."""
        position = self.current_cover_position
        if position is None:
            return None
        return position == 0


class CasambiVerticalCover(CasambiCover, metaclass=ABCMeta):
    """Base class for covers driven by the Casambi vertical control."""

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        await self._async_casa_command(
            self._api.casa.setVertical(self._obj, CASA_RAW_MAX)
        )

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        await self._async_casa_command(self._api.casa.setVertical(self._obj, 0))

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position (0-100)."""
        position = kwargs[ATTR_POSITION]
        await self._async_casa_command(
            self._api.casa.setVertical(self._obj, round(position * CASA_RAW_MAX / 100))
        )


class CasambiCoverUnit(CasambiVerticalCover, CasambiUnitEntity):
    """Defines a Casambi cover entity for a single vertical-control unit."""

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a Casambi cover entity."""

        desc = TypedEntityDescription(key=unit.uuid, name=None, entity_type="cover")

        self._obj: Unit
        super().__init__(api, desc, unit)

    @property
    def current_cover_position(self) -> int | None:
        """Return the current position of the cover (0-100)."""
        unit = self._obj
        if unit.state is not None and unit.state.vertical is not None:
            return round(unit.state.vertical * 100 / CASA_RAW_MAX)
        return None


class CasambiCoverGroup(CasambiVerticalCover, CasambiNetworkGroup):
    """Defines a Casambi cover entity for a group of vertical-control units."""

    def __init__(self, api: CasambiApi, group: Group) -> None:
        """Initialize a Casambi cover group entity."""

        desc = TypedEntityDescription(
            key=str(group.groudId), name=group.name, entity_type="cover"
        )

        self._obj: Group
        super().__init__(api, desc, group)

    @property
    def current_cover_position(self) -> int | None:
        """Return the average position of the covers in the group (0-100)."""
        values = [
            unit.state.vertical
            for unit in self._unit_map.values()
            if unit.state is not None and unit.state.vertical is not None
        ]
        if values:
            return round(sum(values) / len(values) * 100 / CASA_RAW_MAX)
        return None


class CasambiMotorCover(CasambiCover, CasambiUnitEntity, metaclass=ABCMeta):
    """Base class for motor-driven covers with an optional start/stop toggle.

    Both the Winsol louvre (slider $pos + onoff $startstop) and the Winsol
    SO! screen (dimmer position + onoff $toggle) carry a writable ONOFF
    control that starts or stops the motion.
    """

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a motor cover entity."""
        desc = TypedEntityDescription(key=unit.uuid, name=None, entity_type="cover")

        self._obj: Unit
        super().__init__(api, desc, unit)

        self._toggle_control = next(
            (
                c
                for c in unit.unitType.controls
                if c.type == UnitControlType.ONOFF and not c.readonly
            ),
            None,
        )
        if self._toggle_control is not None:
            self._attr_supported_features = (
                self._attr_supported_features or CoverEntityFeature(0)
            ) | CoverEntityFeature.STOP

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover by triggering the start/stop toggle."""
        if self._toggle_control is None:
            return
        await self._async_casa_command(
            self._api.casa.setControlValue(self._obj, self._toggle_control, 1)
        )


class CasambiLouvreCover(CasambiMotorCover):
    """Cover for louvre motors whose position is a Casambi slider.

    E.g. the Winsol Lamel: the slider is the louvre angle (0-142 degrees on
    the wire, normalized to 0-255 by the library).
    """

    @property
    def current_cover_position(self) -> int | None:
        """Return the louvre position (0-100)."""
        unit = self._obj
        if unit.state is not None and unit.state.slider is not None:
            return round(unit.state.slider * 100 / CASA_RAW_MAX)
        return None

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the louvres fully."""
        await self._async_casa_command(
            self._api.casa.setSlider(self._obj, CASA_RAW_MAX)
        )

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the louvres."""
        await self._async_casa_command(self._api.casa.setSlider(self._obj, 0))

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the louvres to a specific position (0-100)."""
        position = kwargs[ATTR_POSITION]
        await self._async_casa_command(
            self._api.casa.setSlider(self._obj, round(position * CASA_RAW_MAX / 100))
        )


class CasambiScreenCover(CasambiMotorCover):
    """Cover for screen motors whose position is a Casambi dimmer.

    E.g. the Winsol SO! screen: the fixture's dimmer control is labeled
    "Screen Position".

    The dimmer runs the opposite way round to Home Assistant: Casambi
    reports 0 for a fully retracted (open) screen and 255 for a fully
    extended (closed) one, where Home Assistant uses 100 for open and 0
    for closed. Confirmed on Winsol SO! V4.1 hardware, where open and
    close were swapped before this inversion.

    The screen reports the position it is heading for, not where it is,
    and its onoff control (``$toggle``) reverses the motion rather than
    stopping it. Captured on SO! V4.1 hardware: close sent, the screen
    reports target 255; ``$toggle`` written while extending, the screen
    reports target 0 and retracts. Stopping therefore sends the position
    the screen has reached, estimated from when the motion started and
    from the full travel time the motor reports on its sensor channels.
    """

    _attr_device_class = CoverDeviceClass.SHADE

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a screen cover entity."""
        super().__init__(api, unit)
        self._attr_device_class = CoverDeviceClass.SHADE
        self._attr_supported_features = (
            self._attr_supported_features or CoverEntityFeature(0)
        ) | CoverEntityFeature.STOP
        self._travel_ms: dict[int, int] = {}
        self._target: int | None = None
        self._move_from = 0.0
        self._move_start: float | None = None
        self._track_motion(unit)

    def _travel_seconds(self, extending: bool) -> float:
        """Return the full-range travel time in the given direction."""
        channel = TRAVEL_CHANNEL_EXTEND if extending else TRAVEL_CHANNEL_RETRACT
        travel_ms = self._travel_ms.get(channel) or next(
            iter(self._travel_ms.values()), DEFAULT_TRAVEL_MS
        )
        return max(travel_ms, 1) / 1000

    def _estimate(self, now: float) -> float:
        """Return the estimated raw position (0-255) at ``now``."""
        if self._target is None:
            return 0.0
        if self._move_start is None:
            return float(self._target)
        distance = self._target - self._move_from
        speed = CASA_RAW_MAX / self._travel_seconds(distance > 0)
        travelled = (now - self._move_start) * speed
        if travelled >= abs(distance):
            return float(self._target)
        return self._move_from + (travelled if distance > 0 else -travelled)

    def _track_motion(self, unit: Unit) -> None:
        """Follow the reported target and the travel times of a state update."""
        state = unit.state
        if state is None:
            return
        decoded = decode_louvre_channel(state.raw_state)
        if decoded is not None and decoded[0] in (
            TRAVEL_CHANNEL_EXTEND,
            TRAVEL_CHANNEL_RETRACT,
        ):
            self._travel_ms[decoded[0]] = decoded[1]
        target = state.dimmer
        if target is None or target == self._target:
            return
        now = monotonic()
        if self._target is not None:
            # A new target: the screen starts moving from where it is now.
            self._move_from = self._estimate(now)
            self._move_start = now
        self._target = target

    @callback
    def _change_callback(self, unit: Unit) -> None:
        self._track_motion(unit)
        super()._change_callback(unit)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the screen where it is by sending the reached position."""
        if self._target is None or self._move_start is None:
            return
        now = monotonic()
        if self._estimate(now) == self._target:
            self._move_start = None
            return
        # Aim slightly ahead to cover the time the command takes to arrive.
        position = round(self._estimate(now + STOP_LATENCY_SECONDS))
        position = min(max(position, 0), CASA_RAW_MAX)
        await self._async_casa_command(self._api.casa.setLevel(self._obj, position))

    @property
    def current_cover_position(self) -> int | None:
        """Return the screen position (0-100, inverted from the dimmer)."""
        unit = self._obj
        if unit.state is not None and unit.state.dimmer is not None:
            return 100 - round(unit.state.dimmer * 100 / CASA_RAW_MAX)
        return None

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open (retract) the screen."""
        await self._async_casa_command(self._api.casa.setLevel(self._obj, 0))

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close (extend) the screen."""
        await self._async_casa_command(self._api.casa.setLevel(self._obj, CASA_RAW_MAX))

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the screen to a specific position (0-100)."""
        position = kwargs[ATTR_POSITION]
        await self._async_casa_command(
            self._api.casa.setLevel(
                self._obj, round((100 - position) * CASA_RAW_MAX / 100)
            )
        )
