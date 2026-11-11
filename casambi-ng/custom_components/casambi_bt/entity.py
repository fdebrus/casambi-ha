"""Base classes of the Casambi entities.

Unique ids, kept stable across versions:

- network entities: ``<network id>[-scene|-group]-<key>``
- unit entities: ``<network id>-unit-<unit uuid>-<entity type>``
"""

from __future__ import annotations

from typing import Any, Final, cast

from CasambiBt import Group, Unit

from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .hub import CasambiHub

# Snapshot meaning "write Home Assistant state on every pushed update".
_ALWAYS_WRITE: Final = object()

# Newer cores link a device to its parent by registry id and deprecate the
# identifier tuple (`via_device`, removed in 2027.8); older cores only know
# the tuple. Both populate the same `via_device_id` on the device entry.
_VIA_DEVICE_ID_SUPPORTED: Final = "via_device_id" in getattr(
    DeviceInfo, "__optional_keys__", frozenset()
)


def network_device_info(hub: CasambiHub) -> DeviceInfo:
    """Return the device info of the network itself."""
    return DeviceInfo(
        name=hub.network_name,
        manufacturer="Casambi",
        model="Network",
        identifiers={(DOMAIN, hub.network_id)},
        connections={(dr.CONNECTION_BLUETOOTH, hub.address)},
    )


def unit_device_info(hub: CasambiHub, unit: Unit) -> DeviceInfo:
    """Return the device info of a unit, linked to the network device."""
    info: dict[str, Any] = {
        "name": unit.name,
        "manufacturer": unit.unitType.manufacturer,
        "model": unit.unitType.model,
        "sw_version": unit.firmwareVersion,
        "identifiers": {(DOMAIN, unit.uuid)},
    }
    if _VIA_DEVICE_ID_SUPPORTED and hub.network_device_id is not None:
        info["via_device_id"] = hub.network_device_id
    else:
        info["via_device"] = (DOMAIN, hub.network_id)
    return cast("DeviceInfo", info)


class CasambiEntity(Entity):
    """An entity of a Casambi network, available while the link is up."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: CasambiHub) -> None:
        """Initialize the entity."""
        self.hub = hub

    @property
    def available(self) -> bool:
        """Return True while the network can be reached."""
        return self.hub.available

    async def async_added_to_hass(self) -> None:
        """Follow the link."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hub.subscribe_connection(self._handle_connection))

    @callback
    def _handle_connection(self) -> None:
        """Show the new availability when the link drops or comes back."""
        self.async_write_ha_state()


class CasambiNetworkEntity(CasambiEntity):
    """An entity of the network device: a scene, a group, the status."""

    def __init__(self, hub: CasambiHub, key: str, *, scope: str | None = None) -> None:
        """Initialize a network entity.

        :param key: What tells the entity apart within its scope.
        :param scope: "scene" or "group" for an entity made of one.
        """
        super().__init__(hub)
        prefix = hub.network_id if scope is None else f"{hub.network_id}-{scope}"
        self._attr_unique_id = f"{prefix}-{key}"
        self._attr_device_info = network_device_info(hub)


class CasambiGroupEntity(CasambiNetworkEntity):
    """An entity made of a Casambi group, following every member."""

    def __init__(self, hub: CasambiHub, group: Group) -> None:
        """Initialize a group entity."""
        super().__init__(hub, str(group.groudId), scope="group")
        self._group = group
        self._attr_name = group.name

    @property
    def available(self) -> bool:
        """Return True if any unit of the group can be reached."""
        return super().available and any(unit.online for unit in self._group.units)

    async def async_added_to_hass(self) -> None:
        """Follow the members."""
        await super().async_added_to_hass()
        for unit in self._group.units:
            self.async_on_remove(self.hub.subscribe_unit(unit, self._handle_update))

    @callback
    def _handle_update(self, _unit: Unit) -> None:
        self.async_write_ha_state()


class CasambiUnitEntity(CasambiEntity):
    """An entity of one unit.

    A unit pushes its whole state several times a minute (a louvre motor
    cycles through its sensor channels every few seconds) and each of its
    entities hears every push. An entity that shows only part of that
    state returns a hashable summary of it in ``_snapshot``, so that a push
    changing nothing it shows is not written to Home Assistant.
    """

    def __init__(self, hub: CasambiHub, unit: Unit, entity_type: str) -> None:
        """Initialize a unit entity.

        :param entity_type: What tells the entity apart among the unit's.
        """
        super().__init__(hub)
        self._unit = unit
        self._attr_unique_id = f"{hub.network_id}-unit-{unit.uuid}-{entity_type}"
        self._attr_device_info = unit_device_info(hub, unit)
        self._written: object = None

    @property
    def available(self) -> bool:
        """Return True while the unit can be reached."""
        return super().available and self._unit.online

    def _snapshot(self) -> object:
        """Return what this entity shows; the default writes on every push."""
        return _ALWAYS_WRITE

    @callback
    def _apply_update(self, unit: Unit) -> None:
        """Pick up what this entity needs from a pushed state."""

    @callback
    def _write_state(self) -> None:
        """Write the state and remember what was written.

        Every write of a unit entity goes through here, so that a later
        push is compared with what Home Assistant really shows. The one
        exception is the first write, which Home Assistant does itself
        right after ``async_added_to_hass``; that one is recorded there.
        """
        self._written = self._snapshot()
        self.async_write_ha_state()

    @callback
    def _write_if_changed(self) -> None:
        """Write the state unless it would show what it shows already."""
        current = self._snapshot()
        if current is _ALWAYS_WRITE or current != self._written:
            self._write_state()

    @callback
    def _handle_connection(self) -> None:
        self._write_state()

    @callback
    def _handle_update(self, unit: Unit) -> None:
        # The library updates the unit (the very object this entity holds)
        # in place before notifying, so the comparison is against what was
        # last written, not a value read before the update.
        self._apply_update(unit)
        self._write_if_changed()

    async def async_added_to_hass(self) -> None:
        """Follow the unit."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hub.subscribe_unit(self._unit, self._handle_update))
        # What Home Assistant is about to write as the initial state.
        self._written = self._snapshot()


class CasambiUnitConfigEntity(CasambiUnitEntity):
    """A setting kept by Home Assistant on a unit's device.

    Sun tracking, temperature control, frost lock and their dials belong
    to the louvre device but keep their own value; the pushed state only
    tells them whether the unit is online.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def _snapshot(self) -> object:
        """Only availability can change for this entity."""
        return self.available


class CasambiBroadcastUnitEntity(CasambiUnitEntity):
    """An entity of a unit that only broadcasts its state.

    Sensor platforms are passive emitters: they push their readings into
    the mesh but never join it as a controllable node, so they always
    report ``online=False``. Their entities must therefore not depend on
    the unit's online flag, only on the network connection.
    """

    @property
    def available(self) -> bool:
        """Return True while the network can be reached."""
        return self.hub.available
