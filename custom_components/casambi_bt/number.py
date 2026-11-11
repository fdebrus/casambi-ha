"""Numbers: the dials of the louvre automation and the vertical control."""

from __future__ import annotations

from CasambiBt import Group, Unit, UnitControlType

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import DEGREE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .classify import UnitKind
from .const import (
    CASA_RAW_MAX,
    CONF_IMPORT_GROUPS,
    CONF_VERTICAL_AS_COVER,
    DEFAULT_TEMP_SETPOINT,
    entry_option,
)
from .entity import (
    CasambiEntity,
    CasambiGroupEntity,
    CasambiUnitConfigEntity,
    CasambiUnitEntity,
)
from .hub import CasambiHub
from .models import CasambiConfigEntry
from .pergola import LouvreSettings

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasambiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the number entities."""
    data = entry.runtime_data
    hub = data.hub

    entities: list[NumberEntity] = []
    for unit in hub.get_units():
        if hub.kind(unit) is UnitKind.LOUVRE:
            settings = data.pergola.settings[unit.uuid]
            entities.append(CasambiSunOffsetNumber(hub, unit, settings))
            entities.append(CasambiTempSetpointNumber(hub, unit, settings))

    # The vertical control is exposed as a cover instead when enabled.
    if not entry_option(entry, CONF_VERTICAL_AS_COVER, False):
        entities.extend(
            CasambiVerticalNumberUnit(hub, unit)
            for unit in hub.get_units([UnitControlType.VERTICAL])
        )
        if entry_option(entry, CONF_IMPORT_GROUPS, True):
            entities.extend(
                CasambiVerticalNumberGroup(hub, group)
                for group in hub.get_groups()
                if any(
                    unit.unitType.get_control(UnitControlType.VERTICAL) is not None
                    for unit in group.units
                )
            )

    async_add_entities(entities)


class CasambiLouvreSettingNumber(CasambiUnitConfigEntity, RestoreNumber):
    """A dial of the louvre automation whose value survives a restart."""

    _attr_mode = NumberMode.SLIDER
    # The field of the louvre's settings this dial sets.
    _setting: str

    def __init__(
        self, hub: CasambiHub, unit: Unit, settings: LouvreSettings, key: str
    ) -> None:
        """Initialize a dial; ``key`` names it with underscores."""
        super().__init__(hub, unit, key.replace("_", "-"))
        self._settings = settings
        self._attr_translation_key = key

    async def async_added_to_hass(self) -> None:
        """Restore the previous value."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = last.native_value
            setattr(self._settings, self._setting, last.native_value)

    async def async_set_native_value(self, value: float) -> None:
        """Set the value."""
        self._attr_native_value = value
        setattr(self._settings, self._setting, value)
        self._write_state()


class CasambiSunOffsetNumber(CasambiLouvreSettingNumber):
    """Sun preference for the sun tracking of a louvre.

    Positive values tilt the louvres toward more sun, negative values
    toward more shade.
    """

    _attr_native_min_value = -45
    _attr_native_max_value = 45
    _attr_native_step = 1
    _attr_native_unit_of_measurement = DEGREE
    _attr_native_value = 0.0
    _setting = "sun_offset"

    def __init__(self, hub: CasambiHub, unit: Unit, settings: LouvreSettings) -> None:
        """Initialize a sun offset dial."""
        super().__init__(hub, unit, settings, "sun_offset")


class CasambiTempSetpointNumber(CasambiLouvreSettingNumber):
    """Temperature setpoint for the temperature control of a louvre.

    Above the setpoint the louvres tilt toward more shade, below toward
    more sun.
    """

    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_min_value = 15
    _attr_native_max_value = 30
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_value = DEFAULT_TEMP_SETPOINT
    _setting = "temp_setpoint"

    def __init__(self, hub: CasambiHub, unit: Unit, settings: LouvreSettings) -> None:
        """Initialize a temperature setpoint dial."""
        super().__init__(hub, unit, settings, "temp_setpoint")


class CasambiVerticalNumber(CasambiEntity, NumberEntity):
    """The vertical control of a light, as its raw value."""

    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = CASA_RAW_MAX
    _target: Group | Unit

    async def async_set_native_value(self, value: float) -> None:
        """Set the vertical value."""
        await self.hub.async_command(
            self.hub.casa.setVertical, self._target, int(value)
        )


class CasambiVerticalNumberUnit(CasambiVerticalNumber, CasambiUnitEntity):
    """The vertical control of one unit."""

    _attr_translation_key = "vertical"

    def __init__(self, hub: CasambiHub, unit: Unit) -> None:
        """Initialize the vertical number of a unit."""
        super().__init__(hub, unit, "vertical")
        self._target = unit

    @property
    def native_value(self) -> float | None:
        """Return the vertical value of the unit."""
        state = self._unit.state
        if state is not None and state.vertical is not None:
            return float(state.vertical)
        return None


class CasambiVerticalNumberGroup(CasambiVerticalNumber, CasambiGroupEntity):
    """The vertical control of a group."""

    def __init__(self, hub: CasambiHub, group: Group) -> None:
        """Initialize the vertical number of a group."""
        super().__init__(hub, group)
        self._target = group

    @property
    def native_value(self) -> float | None:
        """Return the average vertical value of the group."""
        values = [
            float(unit.state.vertical)
            for unit in self._group.units
            if unit.state is not None and unit.state.vertical is not None
        ]
        if values:
            return sum(values) / len(values)
        return None
