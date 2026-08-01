"""Support for Casambi environment sensor platforms (e.g. Sensor Platform V4).

EXT/Elements sensor platforms broadcast their readings as rotating state
packets; the library accumulates the latest raw value per packet type in
``unit.sensor_cache``. The packet semantics were reverse-engineered by the
https://github.com/superkikim/casambi-bt-hass fork:

- packet type 1: wind speed, raw / 4
- packet type 2: solar radiation, raw / 4
- packet type 0: rain (1 = dry, 5 = raining) - exposed as binary sensor
- packet type 3: PIR presence - exposed as binary sensor

Additionally the platform reports lux (12 bit) and presence (2 bit) as
first-class controls parsed into the unit state.
"""

from __future__ import annotations

import logging
from typing import Final

from CasambiBt import Unit, UnitControlType

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import LIGHT_LUX, UnitOfSpeed
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CasambiApi, CasambiConfigEntry
from .classify import UnitKind, classify_unit
from .entities import CasambiUnitEntity, TypedEntityDescription

_LOGGER = logging.getLogger(__name__)

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0

PACKET_WIND: Final = 1
PACKET_SOLAR: Final = 2


class TypedSensorEntityDescription(TypedEntityDescription, SensorEntityDescription):
    """Describes a Casambi sensor entity."""


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasambiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create sensor entities for Casambi sensor platforms."""
    casa_api = config_entry.runtime_data

    entities: list[SensorEntity] = []
    for unit in casa_api.get_units():
        if classify_unit(unit) is not UnitKind.SENSOR_PLATFORM:
            continue
        _LOGGER.debug("Creating environment sensors for unit %s", unit.name)
        entities.append(CasambiWindSensor(casa_api, unit))
        entities.append(CasambiSolarSensor(casa_api, unit))
        if unit.unitType.get_control(UnitControlType.LUX) is not None:
            entities.append(CasambiLuxSensor(casa_api, unit))

    async_add_entities(entities)


class CasambiPacketSensor(CasambiUnitEntity, SensorEntity):
    """A multiplexed environment reading of a Casambi sensor platform."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _packet_type: int
    _divisor: float = 1.0

    def __init__(self, api: CasambiApi, unit: Unit, entity_type: str) -> None:
        """Initialize an environment sensor entity."""
        desc = TypedSensorEntityDescription(
            key=unit.uuid, entity_type=entity_type, translation_key=entity_type
        )
        self._obj: Unit
        super().__init__(api, desc, unit)

    @property
    def native_value(self) -> float | None:
        """Return the latest accumulated reading."""
        unit = self._obj
        raw = unit.sensor_cache.get(self._packet_type)
        if raw is None:
            return None
        return raw / self._divisor


class CasambiWindSensor(CasambiPacketSensor):
    """The wind speed reading of a Casambi sensor platform."""

    _attr_device_class = SensorDeviceClass.WIND_SPEED
    _attr_native_unit_of_measurement = UnitOfSpeed.KILOMETERS_PER_HOUR
    _packet_type = PACKET_WIND
    _divisor = 4.0

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a wind speed sensor entity."""
        super().__init__(api, unit, "wind")


class CasambiSolarSensor(CasambiPacketSensor):
    """The solar radiation reading of a Casambi sensor platform."""

    _attr_icon = "mdi:weather-sunny"
    _packet_type = PACKET_SOLAR
    _divisor = 4.0

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a solar radiation sensor entity."""
        super().__init__(api, unit, "solar")


class CasambiLuxSensor(CasambiUnitEntity, SensorEntity):
    """The illuminance reading of a Casambi sensor platform."""

    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = LIGHT_LUX

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize an illuminance sensor entity."""
        desc = TypedSensorEntityDescription(
            key=unit.uuid, entity_type="illuminance", translation_key="illuminance"
        )
        self._obj: Unit
        super().__init__(api, desc, unit)

    @property
    def native_value(self) -> int | None:
        """Return the current illuminance."""
        unit = self._obj
        if unit.state is not None:
            return unit.state.lux
        return None
