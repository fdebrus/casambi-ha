"""Support for the vertical control of Casambi compatible lights."""

from __future__ import annotations

from abc import ABCMeta, abstractmethod

from CasambiBt import Group, Unit, UnitControlType

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import DEGREE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CasambiApi, CasambiConfigEntry
from .classify import UnitKind, classify_unit
from .const import (
    CASA_RAW_MAX,
    CONF_IMPORT_GROUPS,
    CONF_VERTICAL_AS_COVER,
    DEFAULT_TEMP_SETPOINT,
    entry_option,
)
from .entities import (
    CasambiEntity,
    CasambiNetworkGroup,
    CasambiUnitConfigEntity,
    CasambiUnitEntity,
    TypedEntityDescription,
)

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasambiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the Casambi number entities."""
    casa_api = config_entry.runtime_data

    entities: list[NumberEntity] = []
    for u in casa_api.get_units():
        if classify_unit(u) is UnitKind.LOUVRE:
            entities.append(CasambiSunOffsetNumber(casa_api, u))
            entities.append(CasambiTempSetpointNumber(casa_api, u))

    # The vertical control is exposed as a cover instead when enabled.
    if not entry_option(config_entry, CONF_VERTICAL_AS_COVER, False):
        entities.extend(
            CasambiVerticalNumberUnit(casa_api, u)
            for u in casa_api.get_units([UnitControlType.VERTICAL])
        )
        if entry_option(config_entry, CONF_IMPORT_GROUPS, True):
            entities.extend(
                CasambiVerticalNumberGroup(casa_api, g)
                for g in casa_api.get_groups()
                if any(
                    u.unitType.get_control(UnitControlType.VERTICAL) is not None
                    for u in g.units
                )
            )

    async_add_entities(entities)


class CasambiLouvreSettingNumber(CasambiUnitConfigEntity, RestoreNumber):
    """A dial of the louvre automation whose value survives a restart.

    The value is shared with the automation switches through a dict on
    the api, keyed by unit uuid, which subclasses name in ``_values``.
    """

    _attr_mode = NumberMode.SLIDER
    _obj: Unit

    @property
    @abstractmethod
    def _values(self) -> dict[str, float]:
        """Return the shared dict this dial writes to."""

    async def async_added_to_hass(self) -> None:
        """Restore the previous value."""
        await super().async_added_to_hass()
        last_number_data = await self.async_get_last_number_data()
        if last_number_data is not None and last_number_data.native_value is not None:
            self._attr_native_value = last_number_data.native_value
            self._values[self._obj.uuid] = last_number_data.native_value

    async def async_set_native_value(self, value: float) -> None:
        """Set the value."""
        self._attr_native_value = value
        self._values[self._obj.uuid] = value
        self.async_write_ha_state()


class CasambiSunOffsetNumber(CasambiLouvreSettingNumber):
    """Sun preference for the sun tracking of a louvre unit.

    Positive values tilt the louvres toward more sun, negative values
    toward more shade. Used by the sun tracking switch.
    """

    _attr_native_min_value = -45
    _attr_native_max_value = 45
    _attr_native_step = 1
    _attr_native_unit_of_measurement = DEGREE
    _attr_native_value = 0.0

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a sun offset entity."""
        desc = TypedEntityDescription(
            key=unit.uuid, entity_type="sun-offset", translation_key="sun_offset"
        )
        super().__init__(api, desc, unit)

    @property
    def _values(self) -> dict[str, float]:
        return self._api.sun_offsets


class CasambiTempSetpointNumber(CasambiLouvreSettingNumber):
    """Temperature setpoint for the temperature control of a louvre unit.

    Above the setpoint the louvres tilt toward more shade, below toward
    more sun. Used by the temperature control switch.
    """

    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_min_value = 15
    _attr_native_max_value = 30
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_value = DEFAULT_TEMP_SETPOINT

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a temperature setpoint entity."""
        desc = TypedEntityDescription(
            key=unit.uuid,
            entity_type="temp-setpoint",
            translation_key="temp_setpoint",
        )
        super().__init__(api, desc, unit)

    @property
    def _values(self) -> dict[str, float]:
        return self._api.temp_setpoints


class TypedNumberEntityDescription(TypedEntityDescription, NumberEntityDescription):
    """Describes a CasambiVerticalNumberUnit."""


class CasambiVerticalNumber(CasambiEntity, NumberEntity, metaclass=ABCMeta):
    """Defines a Casambi vertical entity base class.

    This class contains common functionality for units and groups.
    """

    def __init__(
        self,
        api: CasambiApi,
        description: TypedNumberEntityDescription,
        obj: Group | Unit,
    ) -> None:
        """Initialize a Casambi vertical entity base class."""

        self._attr_mode = NumberMode.SLIDER
        self._attr_native_min_value = 0
        self._attr_native_max_value = CASA_RAW_MAX

        self._obj: Group | Unit
        super().__init__(api, description, obj)

    async def async_set_native_value(self, value: float) -> None:
        """Set the vertical value."""
        await self._async_casa_command(
            self._api.casa.setVertical(self._obj, int(value))
        )


class CasambiVerticalNumberUnit(CasambiVerticalNumber, CasambiUnitEntity):
    """Defines a Casambi vertical entity."""

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a Casambi vertical entity."""

        desc = TypedNumberEntityDescription(
            key=unit.uuid, entity_type="vertical", translation_key="vertical"
        )

        self._obj: Unit
        super().__init__(api, desc, unit)

    @property
    def native_value(self) -> float | None:
        """Get the vertical value of the unit."""
        unit = self._obj
        if unit.state is not None and unit.state.vertical is not None:
            return float(unit.state.vertical)
        return None


class CasambiVerticalNumberGroup(CasambiVerticalNumber, CasambiNetworkGroup):
    """Defines a Casambi vertical entity group."""

    def __init__(self, api: CasambiApi, group: Group) -> None:
        """Initialize a Casambi vertical group entity."""

        desc = TypedNumberEntityDescription(
            key=str(group.groudId), name=group.name, entity_type="vertical"
        )

        self._obj: Group
        super().__init__(api, desc, group)

    @property
    def native_value(self) -> float | None:
        """Get the average vertical value of the group."""
        values = [
            float(unit.state.vertical)
            for unit in self._unit_map.values()
            if unit.state is not None and unit.state.vertical is not None
        ]
        if values:
            return sum(values) / len(values)
        return None
