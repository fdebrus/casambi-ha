"""Common functionality for entities."""

from abc import ABCMeta
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, Final, cast

from CasambiBt import Group as CasambiGroup, Scene as CasambiScene, Unit as CasambiUnit

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry
from homeassistant.helpers.entity import (
    DeviceInfo,
    Entity,
    EntityCategory,
    EntityDescription,
)

from . import CasambiApi
from .const import DOMAIN

# Snapshot meaning "write Home Assistant state on every pushed update".
_ALWAYS_WRITE: Final = object()

# Newer cores link a device to its parent by registry id and deprecate the
# identifier tuple (`via_device`, removed in 2027.8); older cores only know
# the tuple. Both populate the same `via_device_id` on the device entry.
_VIA_DEVICE_ID_SUPPORTED: Final = "via_device_id" in getattr(
    DeviceInfo, "__optional_keys__", frozenset()
)


def unit_device_info(api: CasambiApi, unit: CasambiUnit) -> DeviceInfo:
    """Return the device info of a unit, linked to the network device."""
    info: dict[str, Any] = {
        "name": unit.name,
        "manufacturer": unit.unitType.manufacturer,
        "model": unit.unitType.model,
        "sw_version": unit.firmwareVersion,
        "identifiers": {(DOMAIN, unit.uuid)},
    }
    if _VIA_DEVICE_ID_SUPPORTED and api.network_device_id is not None:
        info["via_device_id"] = api.network_device_id
    else:
        info["via_device"] = (DOMAIN, api.casa.networkId)
    return cast("DeviceInfo", info)


@dataclass(kw_only=True)
class TypedEntityDescription(EntityDescription):
    """Describes a CasambiUnitEntity."""

    entity_type: str | None = None


class CasambiEntity(Entity, metaclass=ABCMeta):
    """Placeholder class for storing a Casambi object."""

    def __init__(
        self,
        api: CasambiApi,
        description: EntityDescription,
        obj: CasambiUnit | CasambiGroup | CasambiScene | None,
    ):
        """Initialize Casambi Entity."""
        self._api = api
        self._obj = obj

        self.entity_description = description
        self._attr_has_entity_name = True

        self._attr_should_poll = False

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._api.available

    async def _async_casa_command(self, command: Coroutine[Any, Any, None]) -> None:
        """Execute a casambi-bt command and raise a HomeAssistantError on failure."""
        try:
            await command
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
            ) from err

    @callback
    def _change_callback(self, _unit: CasambiUnit) -> None:
        self.async_write_ha_state()

    @callback
    def _connection_callback(self) -> None:
        """Re-evaluate availability when the connection state changes."""
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe to connection state changes."""
        self._api.register_connection_updates(self._connection_callback)

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from connection state changes."""
        self._api.unregister_connection_updates(self._connection_callback)


class CasambiNetworkEntity(CasambiEntity, metaclass=ABCMeta):
    """Defines a Casambi Entity belonging to the network device."""

    def __init__(
        self,
        api: CasambiApi,
        description: EntityDescription | TypedEntityDescription,
        obj: CasambiGroup | CasambiScene | None = None,
    ):
        """Initialize Casambi Entity."""
        super().__init__(api, description, obj)

    @property
    def unique_id(self) -> str:
        """Return the unique ID for this entity."""
        name = f"{self._api.casa.networkId}"
        if self._obj is not None:
            if isinstance(self._obj, CasambiScene):
                name += "-scene"
            elif isinstance(self._obj, CasambiGroup):
                name += "-group"
        name += f"-{self.entity_description.key}"
        return name

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this Casambi entity."""
        return DeviceInfo(
            name=self._api.casa.networkName,
            manufacturer="Casambi",
            model="Network",
            identifiers={(DOMAIN, self._api.casa.networkId)},
            connections={(device_registry.CONNECTION_BLUETOOTH, self._api.address)},
        )


class CasambiNetworkGroup(CasambiNetworkEntity, metaclass=ABCMeta):
    """Base entity for Casambi groups."""

    def __init__(
        self,
        api: CasambiApi,
        description: TypedEntityDescription,
        obj: CasambiGroup,
    ):
        """Initialize Casambi Entity."""
        super().__init__(api, description, obj)

        self._unit_map = dict(
            zip([u.deviceId for u in obj.units], obj.units, strict=True)
        )

    @property
    def available(self) -> bool:
        """Return true if any of the units in the group is available."""
        return super().available and any(
            unit.online for unit in self._unit_map.values()
        )

    async def async_added_to_hass(self) -> None:
        """Run when the group is about to be added to hass."""
        await super().async_added_to_hass()
        group = cast("CasambiGroup", self._obj)
        for unit in group.units:
            self._api.register_unit_updates(unit, self._change_callback)

    async def async_will_remove_from_hass(self) -> None:
        """Run when the group will be removed from hass."""
        await super().async_will_remove_from_hass()
        group = cast("CasambiGroup", self._obj)
        for unit in group.units:
            self._api.unregister_unit_updates(unit, self._change_callback)


class CasambiUnitEntity(CasambiEntity, metaclass=ABCMeta):
    """Base entity for Casambi units."""

    def __init__(
        self, api: CasambiApi, description: TypedEntityDescription, obj: CasambiUnit
    ) -> None:
        """Initialize Casambi Entity."""
        super().__init__(api, description, obj)
        self._written: object = None

    @property
    def unique_id(self) -> str:
        """Return an unique identifier for the unit."""
        unit = cast("CasambiUnit", self._obj)
        desc = cast("TypedEntityDescription", self.entity_description)
        return f"{self._api.casa.networkId}-unit-{unit.uuid}-{desc.entity_type}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return specific device information."""
        return unit_device_info(self._api, cast("CasambiUnit", self._obj))

    @property
    def available(self) -> bool:
        """Return True if the unit is available."""
        unit = cast("CasambiUnit", self._obj)
        return super().available and unit.online

    def _snapshot(self) -> object:
        """Return what this entity shows, to skip writes that change nothing.

        A unit pushes its whole state several times a minute (the louvre
        motor cycles through its sensor channels every few seconds), and
        every entity of the unit is notified each time. Entities that show
        only part of that state return a hashable summary of it here, so
        that a push changing nothing they show is not written to Home
        Assistant. The default writes on every push.
        """
        return _ALWAYS_WRITE

    @callback
    def _apply_update(self, unit: CasambiUnit) -> None:
        """Pick up what this entity needs from a pushed state."""

    @callback
    def async_write_ha_state(self) -> None:
        """Write the state and remember what was written."""
        self._written = self._snapshot()
        super().async_write_ha_state()

    @callback
    def _change_callback(self, unit: CasambiUnit) -> None:
        # The library updates the unit (the very object this entity holds)
        # in place before notifying, so the comparison is against what was
        # last written, not a value read before the update.
        self._apply_update(unit)
        current = self._snapshot()
        if current is _ALWAYS_WRITE or current != self._written:
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Run when the unit is about to be added to hass."""
        await super().async_added_to_hass()
        unit = cast("CasambiUnit", self._obj)
        self._api.register_unit_updates(unit, self._change_callback)

    async def async_will_remove_from_hass(self) -> None:
        """Run when the unit will be removed from hass."""
        await super().async_will_remove_from_hass()
        unit = cast("CasambiUnit", self._obj)
        self._api.unregister_unit_updates(unit, self._change_callback)


class CasambiUnitConfigEntity(CasambiUnitEntity, metaclass=ABCMeta):
    """Base for config entities of a unit whose value is not in its state.

    Sun tracking, temperature control, frost lock and their dials belong
    to the louvre device but keep their own value; the pushed state only
    tells them whether the unit is online.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def _snapshot(self) -> object:
        """Only availability can change for this entity."""
        return self.available


class CasambiBroadcastUnitEntity(CasambiUnitEntity, metaclass=ABCMeta):
    """Base entity for units that only broadcast their state.

    Sensor platforms are passive emitters: they push their readings into
    the mesh but never join it as a controllable node, so they always
    report ``online=False``. Their entities must therefore not depend on
    the unit's online flag, only on the network connection.
    """

    @property
    def available(self) -> bool:
        """Return True if the network is connected."""
        return self._api.available
