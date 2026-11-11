"""Sensors: the readings of sensor platforms and of louvre motors.

A sensor platform (e.g. Sensor Platform V4) broadcasts wind speed and
solar radiation as rotating packets (see ``platform_packets``) and lux as
a control of its own. A Winsol louvre motor multiplexes four sensor
channels into its state (see ``louvre_channels``).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Final

from CasambiBt import Unit, UnitControlType

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    LIGHT_LUX,
    EntityCategory,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .classify import UnitKind
from .entity import CasambiBroadcastUnitEntity, CasambiUnitEntity
from .hub import CasambiHub
from .louvre_channels import decode_channel, louvre_temperature
from .models import CasambiConfigEntry
from .platform_packets import PACKET_SOLAR, PACKET_WIND

_LOGGER = logging.getLogger(__name__)

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0

# The channels of a louvre motor, from the fixture definition (sensor tag =
# channel - 1). Confirmed on a Winsol SO! Climate pergola: the temperature
# followed the outdoor temperature (14 °C against 12.1 °C at night), Travel
# Distance rose with every louvre movement (20 -> 31 in an evening),
# #Overcurrent stayed 0 and Module State read 50.
LOUVRE_CHANNELS: Final[dict[int, SensorEntityDescription]] = {
    1: SensorEntityDescription(
        key="louvre_temperature",
        translation_key="louvre_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    2: SensorEntityDescription(
        key="louvre_overcurrent",
        translation_key="louvre_overcurrent",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    3: SensorEntityDescription(
        key="louvre_travel_distance",
        translation_key="louvre_travel_distance",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    4: SensorEntityDescription(
        key="louvre_module_state",
        translation_key="louvre_module_state",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
}

# A channel the fixture doesn't name stays a raw diagnostic value.
UNKNOWN_LOUVRE_CHANNEL: Final = SensorEntityDescription(
    key="louvre_channel",
    translation_key="louvre_channel",
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
)


@dataclass(frozen=True, kw_only=True)
class PacketSensorDescription(SensorEntityDescription):
    """A reading a sensor platform sends as one of its rotating packets."""

    packet: int


PACKET_SENSORS: Final = (
    PacketSensorDescription(
        key="wind",
        translation_key="wind",
        packet=PACKET_WIND,
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    PacketSensorDescription(
        key="solar",
        translation_key="solar",
        packet=PACKET_SOLAR,
        state_class=SensorStateClass.MEASUREMENT,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasambiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the sensor entities."""
    hub = entry.runtime_data.hub

    entities: list[SensorEntity] = []
    for unit in hub.get_units():
        kind = hub.kind(unit)
        if kind is UnitKind.SENSOR_PLATFORM:
            _LOGGER.debug("Creating environment sensors for unit %s", unit.name)
            entities.extend(
                CasambiPacketSensor(hub, unit, description)
                for description in PACKET_SENSORS
            )
            if unit.unitType.get_control(UnitControlType.LUX) is not None:
                entities.append(CasambiLuxSensor(hub, unit))
        elif kind is UnitKind.LOUVRE:
            # One channel per SENSOR control the motor declares; the
            # channel headers on the wire count from 1.
            channels = sum(
                1
                for control in unit.unitType.controls
                if control.type == UnitControlType.SENSOR
            )
            entities.extend(
                CasambiLouvreChannelSensor(hub, unit, channel)
                for channel in range(1, channels + 1)
            )

    async_add_entities(entities)


class CasambiLouvreChannelSensor(CasambiUnitEntity, SensorEntity):
    """One multiplexed sensor channel of a louvre motor.

    The motor rotates through its channels every few seconds; this entity
    keeps the last value seen for its own channel and ignores the others.
    """

    def __init__(self, hub: CasambiHub, unit: Unit, channel: int) -> None:
        """Initialize a channel sensor."""
        # The unique id keeps the channel number so history survives renames.
        super().__init__(hub, unit, f"channel-{channel}")
        self.entity_description = LOUVRE_CHANNELS.get(channel, UNKNOWN_LOUVRE_CHANNEL)
        if self.entity_description is UNKNOWN_LOUVRE_CHANNEL:
            self._attr_translation_placeholders = {"channel": str(channel)}
        self._channel = channel
        self._attr_native_value = None
        self._apply_update(unit)

    @callback
    def _apply_update(self, unit: Unit) -> None:
        """Pick up the value if the pushed state carries this channel."""
        state = unit.state
        decoded = decode_channel(state.raw_state if state else None)
        if decoded is None or decoded[0] != self._channel:
            return
        if self.device_class is SensorDeviceClass.TEMPERATURE:
            self._attr_native_value = louvre_temperature(decoded[1])
        else:
            self._attr_native_value = decoded[1]

    def _snapshot(self) -> object:
        """Write only when this channel's value or availability changed."""
        return (self._attr_native_value, self.available)


class CasambiPacketSensor(CasambiBroadcastUnitEntity, SensorEntity):
    """A reading of a sensor platform sent as a rotating packet."""

    entity_description: PacketSensorDescription

    def __init__(
        self, hub: CasambiHub, unit: Unit, description: PacketSensorDescription
    ) -> None:
        """Initialize a packet sensor."""
        super().__init__(hub, unit, description.key)
        self.entity_description = description

    def _snapshot(self) -> object:
        """Write only when this reading or availability changed."""
        return (self.native_value, self.available)

    @property
    def native_value(self) -> float | None:
        """Return the latest reading of this packet."""
        value = self.hub.platform_reading(self._unit, self.entity_description.packet)
        return None if value is None else float(value)


class CasambiLuxSensor(CasambiBroadcastUnitEntity, SensorEntity):
    """The illuminance reading of a sensor platform."""

    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = LIGHT_LUX
    _attr_translation_key = "illuminance"

    def __init__(self, hub: CasambiHub, unit: Unit) -> None:
        """Initialize an illuminance sensor."""
        super().__init__(hub, unit, "illuminance")

    def _snapshot(self) -> object:
        """Write only when the illuminance or availability changed."""
        return (self.native_value, self.available)

    @property
    def native_value(self) -> int | None:
        """Return the current illuminance."""
        state = self._unit.state
        return None if state is None else state.lux
