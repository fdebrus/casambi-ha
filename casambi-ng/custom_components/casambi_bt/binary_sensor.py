"""Binary sensors: the link status and the detections of sensor platforms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from CasambiBt import Unit, UnitControlType

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .classify import UnitKind
from .entity import CasambiBroadcastUnitEntity, CasambiNetworkEntity
from .hub import CasambiHub
from .models import CasambiConfigEntry
from .platform_packets import PACKET_PIR, PACKET_RAIN

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class PacketBinarySensorDescription(BinarySensorEntityDescription):
    """A detection a sensor platform sends as one of its rotating packets."""

    packet: int
    # The detection is on from this value up.
    threshold: int = 1


PACKET_BINARY_SENSORS: Final = (
    PacketBinarySensorDescription(
        key="rain",
        translation_key="rain",
        packet=PACKET_RAIN,
        device_class=BinarySensorDeviceClass.MOISTURE,
    ),
    PacketBinarySensorDescription(
        key="motion",
        translation_key="motion",
        packet=PACKET_PIR,
        device_class=BinarySensorDeviceClass.MOTION,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasambiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the binary sensor entities."""
    hub = entry.runtime_data.hub
    entities: list[BinarySensorEntity] = [CasambiStatusSensor(hub)]

    for unit in hub.get_units():
        if hub.kind(unit) is not UnitKind.SENSOR_PLATFORM:
            continue
        entities.extend(
            CasambiPacketBinarySensor(hub, unit, description)
            for description in PACKET_BINARY_SENSORS
        )
        if unit.unitType.get_control(UnitControlType.PRESENCE) is not None:
            entities.append(CasambiPresenceSensor(hub, unit))

    async_add_entities(entities)


class CasambiStatusSensor(CasambiNetworkEntity, BinarySensorEntity):
    """Whether the network can be reached; always available itself."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "status"

    def __init__(self, hub: CasambiHub) -> None:
        """Initialize the status sensor of the network."""
        super().__init__(hub, "status")

    @property
    def is_on(self) -> bool:
        """Return True while the link is up."""
        return self.hub.available

    @property
    def available(self) -> bool:
        """Return True: the status is what tells when the link is down."""
        return True


class CasambiPacketBinarySensor(CasambiBroadcastUnitEntity, BinarySensorEntity):
    """A detection of a sensor platform sent as a rotating packet."""

    entity_description: PacketBinarySensorDescription

    def __init__(
        self, hub: CasambiHub, unit: Unit, description: PacketBinarySensorDescription
    ) -> None:
        """Initialize a packet binary sensor."""
        super().__init__(hub, unit, description.key)
        self.entity_description = description

    def _snapshot(self) -> object:
        """Write only when this reading or availability changed."""
        return (self.is_on, self.available)

    @property
    def is_on(self) -> bool | None:
        """Return True if the reading reaches the threshold."""
        value = self.hub.platform_reading(self._unit, self.entity_description.packet)
        if value is None:
            return None
        return value >= self.entity_description.threshold


class CasambiPresenceSensor(CasambiBroadcastUnitEntity, BinarySensorEntity):
    """The presence reading of a sensor platform."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_translation_key = "presence"

    def __init__(self, hub: CasambiHub, unit: Unit) -> None:
        """Initialize a presence sensor."""
        super().__init__(hub, unit, "presence")

    def _snapshot(self) -> object:
        """Write only when presence or availability changed."""
        return (self.is_on, self.available)

    @property
    def is_on(self) -> bool | None:
        """Return True if presence is detected."""
        state = self._unit.state
        if state is not None and state.presence is not None:
            return state.presence > 0
        return None
