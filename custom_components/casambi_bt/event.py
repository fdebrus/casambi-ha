"""Support for Casambi wall switch button events."""

from __future__ import annotations

import logging

from CasambiBt._switch import SwitchEvent

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry, entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CasambiApi, CasambiConfigEntry
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# State is pushed by the Casambi network, no coordinated polling is required.
PARALLEL_UPDATES = 0

EVENT_TYPES = ["press", "release", "hold", "release_after_hold", "unknown"]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CasambiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the Casambi button event entities.

    Which buttons exist can't be read from the network, so entities are
    created dynamically when a button is used for the first time and
    recreated from the entity registry on later starts.
    """
    casa_api = config_entry.runtime_data
    known: set[tuple[int, int]] = set()
    entities: dict[tuple[int, int], CasambiButtonEventEntity] = {}

    def _add_button(
        unit_id: int, button: int, pending: SwitchEvent | None = None
    ) -> CasambiButtonEventEntity:
        entity = CasambiButtonEventEntity(casa_api, unit_id, button)
        if pending is not None:
            # Must be set before adding: the add may run eagerly and process
            # async_added_to_hass immediately.
            entity.set_pending(pending)
        known.add((unit_id, button))
        entities[(unit_id, button)] = entity
        async_add_entities([entity])
        return entity

    # Recreate entities that were discovered in earlier sessions.
    registry = er.async_get(hass)
    prefix = f"{casa_api.casa.networkId}-switch-"
    for entry in er.async_entries_for_config_entry(registry, config_entry.entry_id):
        if entry.domain != "event" or not entry.unique_id.startswith(prefix):
            continue
        try:
            unit_id, button = (
                int(part) for part in entry.unique_id.removeprefix(prefix).split("-")
            )
        except ValueError:
            continue
        if (unit_id, button) not in known:
            _add_button(unit_id, button)

    @callback
    def _handle_switch_event(event: SwitchEvent) -> None:
        key = (event.unit_id, event.button)
        if key not in known:
            _LOGGER.debug(
                "Discovered new switch button: unit %i button %i",
                event.unit_id,
                event.button,
            )
            _add_button(*key, pending=event)
            return
        entities[key].handle_event(event)

    casa_api.register_switch_events(_handle_switch_event)
    config_entry.async_on_unload(
        lambda: casa_api.unregister_switch_events(_handle_switch_event)
    )


class CasambiButtonEventEntity(EventEntity):
    """Event entity for a single button of a Casambi wall switch."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = EVENT_TYPES
    _attr_translation_key = "button"

    def __init__(self, api: CasambiApi, unit_id: int, button: int) -> None:
        """Initialize the button event entity."""
        self._api = api
        self._unit_id = unit_id
        self._button = button
        self._pending: SwitchEvent | None = None

        self._attr_unique_id = f"{api.casa.networkId}-switch-{unit_id}-{button}"
        self._attr_translation_placeholders = {"button": str(button)}

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device of the switch unit if known, else the network."""
        for unit in self._api.get_units():
            if unit.deviceId == self._unit_id:
                return DeviceInfo(
                    name=unit.name,
                    manufacturer=unit.unitType.manufacturer,
                    model=unit.unitType.model,
                    sw_version=unit.firmwareVersion,
                    identifiers={(DOMAIN, unit.uuid)},
                    via_device=(DOMAIN, self._api.casa.networkId),
                )
        return DeviceInfo(
            name=self._api.casa.networkName,
            manufacturer="Casambi",
            model="Network",
            identifiers={(DOMAIN, self._api.casa.networkId)},
            connections={(device_registry.CONNECTION_BLUETOOTH, self._api.address)},
        )

    @property
    def available(self) -> bool:
        """Return True if the network is connected."""
        return self._api.available

    def set_pending(self, event: SwitchEvent) -> None:
        """Store the event that triggered the creation of this entity."""
        self._pending = event

    async def async_added_to_hass(self) -> None:
        """Process the triggering event once the entity is registered."""
        if self._pending is not None:
            self.handle_event(self._pending)
            self._pending = None

    @callback
    def handle_event(self, event: SwitchEvent) -> None:
        """Handle a switch event for this button."""
        self._trigger_event(event.event.name.lower())
        self.async_write_ha_state()
