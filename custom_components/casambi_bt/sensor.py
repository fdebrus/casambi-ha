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

Winsol louvre motors multiplex four sensor channels of their own into
their 5-byte state (bits 0-3 select the channel, bits 4-27 carry its
24-bit value). The Casambi fixture definition of the Winsol Lamel
Standard V4.1 (fixture 38915) names them by tag, channel = tag + 1:
Temperatuur (°C), #Overcurrent, Travel Distance and Module State.
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

from . import CasambiApi, CasambiConfigEntry
from .classify import UnitKind, classify_unit
from .entities import (
    CasambiBroadcastUnitEntity,
    CasambiUnitEntity,
    TypedEntityDescription,
)

_LOGGER = logging.getLogger(__name__)

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0

PACKET_WIND: Final = 1
PACKET_SOLAR: Final = 2

# Louvre state layout: a 4-bit channel header followed by a 24-bit value.
LOUVRE_HEADER_BITS: Final = 4
LOUVRE_VALUE_BITS: Final = 24


@dataclass(frozen=True, kw_only=True)
class LouvreChannel:
    """How one named sensor channel of a louvre motor is exposed."""

    key: str
    device_class: SensorDeviceClass | None = None
    unit: str | None = None
    state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    category: EntityCategory | None = EntityCategory.DIAGNOSTIC


# From the fixture definition (sensor tag = channel - 1). Confirmed on a
# Winsol SO! Climate pergola: the temperature followed the outdoor
# temperature (14 °C against 12.1 °C at night), Travel Distance rose with
# every louvre movement (20 -> 31 in an evening), #Overcurrent stayed 0
# and Module State read 50.
LOUVRE_CHANNELS: Final[dict[int, LouvreChannel]] = {
    1: LouvreChannel(
        key="louvre_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
        category=None,
    ),
    2: LouvreChannel(
        key="louvre_overcurrent",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    3: LouvreChannel(
        key="louvre_travel_distance",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    4: LouvreChannel(key="louvre_module_state"),
}


def decode_louvre_channel(raw: bytes | None) -> tuple[int, int] | None:
    """Return the (channel, value) multiplexed into a louvre state."""
    if raw is None or len(raw) * 8 < LOUVRE_HEADER_BITS + LOUVRE_VALUE_BITS:
        return None
    packed = int.from_bytes(raw, "little")
    channel = packed & ((1 << LOUVRE_HEADER_BITS) - 1)
    value = (packed >> LOUVRE_HEADER_BITS) & ((1 << LOUVRE_VALUE_BITS) - 1)
    return channel, value


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
        kind = classify_unit(unit)
        if kind is UnitKind.SENSOR_PLATFORM:
            _LOGGER.debug("Creating environment sensors for unit %s", unit.name)
            entities.append(CasambiWindSensor(casa_api, unit))
            entities.append(CasambiSolarSensor(casa_api, unit))
            if unit.unitType.get_control(UnitControlType.LUX) is not None:
                entities.append(CasambiLuxSensor(casa_api, unit))
        elif kind is UnitKind.LOUVRE:
            # One raw channel per SENSOR control the motor declares; the
            # channel headers on the wire count from 1.
            channels = sum(
                1 for c in unit.unitType.controls if c.type == UnitControlType.SENSOR
            )
            entities.extend(
                CasambiLouvreChannelSensor(casa_api, unit, channel)
                for channel in range(1, channels + 1)
            )

    async_add_entities(entities)


class CasambiLouvreChannelSensor(CasambiUnitEntity, SensorEntity):
    """One multiplexed sensor channel of a louvre motor.

    The motor rotates through its channels every few seconds; this entity
    keeps the last value seen for its own channel and ignores the others.
    Known channels get their fixture name and meaning; any other channel
    stays a raw diagnostic value.
    """

    def __init__(self, api: CasambiApi, unit: Unit, channel: int) -> None:
        """Initialize a channel sensor."""
        spec = LOUVRE_CHANNELS.get(channel)
        # The unique id keeps the channel number so history survives renames.
        desc = TypedSensorEntityDescription(
            key=unit.uuid,
            entity_type=f"channel-{channel}",
            translation_key=spec.key if spec else "louvre_channel",
        )
        self._obj: Unit
        super().__init__(api, desc, unit)
        self._channel = channel
        if spec is None:
            self._attr_translation_placeholders = {"channel": str(channel)}
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_state_class = SensorStateClass.MEASUREMENT
        else:
            self._attr_device_class = spec.device_class
            self._attr_native_unit_of_measurement = spec.unit
            self._attr_state_class = spec.state_class
            self._attr_entity_category = spec.category
        self._value: int | None = None
        self._apply_update(unit)

    @callback
    def _apply_update(self, unit: Unit) -> None:
        """Pick up the value if the pushed state carries this channel."""
        state = unit.state
        decoded = decode_louvre_channel(state.raw_state if state else None)
        if decoded is not None and decoded[0] == self._channel:
            self._value = decoded[1]

    def _snapshot(self) -> object:
        """Write only when this channel's value or availability changed."""
        return (self._value, self.available)

    @property
    def native_value(self) -> int | None:
        """Return the last value seen on this channel."""
        return self._value


class CasambiPacketSensor(CasambiBroadcastUnitEntity, SensorEntity):
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

    def _snapshot(self) -> object:
        """Write only when this reading or availability changed."""
        return (self.native_value, self.available)

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

    _packet_type = PACKET_SOLAR
    _divisor = 4.0

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a solar radiation sensor entity."""
        super().__init__(api, unit, "solar")


class CasambiLuxSensor(CasambiBroadcastUnitEntity, SensorEntity):
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

    def _snapshot(self) -> object:
        """Write only when the illuminance or availability changed."""
        return (self.native_value, self.available)

    @property
    def native_value(self) -> int | None:
        """Return the current illuminance."""
        unit = self._obj
        if unit.state is not None:
            return unit.state.lux
        return None
