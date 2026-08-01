"""Binary Sensor implementation for Casambi."""

import logging
from typing import Final

from CasambiBt import Unit, UnitControlType

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CasambiApi, CasambiConfigEntry
from .classify import UnitKind, classify_unit
from .entities import CasambiNetworkEntity, CasambiUnitEntity, TypedEntityDescription

_LOGGER = logging.getLogger(__name__)

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0

PACKET_RAIN: Final = 0
PACKET_PIR: Final = 3

# Rain packets carry 1 while dry and 5 while raining.
RAIN_THRESHOLD: Final = 2


NETWORK_SENSORS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="status",
        translation_key="status",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasambiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor."""
    _LOGGER.debug("Setting up binary sensor entities. config_entry: %s", config_entry)
    api = config_entry.runtime_data
    binary_sensors: list[BinarySensorEntity] = [
        CasambiBinarySensorEntity(api, description) for description in NETWORK_SENSORS
    ]

    for unit in api.get_units():
        if classify_unit(unit) is not UnitKind.SENSOR_PLATFORM:
            continue
        binary_sensors.append(CasambiRainSensor(api, unit))
        binary_sensors.append(CasambiPirSensor(api, unit))
        if unit.unitType.get_control(UnitControlType.PRESENCE) is not None:
            binary_sensors.append(CasambiPresenceSensor(api, unit))

    async_add_entities(binary_sensors)


class CasambiBinarySensorEntity(BinarySensorEntity, CasambiNetworkEntity):
    """Defines a Casambi Binary Sensor Entity."""

    def __init__(self, api: CasambiApi, description: BinarySensorEntityDescription):
        """Initialize a Casambi Binary Sensor Entity."""
        super().__init__(api=api, description=description)

    @property
    def is_on(self) -> bool:
        """Getter for state."""
        return self._api.available

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return True


class CasambiPacketBinarySensor(CasambiUnitEntity, BinarySensorEntity):
    """A multiplexed binary reading of a Casambi sensor platform."""

    _packet_type: int
    _threshold: int = 1

    def __init__(self, api: CasambiApi, unit: Unit, entity_type: str) -> None:
        """Initialize a binary environment sensor entity."""
        desc = TypedEntityDescription(
            key=unit.uuid, entity_type=entity_type, translation_key=entity_type
        )
        self._obj: Unit
        super().__init__(api, desc, unit)

    @property
    def is_on(self) -> bool | None:
        """Return True if the reading is above the threshold."""
        unit = self._obj
        raw = unit.sensor_cache.get(self._packet_type)
        if raw is None:
            return None
        return raw >= self._threshold


class CasambiRainSensor(CasambiPacketBinarySensor):
    """The rain detection of a Casambi sensor platform."""

    _attr_device_class = BinarySensorDeviceClass.MOISTURE
    _packet_type = PACKET_RAIN
    _threshold = RAIN_THRESHOLD

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a rain sensor entity."""
        super().__init__(api, unit, "rain")


class CasambiPirSensor(CasambiPacketBinarySensor):
    """The PIR motion detection of a Casambi sensor platform."""

    _attr_device_class = BinarySensorDeviceClass.MOTION
    _packet_type = PACKET_PIR

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a PIR motion sensor entity."""
        super().__init__(api, unit, "motion")


class CasambiPresenceSensor(CasambiUnitEntity, BinarySensorEntity):
    """The presence reading of a Casambi sensor platform."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a presence sensor entity."""
        desc = TypedEntityDescription(
            key=unit.uuid, entity_type="presence", translation_key="presence"
        )
        self._obj: Unit
        super().__init__(api, desc, unit)

    @property
    def is_on(self) -> bool | None:
        """Return True if presence is detected."""
        unit = self._obj
        if unit.state is not None and unit.state.presence is not None:
            return unit.state.presence > 0
        return None
