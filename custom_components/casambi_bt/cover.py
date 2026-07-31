"""Support for Casambi covers (e.g. pergola louvres) based on the vertical control."""

from __future__ import annotations

from abc import ABCMeta
import logging
from typing import Any, Final

from CasambiBt import Group, Unit, UnitControlType

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CasambiApi, CasambiConfigEntry
from .const import CONF_IMPORT_GROUPS, CONF_VERTICAL_AS_COVER, entry_option
from .entities import (
    CasambiEntity,
    CasambiNetworkGroup,
    CasambiUnitEntity,
    TypedEntityDescription,
)

_LOGGER = logging.getLogger(__name__)

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0

CASA_VERTICAL_MAX: Final = 255


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasambiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the Casambi cover entities."""
    if not entry_option(config_entry, CONF_VERTICAL_AS_COVER, False):
        return

    casa_api = config_entry.runtime_data

    cover_entities: list[CasambiCover] = [
        CasambiCoverUnit(casa_api, u)
        for u in casa_api.get_units([UnitControlType.VERTICAL])
    ]

    group_entities: list[CasambiCover] = []
    if entry_option(config_entry, CONF_IMPORT_GROUPS, True):
        group_entities = [
            CasambiCoverGroup(casa_api, g)
            for g in casa_api.get_groups()
            if any(
                u.unitType.get_control(UnitControlType.VERTICAL) is not None
                for u in g.units
            )
        ]

    async_add_entities(cover_entities + group_entities)


class CasambiCover(CasambiEntity, CoverEntity, metaclass=ABCMeta):
    """Defines a Casambi cover entity base class.

    The Casambi vertical control (used by e.g. Winsol pergola louvres)
    is mapped to the cover position: 0 = closed, 100 = fully open.
    """

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

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        await self._async_casa_command(
            self._api.casa.setVertical(self._obj, CASA_VERTICAL_MAX)
        )

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        await self._async_casa_command(self._api.casa.setVertical(self._obj, 0))

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position (0-100)."""
        position = kwargs[ATTR_POSITION]
        await self._async_casa_command(
            self._api.casa.setVertical(
                self._obj, round(position * CASA_VERTICAL_MAX / 100)
            )
        )


class CasambiCoverUnit(CasambiCover, CasambiUnitEntity):
    """Defines a Casambi cover entity for a single unit."""

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
            return round(unit.state.vertical * 100 / CASA_VERTICAL_MAX)
        return None


class CasambiCoverGroup(CasambiCover, CasambiNetworkGroup):
    """Defines a Casambi cover entity for a group."""

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
            return round(sum(values) / len(values) * 100 / CASA_VERTICAL_MAX)
        return None
