"""Switches: relays, sensor platform elements and the pergola automations.

- Relay: a unit whose only control is an on/off bit (e.g. the Winsol
  On-Off driving the heating).
- Sensor platform elements: the enable bits of its wind, rain, light and
  motion sensors.
- Sun tracking, temperature control, frost lock (per louvre) and weather
  protection (per network): the switches only turn the automations of
  ``pergola`` on and off and keep that setting across restarts.
"""

from __future__ import annotations

from typing import Any, Final

from CasambiBt import Unit, UnitControl, UnitControlType

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import (
    ExtraStoredData,
    RestoredExtraData,
    RestoreEntity,
)

from .classify import UnitKind
from .entity import (
    CasambiBroadcastUnitEntity,
    CasambiNetworkEntity,
    CasambiUnitConfigEntity,
    CasambiUnitEntity,
)
from .hub import CasambiHub
from .louvre_channels import read_bit
from .models import CasambiConfigEntry
from .pergola import PergolaController
from .weather import WeatherProtection

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0

# The sensor platform's writable enable bits, in bit-offset order
# (Sensor Platform V4: wind at 34, rain at 35, light at 36, motion at 37).
SENSOR_ENABLE_KEYS: Final = (
    "wind_sensor_enabled",
    "rain_sensor_enabled",
    "light_sensor_enabled",
    "motion_sensor_enabled",
)


def _writable_bits(unit: Unit) -> list[UnitControl]:
    """Return the on/off bits of a unit that can be written, by offset."""
    return sorted(
        (
            control
            for control in unit.unitType.controls
            if control.type == UnitControlType.ONOFF and not control.readonly
        ),
        key=lambda control: control.offset,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasambiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the relay, sensor element and automation switches."""
    data = entry.runtime_data
    hub, pergola = data.hub, data.pergola

    entities: list[SwitchEntity] = []
    for unit in hub.get_units():
        kind = hub.kind(unit)
        if kind is UnitKind.RELAY:
            entities.append(CasambiRelaySwitch(hub, unit))
        elif kind is UnitKind.LOUVRE:
            entities.append(CasambiSunTrackingSwitch(hub, pergola, unit))
            entities.append(CasambiTemperatureControlSwitch(hub, pergola, unit))
            entities.append(CasambiFrostLockSwitch(hub, pergola, unit))
        elif kind is UnitKind.SENSOR_PLATFORM:
            entities.extend(
                CasambiSensorEnableSwitch(hub, unit, control, key)
                for control, key in zip(
                    _writable_bits(unit), SENSOR_ENABLE_KEYS, strict=False
                )
            )

    if pergola.weather is not None:
        entities.append(CasambiWeatherProtectionSwitch(hub, pergola.weather))

    async_add_entities(entities)


class CasambiBitSwitch(CasambiUnitEntity, SwitchEntity):
    """A switch that writes one on/off bit of a unit."""

    def __init__(
        self, hub: CasambiHub, unit: Unit, control: UnitControl, entity_type: str
    ) -> None:
        """Initialize the switch of a bit."""
        super().__init__(hub, unit, entity_type)
        self._control = control

    def _snapshot(self) -> object:
        """Write only when the bit or availability changed."""
        return (self.is_on, self.available)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set the bit."""
        await self._async_write(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Clear the bit."""
        await self._async_write(0)

    async def _async_write(self, value: int) -> None:
        await self.hub.async_command(
            self.hub.casa.setControlValue, self._unit, self._control, value
        )
        self._write_state()


class CasambiRelaySwitch(CasambiBitSwitch):
    """A Casambi unit that only switches an external load on and off.

    E.g. the Winsol On-Off relay of a SO! Climate pergola, which drives
    the heating. It carries a single writable ONOFF bit and no dimmer, so
    it is a switch rather than a light.

    The bit is written directly in both directions. The library's turnOn
    sends a SetLevel ("restore last level"), which a unit without a dimmer
    ignores: captured on the Winsol On-Off, ``SetLevel ff05`` left the
    relay off, while turning it off already wrote the ONOFF bit.
    """

    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_name = None

    def __init__(self, hub: CasambiHub, unit: Unit) -> None:
        """Initialize a relay switch."""
        control = next(
            control
            for control in unit.unitType.controls
            if control.type == UnitControlType.ONOFF and not control.readonly
        )
        super().__init__(hub, unit, control, "relay")

    @property
    def is_on(self) -> bool:
        """Return True if the relay is on."""
        return self._unit.is_on


class CasambiSensorEnableSwitch(CasambiBroadcastUnitEntity, CasambiBitSwitch):
    """Enables or disables one element of a Casambi sensor platform.

    The bit gates the element's reporting and the network's own use of it:
    disabling the rain element also disables the pergola's built-in rain
    protection.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, hub: CasambiHub, unit: Unit, control: UnitControl, translation_key: str
    ) -> None:
        """Initialize a sensor enable switch."""
        super().__init__(hub, unit, control, f"enable-{control.offset}")
        self._attr_translation_key = translation_key

    @property
    def is_on(self) -> bool | None:
        """Return True if the sensor element is enabled."""
        state = self._unit.state
        return read_bit(state.raw_state if state else None, self._control.offset)


class CasambiRestoredSwitch(SwitchEntity, RestoreEntity):
    """A switch whose own on/off setting survives restarts and reloads.

    The state Home Assistant stores at shutdown is whatever the state
    machine holds, and these switches read "unavailable" whenever the
    network does; restored from that, they would come back off after
    every restart or reload during an outage. The setting is therefore
    kept as extra data, the way RestoreNumber keeps its value.

    The setting is restored when the entity is added and made to take
    effect by ``_apply``, which also runs on every change.
    """

    _attr_is_on: bool | None = False

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """Keep the setting itself, whatever the entity state is."""
        return RestoredExtraData({"is_on": self._attr_is_on})

    async def async_added_to_hass(self) -> None:
        """Take the setting back from the last run and apply it."""
        await super().async_added_to_hass()
        extra = await self.async_get_last_extra_data()
        if extra is not None:
            self._attr_is_on = bool(extra.as_dict().get("is_on"))
        else:
            # Before the extra data existed only the state was stored.
            last_state = await self.async_get_last_state()
            if last_state is not None and last_state.state in (STATE_ON, STATE_OFF):
                self._attr_is_on = last_state.state == STATE_ON
        self._apply(bool(self._attr_is_on))

    @callback
    def _apply(self, on: bool) -> None:
        """Make the setting take effect."""
        raise NotImplementedError

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the setting on."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the setting off."""
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        self._attr_is_on = on
        self._apply(on)
        self.async_write_ha_state()


class CasambiLouvreAutomationSwitch(CasambiUnitConfigEntity, CasambiRestoredSwitch):
    """A switch of the automation of one louvre."""

    def __init__(
        self, hub: CasambiHub, pergola: PergolaController, unit: Unit, key: str
    ) -> None:
        """Initialize an automation switch; ``key`` names it with underscores."""
        super().__init__(hub, unit, key.replace("_", "-"))
        self._pergola = pergola
        self._attr_translation_key = key

    async def _async_set(self, on: bool) -> None:
        self._attr_is_on = on
        self._apply(on)
        self._write_state()


class CasambiSunTrackingSwitch(CasambiLouvreAutomationSwitch):
    """Enables sun tracking for a louvre.

    While on, the louvre angle follows the sun so that the slats block
    direct sunlight. The sun offset number shifts the result, and the
    temperature control switch adds a temperature-dependent bias.
    """

    def __init__(self, hub: CasambiHub, pergola: PergolaController, unit: Unit) -> None:
        """Initialize a sun tracking switch."""
        super().__init__(hub, pergola, unit, "sun_tracking")
        self._tracker = pergola.trackers[unit.uuid]

    @callback
    def _apply(self, on: bool) -> None:
        self._tracker.set_enabled(on)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable sun tracking and apply it at once."""
        await super().async_turn_on(**kwargs)
        await self._tracker.async_apply()

    async def async_will_remove_from_hass(self) -> None:
        """Stop tracking when the entity is removed."""
        self._tracker.set_enabled(False)
        await super().async_will_remove_from_hass()


class CasambiTemperatureControlSwitch(CasambiLouvreAutomationSwitch):
    """Biases the sun tracking of a louvre by temperature.

    While on (and sun tracking is on), the louvres tilt toward more shade
    when the temperature source reads above the setpoint and toward more
    sun when it reads below.
    """

    def __init__(self, hub: CasambiHub, pergola: PergolaController, unit: Unit) -> None:
        """Initialize a temperature control switch."""
        super().__init__(hub, pergola, unit, "temperature_control")

    @callback
    def _apply(self, on: bool) -> None:
        self._pergola.settings[self._unit.uuid].temp_control = on


class CasambiFrostLockSwitch(CasambiLouvreAutomationSwitch):
    """Keeps a louvre from being opened when it may be frozen.

    While on and the temperature source reads below the frost lock
    threshold, Home Assistant refuses to open the louvres and sun tracking
    pauses; closing and stopping stay possible. On by default.
    """

    _attr_is_on = True

    def __init__(self, hub: CasambiHub, pergola: PergolaController, unit: Unit) -> None:
        """Initialize a frost lock switch."""
        super().__init__(hub, pergola, unit, "frost_lock")

    @callback
    def _apply(self, on: bool) -> None:
        self._pergola.settings[self._unit.uuid].frost_lock = on


class CasambiWeatherProtectionSwitch(CasambiNetworkEntity, CasambiRestoredSwitch):
    """Enables the weather protection of the network."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "weather_protection"

    def __init__(self, hub: CasambiHub, weather: WeatherProtection) -> None:
        """Initialize the weather protection switch."""
        super().__init__(hub, "weather-protection")
        self._weather = weather

    @callback
    def _apply(self, on: bool) -> None:
        if on:
            self._weather.enable()
        elif self._weather.enabled:
            self._weather.disable()
